import io
import contextlib
import os
import tempfile
import unittest

from support import JOIN_QUERY, make_source, make_target, row_count  # noqa: F401

from dumpdata.cli import main, read_query


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.source_path = os.path.join(self.directory, "source.db")
        self.target_path = os.path.join(self.directory, "target.db")
        make_source(self.source_path).close()

    def run_cli(self, *argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = main(list(argv))
        return code, buffer.getvalue()

    def test_columns_reports_the_repeated_names_and_a_fixed_select_list(self):
        code, output = self.run_cli(
            "columns",
            "--driver", "sqlite3",
            "--dsn", self.source_path,
            "--dialect", "sqlite",
            "--query", JOIN_QUERY,
        )
        self.assertEqual(code, 0)
        self.assertIn("Repeated column names", output)
        self.assertIn("id: positions [0, 3]", output)
        self.assertIn('"id_2"', output)

    def test_columns_is_quiet_when_there_is_nothing_to_fix(self):
        _, output = self.run_cli(
            "columns",
            "--driver", "sqlite3",
            "--dsn", self.source_path,
            "--dialect", "sqlite",
            "--query", "SELECT id, name FROM customers",
        )
        self.assertIn("No repeated column names", output)

    def test_plan_prints_a_ctas(self):
        _, output = self.run_cli(
            "plan",
            "--dialect", "oracle",
            "--query", "SELECT 1 AS a FROM dual",
            "--table", "PGIS_POLICY_DTL",
            "--nologging",
            "--parallel", "8",
        )
        self.assertIn('CREATE TABLE "PGIS_POLICY_DTL" NOLOGGING PARALLEL 8 AS', output)

    def test_plan_aliases_repeats_when_it_can_probe_the_query(self):
        _, output = self.run_cli(
            "plan",
            "--dialect", "sqlite",
            "--driver", "sqlite3",
            "--dsn", self.source_path,
            "--query", JOIN_QUERY,
            "--table", "dump",
        )
        self.assertIn("repeated column names aliased apart", output)
        self.assertIn('AS "id_2"', output)

    def test_plan_can_print_an_insert_select(self):
        _, output = self.run_cli(
            "plan",
            "--dialect", "oracle",
            "--query", "SELECT 1 AS a FROM dual",
            "--table", "t",
            "--mode", "insert",
            "--direct-path",
        )
        self.assertIn("/*+ APPEND */", output)

    def test_copy_moves_the_rows(self):
        code, output = self.run_cli(
            "copy",
            "--source-driver", "sqlite3",
            "--source-dsn", self.source_path,
            "--source-dialect", "sqlite",
            "--target-driver", "sqlite3",
            "--target-dsn", self.target_path,
            "--target-dialect", "sqlite",
            "--query", JOIN_QUERY,
            "--table", "dump",
            "--batch-size", "16",
            "--quiet",
        )
        self.assertEqual(code, 0)
        self.assertIn("copied 100 rows", output)
        self.assertIn("renamed duplicate columns: id -> id_2", output)
        target = make_target(self.target_path)
        self.assertEqual(row_count(target, "dump"), 100)
        target.close()

    def test_copy_reports_an_early_stop(self):
        _, output = self.run_cli(
            "copy",
            "--source-driver", "sqlite3",
            "--source-dsn", self.source_path,
            "--source-dialect", "sqlite",
            "--target-driver", "sqlite3",
            "--target-dsn", self.target_path,
            "--target-dialect", "sqlite",
            "--query", "SELECT id AS order_id, name FROM customers",
            "--table", "dump",
            "--mode", "keyset",
            "--key-column", "order_id",
            "--batch-size", "5",
            "--max-batches", "2",
            "--quiet",
        )
        self.assertIn("stopped early", output)

    def test_csv_exports_and_prints_the_loader(self):
        out = os.path.join(self.directory, "dump.csv")
        _, output = self.run_cli(
            "csv",
            "--driver", "sqlite3",
            "--dsn", self.source_path,
            "--dialect", "sqlite",
            "--query", JOIN_QUERY,
            "--out", out,
            "--table", "dump",
            "--load-dialect", "postgresql",
            "--quiet",
        )
        self.assertIn("exported 100 rows", output)
        self.assertIn("\\copy", output)
        self.assertTrue(os.path.exists(out))

    def test_a_query_can_be_read_from_a_file(self):
        path = os.path.join(self.directory, "q.sql")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(JOIN_QUERY)
        self.assertEqual(read_query(f"@{path}"), JOIN_QUERY)
        self.assertEqual(read_query("SELECT 1"), "SELECT 1")

    def test_an_unknown_dialect_is_rejected_before_connecting(self):
        with self.assertRaises(ValueError):
            self.run_cli(
                "plan", "--dialect", "db2", "--query", "SELECT 1", "--table", "t"
            )


if __name__ == "__main__":
    unittest.main()
