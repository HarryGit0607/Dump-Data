import os
import sqlite3
import tempfile
import unittest

from support import (  # noqa: F401  (also sets sys.path)
    JOIN_QUERY,
    KEYED_JOIN_QUERY,
    make_source,
    make_target,
    row_count,
    table_columns,
    table_types,
)

from dumpdata.columns import DuplicateColumnError
from dumpdata.copier import (
    Checkpoint,
    CopyOptions,
    Source,
    Target,
    _key_index,
    build_keyset_query,
    copy_query_to_table,
    default_is_retryable,
)
from dumpdata.dialects import ORACLE, SQLITE


class TempDatabases(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.source_path = os.path.join(self.directory, "source.db")
        self.target_path = os.path.join(self.directory, "target.db")
        self.source_connection = make_source(self.source_path)
        self.target_connection = make_target(self.target_path)

    def tearDown(self):
        self.source_connection.close()
        self.target_connection.close()

    def source(self, query=JOIN_QUERY, **kwargs):
        return Source(connection=self.source_connection, query=query, dialect="sqlite", **kwargs)

    def target(self, table="dump", **kwargs):
        return Target(connection=self.target_connection, table=table, dialect="sqlite", **kwargs)


class OptionValidationTests(unittest.TestCase):
    def test_keyset_requires_a_key_column(self):
        with self.assertRaises(ValueError):
            CopyOptions(mode="keyset")

    def test_checkpoints_require_keyset_mode(self):
        with self.assertRaises(ValueError):
            CopyOptions(mode="stream", checkpoint_path="/tmp/x.json")

    def test_bad_mode_and_batch_size_are_rejected(self):
        with self.assertRaises(ValueError):
            CopyOptions(mode="magic")
        with self.assertRaises(ValueError):
            CopyOptions(batch_size=0)


class KeysetQueryTests(unittest.TestCase):
    def test_the_original_query_is_wrapped_not_edited(self):
        sql = build_keyset_query("SELECT a FROM t ORDER BY a;", "a", SQLITE, 500)
        self.assertIn("SELECT a FROM t ORDER BY a\n) src", sql)
        self.assertIn('WHERE src."a" > ?', sql)
        self.assertTrue(sql.endswith("LIMIT 500"))

    def test_the_key_bind_comes_after_the_query_own_parameters(self):
        sql = build_keyset_query("SELECT a FROM t WHERE b = :1", "a", ORACLE, 10, param_offset=1)
        self.assertIn('WHERE src."a" > :2', sql)
        self.assertTrue(sql.endswith("FETCH FIRST 10 ROWS ONLY"))

    def test_the_first_page_has_no_bound(self):
        sql = build_keyset_query("SELECT a FROM t", "a", SQLITE, 10, with_bound=False)
        self.assertNotIn("WHERE", sql)

    def test_inclusive_bound_is_available_for_restarts(self):
        sql = build_keyset_query("SELECT a FROM t", "a", SQLITE, 10, inclusive=True)
        self.assertIn('src."a" >= ?', sql)


class StreamCopyTests(TempDatabases):
    def test_join_with_repeated_names_keeps_every_column(self):
        result = copy_query_to_table(
            self.source(), self.target(), CopyOptions(batch_size=7)
        )
        self.assertEqual(result.rows_copied, 100)
        self.assertEqual(row_count(self.target_connection, "dump"), 100)
        # customers.(id, name, created_at) + orders.(id, customer_id, name, amount, created_at)
        self.assertEqual(
            table_columns(self.target_connection, "dump"),
            [
                "id",
                "name",
                "created_at",
                "id_2",
                "customer_id",
                "name_2",
                "amount",
                "created_at_2",
            ],
        )
        self.assertEqual(
            [(column.source_name, column.name) for column in result.renamed_columns],
            [("id", "id_2"), ("name", "name_2"), ("created_at", "created_at_2")],
        )

    def test_batching_does_not_change_the_row_count(self):
        for batch_size in (1, 3, 100, 1000):
            self.tearDown()
            self.setUp()
            result = copy_query_to_table(
                self.source(), self.target(), CopyOptions(batch_size=batch_size)
            )
            self.assertEqual(result.rows_copied, 100)
            self.assertEqual(row_count(self.target_connection, "dump"), 100)

    def test_inferred_types_follow_the_data(self):
        copy_query_to_table(self.source(), self.target())
        types = table_types(self.target_connection, "dump")
        self.assertEqual(types["id"], "INTEGER")
        self.assertEqual(types["amount"], "REAL")
        self.assertEqual(types["name"], "TEXT")

    def test_column_types_can_be_overridden(self):
        copy_query_to_table(
            self.source(),
            self.target(),
            CopyOptions(column_types={"amount": "decimal", "created_at": "datetime"}),
        )
        types = table_types(self.target_connection, "dump")
        self.assertEqual(types["amount"], "NUMERIC")
        self.assertEqual(types["created_at"], "TIMESTAMP")

    def test_strict_mode_refuses_the_ambiguous_join(self):
        with self.assertRaises(DuplicateColumnError):
            copy_query_to_table(
                self.source(), self.target(), CopyOptions(on_duplicate_columns="error")
            )

    def test_progress_is_reported_per_batch(self):
        seen = []
        copy_query_to_table(
            self.source(),
            self.target(),
            CopyOptions(batch_size=25, progress=seen.append),
        )
        self.assertEqual([event.batch for event in seen], [1, 2, 3, 4])
        self.assertEqual([event.rows_copied for event in seen], [25, 50, 75, 100])
        self.assertGreaterEqual(seen[-1].rows_per_second, 0)
        self.assertIsNone(seen[-1].eta_seconds(100))
        self.assertGreater(seen[0].eta_seconds(1000), 0)
        self.assertIn("rows/s", str(seen[0]))

    def test_query_parameters_are_passed_through(self):
        result = copy_query_to_table(
            self.source(
                query="SELECT id, name FROM customers WHERE id <= ?",
                params=(4,),
            ),
            self.target(),
        )
        self.assertEqual(result.rows_copied, 4)

    def test_an_empty_result_still_creates_the_table(self):
        result = copy_query_to_table(
            self.source(query="SELECT id, name FROM customers WHERE id < 0"),
            self.target(),
        )
        self.assertEqual(result.rows_copied, 0)
        self.assertEqual(table_columns(self.target_connection, "dump"), ["id", "name"])

    def test_truncate_replaces_the_previous_load(self):
        options = CopyOptions(truncate_target=True)
        copy_query_to_table(self.source(), self.target(), options)
        copy_query_to_table(self.source(), self.target(), options)
        self.assertEqual(row_count(self.target_connection, "dump"), 100)

    def test_without_truncate_the_load_appends(self):
        copy_query_to_table(self.source(), self.target())
        copy_query_to_table(self.source(), self.target())
        self.assertEqual(row_count(self.target_connection, "dump"), 200)


class KeysetCopyTests(TempDatabases):
    def options(self, **kwargs):
        kwargs.setdefault("mode", "keyset")
        kwargs.setdefault("key_column", "order_id")
        kwargs.setdefault("batch_size", 9)
        return CopyOptions(**kwargs)

    def test_paging_copies_every_row_exactly_once(self):
        result = copy_query_to_table(
            self.source(query=KEYED_JOIN_QUERY), self.target(), self.options()
        )
        self.assertEqual(result.rows_copied, 100)
        self.assertEqual(result.last_key, 100)
        self.assertEqual(row_count(self.target_connection, "dump"), 100)
        distinct = self.target_connection.execute(
            'SELECT COUNT(DISTINCT order_id) FROM "dump"'
        ).fetchone()[0]
        self.assertEqual(distinct, 100)

    def test_a_batch_aligned_result_terminates(self):
        result = copy_query_to_table(
            self.source(query=KEYED_JOIN_QUERY), self.target(), self.options(batch_size=10)
        )
        self.assertEqual(result.rows_copied, 100)
        self.assertEqual(result.batches, 10)

    def test_key_start_skips_what_was_already_loaded(self):
        result = copy_query_to_table(
            self.source(query=KEYED_JOIN_QUERY), self.target(), self.options(key_start=60)
        )
        self.assertEqual(result.rows_copied, 40)

    def test_a_non_unique_key_would_skip_rows_and_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            copy_query_to_table(
                self.source(query=JOIN_QUERY), self.target(), self.options(key_column="name")
            )
        self.assertIn("is not unique", str(caught.exception))

    def test_the_uniqueness_check_can_be_waived(self):
        result = copy_query_to_table(
            self.source(query=JOIN_QUERY),
            self.target(),
            self.options(key_column="name", require_unique_key=False),
        )
        self.assertGreater(result.rows_copied, 0)

    def test_an_ambiguous_key_is_reported_clearly(self):
        with self.assertRaises(ValueError) as caught:
            _key_index([("id",), ("name",), ("id",)], "id")
        self.assertIn("appears 2 times", str(caught.exception))

    def test_a_missing_key_is_reported_clearly(self):
        with self.assertRaises(ValueError) as caught:
            copy_query_to_table(
                self.source(query=KEYED_JOIN_QUERY), self.target(), self.options(key_column="nope")
            )
        message = str(caught.exception)
        self.assertIn("nope", message)
        self.assertIn("exactly once", message)

    def test_an_empty_result_still_creates_the_table(self):
        result = copy_query_to_table(
            self.source(query="SELECT id AS order_id FROM customers WHERE id < 0"),
            self.target(),
            self.options(),
        )
        self.assertEqual(result.rows_copied, 0)
        self.assertEqual(table_columns(self.target_connection, "dump"), ["order_id"])


class CheckpointTests(TempDatabases):
    def setUp(self):
        super().setUp()
        self.checkpoint_path = os.path.join(self.directory, "checkpoint.json")

    def options(self, **kwargs):
        kwargs.setdefault("mode", "keyset")
        kwargs.setdefault("key_column", "order_id")
        kwargs.setdefault("batch_size", 10)
        kwargs.setdefault("checkpoint_path", self.checkpoint_path)
        return CopyOptions(**kwargs)

    def test_an_interrupted_run_resumes_where_it_stopped(self):
        first = copy_query_to_table(
            self.source(query=KEYED_JOIN_QUERY), self.target(), self.options(max_batches=3)
        )
        self.assertFalse(first.complete)
        self.assertEqual(first.rows_copied, 30)
        self.assertTrue(os.path.exists(self.checkpoint_path))

        second = copy_query_to_table(
            self.source(query=KEYED_JOIN_QUERY), self.target(), self.options()
        )
        self.assertTrue(second.complete)
        self.assertEqual(second.resumed_from, 30)
        self.assertEqual(second.rows_copied, 100)
        self.assertEqual(row_count(self.target_connection, "dump"), 100)
        distinct = self.target_connection.execute(
            'SELECT COUNT(DISTINCT order_id) FROM "dump"'
        ).fetchone()[0]
        self.assertEqual(distinct, 100)

    def test_a_finished_run_clears_the_checkpoint(self):
        copy_query_to_table(self.source(query=KEYED_JOIN_QUERY), self.target(), self.options())
        self.assertFalse(os.path.exists(self.checkpoint_path))

    def test_resume_does_not_recreate_or_truncate_the_target(self):
        self.assertFalse(
            copy_query_to_table(
                self.source(query=KEYED_JOIN_QUERY),
                self.target(),
                self.options(max_batches=2, truncate_target=True),
            ).complete
        )
        result = copy_query_to_table(
            self.source(query=KEYED_JOIN_QUERY),
            self.target(),
            self.options(truncate_target=True),
        )
        self.assertEqual(result.rows_copied, 100)
        self.assertEqual(row_count(self.target_connection, "dump"), 100)

    def test_key_values_survive_a_round_trip(self):
        import datetime
        import decimal

        checkpoint = Checkpoint(self.checkpoint_path)
        for value in (
            42,
            "abc",
            3.5,
            decimal.Decimal("1.25"),
            datetime.date(2026, 3, 31),
            datetime.datetime(2026, 3, 31, 12, 30),
        ):
            checkpoint.save(last_key=value, rows_copied=1, table="dump")
            self.assertEqual(checkpoint.load()["last_key"], value)
        checkpoint.clear()
        self.assertIsNone(checkpoint.load())


class FlakyConnection:
    """Target connection whose first N executemany calls fail."""

    class OperationalError(Exception):
        pass

    def __init__(self, connection, failures):
        self._connection = connection
        self.failures = failures
        self.attempts = 0

    def cursor(self):
        return FlakyCursor(self, self._connection.cursor())

    def commit(self):
        return self._connection.commit()

    def rollback(self):
        return self._connection.rollback()


class FlakyCursor:
    def __init__(self, owner, cursor):
        self._owner = owner
        self._cursor = cursor

    def execute(self, *args):
        return self._cursor.execute(*args)

    def executemany(self, statement, rows):
        self._owner.attempts += 1
        if self._owner.attempts <= self._owner.failures:
            raise FlakyConnection.OperationalError("connection reset")
        return self._cursor.executemany(statement, rows)

    def close(self):
        return self._cursor.close()


class RetryTests(TempDatabases):
    def test_transient_failures_are_retried(self):
        flaky = FlakyConnection(self.target_connection, failures=2)
        target = Target(connection=flaky, table="dump", dialect="sqlite")
        result = copy_query_to_table(
            self.source(),
            target,
            CopyOptions(batch_size=100, retries=3, retry_backoff_seconds=0),
        )
        self.assertEqual(result.rows_copied, 100)
        self.assertEqual(flaky.attempts, 3)

    def test_giving_up_raises_the_driver_error(self):
        flaky = FlakyConnection(self.target_connection, failures=99)
        target = Target(connection=flaky, table="dump", dialect="sqlite")
        with self.assertRaises(FlakyConnection.OperationalError):
            copy_query_to_table(
                self.source(),
                target,
                CopyOptions(batch_size=100, retries=1, retry_backoff_seconds=0),
            )

    def test_permanent_errors_are_not_retried(self):
        flaky = FlakyConnection(self.target_connection, failures=99)
        target = Target(connection=flaky, table="dump", dialect="sqlite")
        with self.assertRaises(FlakyConnection.OperationalError):
            copy_query_to_table(
                self.source(),
                target,
                CopyOptions(
                    batch_size=100,
                    retries=5,
                    retry_backoff_seconds=0,
                    is_retryable=lambda error: False,
                ),
            )
        self.assertEqual(flaky.attempts, 1)

    def test_the_default_predicate_recognises_driver_class_names(self):
        self.assertTrue(default_is_retryable(sqlite3.OperationalError("x")))
        self.assertFalse(default_is_retryable(ValueError("x")))


class CursorFactoryTests(TempDatabases):
    def test_a_custom_cursor_factory_is_used(self):
        calls = []

        def factory(connection):
            calls.append(connection)
            return connection.cursor()

        copy_query_to_table(
            self.source(cursor_factory=factory, arraysize=5),
            self.target(),
            CopyOptions(batch_size=50),
        )
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
