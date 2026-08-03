import datetime
import decimal
import unittest

from support import *  # noqa: F401,F403  (path setup)

from dumpdata.columns import (
    Column,
    DuplicateColumnError,
    create_table_sql,
    dedupe_column_names,
    describe_columns,
    infer_logical_type,
    insert_sql,
)
from dumpdata.dialects import ORACLE, POSTGRESQL, SQLITE


class DedupeTests(unittest.TestCase):
    def test_unique_names_are_untouched(self):
        self.assertEqual(dedupe_column_names(["a", "b"]), ["a", "b"])

    def test_repeats_get_a_numeric_suffix(self):
        self.assertEqual(
            dedupe_column_names(["id", "name", "id", "id"]),
            ["id", "name", "id_2", "id_3"],
        )

    def test_collision_is_case_insensitive(self):
        self.assertEqual(dedupe_column_names(["ID", "id"]), ["ID", "id_2"])

    def test_generated_name_does_not_steal_an_existing_one(self):
        self.assertEqual(
            dedupe_column_names(["id", "id_2", "id"]),
            ["id", "id_2", "id_3"],
        )

    def test_blank_names_become_positional(self):
        self.assertEqual(dedupe_column_names(["", None, "x"]), ["column_1", "column_2", "x"])

    def test_names_are_truncated_to_the_identifier_limit(self):
        long_name = "x" * 80
        result = dedupe_column_names([long_name, long_name], max_length=64)
        self.assertEqual([len(name) for name in result], [64, 64])
        self.assertEqual(result[1][-2:], "_2")
        self.assertEqual(len(set(result)), 2)

    def test_error_mode_reports_every_repeat(self):
        with self.assertRaises(DuplicateColumnError) as caught:
            dedupe_column_names(["id", "name", "id"], on_duplicate="error")
        self.assertEqual(caught.exception.duplicates, {"id": [0, 2]})
        self.assertIn("AS customer_id", str(caught.exception))

    def test_invalid_strategy_is_rejected(self):
        with self.assertRaises(ValueError):
            dedupe_column_names(["a"], on_duplicate="ignore")


class InferTypeTests(unittest.TestCase):
    def test_single_types(self):
        self.assertEqual(infer_logical_type([1, 2, 3]), "int")
        self.assertEqual(infer_logical_type([1.5]), "float")
        self.assertEqual(infer_logical_type([decimal.Decimal("1.5")]), "decimal")
        self.assertEqual(infer_logical_type([True, False]), "bool")
        self.assertEqual(infer_logical_type([b"x"]), "bytes")
        self.assertEqual(infer_logical_type(["x"]), "text")
        self.assertEqual(infer_logical_type([datetime.date(2026, 1, 1)]), "date")
        self.assertEqual(infer_logical_type([datetime.datetime(2026, 1, 1)]), "datetime")

    def test_nulls_are_ignored_and_all_null_falls_back_to_text(self):
        self.assertEqual(infer_logical_type([None, 1, None]), "int")
        self.assertEqual(infer_logical_type([None, None]), "text")
        self.assertEqual(infer_logical_type([]), "text")

    def test_mixed_numbers_widen_to_decimal(self):
        self.assertEqual(infer_logical_type([1, 2.5]), "decimal")

    def test_dates_and_datetimes_widen_to_datetime(self):
        self.assertEqual(
            infer_logical_type([datetime.date(2026, 1, 1), datetime.datetime(2026, 1, 1)]),
            "datetime",
        )

    def test_anything_else_mixed_falls_back_to_text(self):
        self.assertEqual(infer_logical_type([1, "x"]), "text")

    def test_bool_is_not_mistaken_for_int(self):
        self.assertEqual(infer_logical_type([True]), "bool")


class DescribeTests(unittest.TestCase):
    description = [("id",), ("amount",), ("id",)]

    def test_columns_keep_source_names_and_flag_renames(self):
        columns = describe_columns(self.description, [(1, 2.5, 9)], POSTGRESQL)
        self.assertEqual([column.name for column in columns], ["id", "amount", "id_2"])
        self.assertEqual([column.source_name for column in columns], ["id", "amount", "id"])
        self.assertEqual([column.renamed for column in columns], [False, False, True])
        self.assertEqual([column.logical_type for column in columns], ["int", "float", "int"])

    def test_strict_mode_refuses_repeated_names(self):
        with self.assertRaises(DuplicateColumnError):
            describe_columns(self.description, [(1, 2.5, 9)], POSTGRESQL, on_duplicate="error")

    def test_short_rows_do_not_break_inference(self):
        columns = describe_columns(self.description, [(1,)], POSTGRESQL)
        self.assertEqual(columns[2].logical_type, "text")


class StatementTests(unittest.TestCase):
    columns = [
        Column("id", "id", "int"),
        Column("amount", "amount", "decimal"),
        Column("id_2", "id", "int"),
    ]

    def test_create_table_uses_engine_types(self):
        sql = create_table_sql("dump", self.columns, POSTGRESQL)
        self.assertIn('CREATE TABLE IF NOT EXISTS "dump"', sql)
        self.assertIn('"id" BIGINT', sql)
        self.assertIn('"amount" NUMERIC', sql)
        self.assertIn('"id_2" BIGINT', sql)

    def test_oracle_create_table_omits_if_not_exists(self):
        sql = create_table_sql("dump", self.columns, ORACLE)
        self.assertNotIn("IF NOT EXISTS", sql)
        self.assertIn('"id" NUMBER(19)', sql)

    def test_create_table_qualifies_the_schema(self):
        self.assertIn('"stage"."dump"', create_table_sql("dump", self.columns, POSTGRESQL, schema="stage"))

    def test_insert_matches_the_paramstyle(self):
        self.assertEqual(
            insert_sql("dump", self.columns, SQLITE),
            'INSERT INTO "dump" ("id", "amount", "id_2") VALUES (?, ?, ?)',
        )
        self.assertTrue(insert_sql("dump", self.columns, ORACLE).endswith("(:1, :2, :3)"))

    def test_empty_column_lists_are_rejected(self):
        with self.assertRaises(ValueError):
            create_table_sql("dump", [], POSTGRESQL)
        with self.assertRaises(ValueError):
            insert_sql("dump", [], POSTGRESQL)


if __name__ == "__main__":
    unittest.main()
