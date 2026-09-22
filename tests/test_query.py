import csv
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from support import make_source  # noqa: F401  (also sets sys.path)

from dumpdata.cli import DEFAULT_QUERY, main
from dumpdata.queryrun import format_preview, preview_query


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "policy.db")
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            "CREATE TABLE PGIT_POLICY (POLH_SYS_ID INTEGER, POLH_NO TEXT)"
        )
        self.connection.executemany(
            "INSERT INTO PGIT_POLICY VALUES (?, ?)",
            [(1, "P-1"), (2, "P-2"), (3, "P-3")],
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def test_select_star_returns_every_column(self):
        columns, rows = preview_query(self.connection, "SELECT * FROM PGIT_POLICY", limit=10)
        self.assertEqual(columns, ["POLH_SYS_ID", "POLH_NO"])
        self.assertEqual(rows, [(1, "P-1"), (2, "P-2"), (3, "P-3")])

    def test_limit_truncates_the_preview(self):
        columns, rows = preview_query(self.connection, DEFAULT_QUERY, limit=1)
        self.assertEqual(len(columns), 2)
        self.assertEqual(rows, [(1, "P-1")])

    def test_format_preview_is_csv(self):
        text = format_preview(["A", "B"], [(1, None)])
        self.assertEqual(text, "A,B\n1,\n")


class QueryCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "policy.db")
        connection = sqlite3.connect(self.path)
        connection.execute(
            "CREATE TABLE PGIT_POLICY (POLH_SYS_ID INTEGER, POLH_NO TEXT)"
        )
        connection.executemany(
            "INSERT INTO PGIT_POLICY VALUES (?, ?)",
            [(10, "A"), (20, "B")],
        )
        connection.commit()
        connection.close()

    def run_cli(self, *argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_query_is_select_star_from_pgit_policy(self):
        code, output, stderr = self.run_cli(
            "query",
            "--driver", "sqlite3",
            "--dsn", self.path,
            "--dialect", "sqlite",
        )
        self.assertEqual(code, 0)
        rows = list(csv.reader(io.StringIO(output)))
        self.assertEqual(rows[0], ["POLH_SYS_ID", "POLH_NO"])
        self.assertEqual(rows[1:], [["10", "A"], ["20", "B"]])
        self.assertIn("2 row(s)", stderr)

    def test_query_file_dumps_csv(self):
        out = os.path.join(self.directory, "pgit_policy.csv")
        sql = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "sql",
            "queries",
            "pgit_policy.sql",
        )
        code, output, _stderr = self.run_cli(
            "query",
            "--driver", "sqlite3",
            "--dsn", self.path,
            "--dialect", "sqlite",
            "--query", "@" + sql,
            "--out", out,
        )
        self.assertEqual(code, 0)
        self.assertIn("2 rows", output)
        with open(out, encoding="utf-8") as handle:
            self.assertEqual(handle.readline().strip(), "POLH_SYS_ID,POLH_NO")
            self.assertEqual(handle.readline().strip(), "10,A")
