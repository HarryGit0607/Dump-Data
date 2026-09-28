import os
import sqlite3
import tempfile
import unittest

from support import make_source  # noqa: F401  (sys.path)

from dumpdata.cli import main
from dumpdata.tpa_dashboard import insights_from_connection, write_dashboard
from dumpdata.tpa_insights import (
    compute_insights,
    map_columns,
    office_from_pol_no,
    parse_pol_no,
)


def seed_tpa(connection):
    connection.execute(
        """
        CREATE TABLE PGIPH_STG_TPA_UPLOAD (
            PSTU_POL_NO TEXT,
            PSTU_CLM_NO TEXT,
            PSTU_CLM_DT TEXT,
            PSTU_CLM_AMT REAL,
            PSTU_APPR_AMT REAL,
            PSTU_PAID_AMT REAL,
            PSTU_STATUS TEXT,
            PSTU_HOSP_NAME TEXT,
            PSTU_MEMB_CODE TEXT,
            PSTU_TPA_CODE TEXT
        )
        """
    )
    rows = [
        ("771001/48/2013/371", "C1", "2013-06-01", 1200, 1000, 1000, "PAID", "City Hospital", "M1", "TPA1"),
        ("771001/48/2013/371", "C2", "2013-11-02", 800, 800, 800, "PAID", "City Hospital", "M1", "TPA1"),
        ("771001/21/2014/090", "C3", "2014-02-10", 15000, 9000, 9000, "PARTIAL", "Metro Care", "M2", "TPA1"),
        ("771001/21/2015/012", "C4", "2015-08-19", 500, 0, 0, "REJECT", "City Hospital", "M3", "TPA2"),
        ("771002/11/2013/001", "X1", "2013-01-01", 99, 99, 99, "PAID", "Other", "MX", "TPA9"),
    ]
    connection.executemany(
        "INSERT INTO PGIPH_STG_TPA_UPLOAD VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    connection.commit()


class OfficeCodeTests(unittest.TestCase):
    def test_example_policy_splits_the_way_the_user_described(self):
        parsed = parse_pol_no("771001/48/2013/371")
        self.assertEqual(parsed["office"], "771001")
        self.assertEqual(parsed["dept"], "48")
        self.assertEqual(parsed["year"], "2013")
        self.assertEqual(parsed["serial"], "371")
        self.assertEqual(office_from_pol_no("771001/48/2013/371"), "771001")


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        seed_tpa(self.connection)

    def tearDown(self):
        self.connection.close()

    def test_office_filter_drops_771002_and_counts_repeats(self):
        payload = insights_from_connection(
            self.connection,
            office="771001",
            table="PGIPH_STG_TPA_UPLOAD",
            sqlite=True,
            source="sqlite",
        )
        self.assertEqual(payload["kpis"]["claims"], 4)
        self.assertEqual(payload["kpis"]["distinct_policies"], 3)
        self.assertEqual(payload["kpis"]["repeat_policies"], 1)
        self.assertEqual(payload["repeat_count"], 1)
        self.assertEqual(payload["repeats"][0]["policy"], "771001/48/2013/371")
        self.assertEqual(payload["repeats"][0]["claims"], 2)
        years = {row["year"]: row["claims"] for row in payload["by_year"]}
        self.assertEqual(years["2013"], 2)
        self.assertEqual(years["2014"], 1)
        self.assertEqual(years["2015"], 1)
        depts = {row["name"]: row["count"] for row in payload["by_dept"]}
        self.assertEqual(depts["48"], 2)
        self.assertEqual(depts["21"], 2)
        self.assertAlmostEqual(payload["kpis"]["total_amount"], 17500)
        self.assertEqual(payload["quality"]["office_rows"], 4)
        self.assertEqual(payload["quality"]["table_rows"], 5)
        self.assertEqual(payload["meta"]["mapped"]["policy"], "PSTU_POL_NO")

    def test_csv_mixed_offices_are_excluded(self):
        rows = [
            {"PSTU_POL_NO": "771001/48/2013/371", "PSTU_CLM_AMT": "10"},
            {"PSTU_POL_NO": "771009/48/2013/001", "PSTU_CLM_AMT": "99"},
        ]
        payload = compute_insights(
            rows,
            office="771001",
            columns=["PSTU_POL_NO", "PSTU_CLM_AMT"],
            mapped=map_columns(["PSTU_POL_NO", "PSTU_CLM_AMT"]),
            source="csv",
        )
        self.assertEqual(payload["kpis"]["claims"], 1)
        self.assertEqual(payload["quality"]["rows_other_offices_seen"], 1)


class DashboardCliTests(unittest.TestCase):
    def test_cli_writes_html_and_data(self):
        directory = tempfile.mkdtemp()
        db = os.path.join(directory, "tpa.db")
        connection = sqlite3.connect(db)
        seed_tpa(connection)
        connection.close()
        out = os.path.join(directory, "dash")
        code = main(
            [
                "dashboard",
                "--driver",
                "sqlite3",
                "--dsn",
                db,
                "--dialect",
                "sqlite",
                "--out",
                out,
            ]
        )
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(out, "index.html")))
        self.assertTrue(os.path.isfile(os.path.join(out, "data.json")))
        self.assertTrue(os.path.isfile(os.path.join(out, "app.js")))
        with open(os.path.join(out, "data.json"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("771001", text)
        self.assertIn('"claims": 4', text)
