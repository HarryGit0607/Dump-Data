import csv
import gzip
import os
import tempfile
import unittest

from support import JOIN_QUERY, make_source  # noqa: F401  (also sets sys.path)

from dumpdata.copier import Source
from dumpdata.csvexport import bulk_load_command, export_query_to_csv


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.connection = make_source(os.path.join(self.directory, "s.db"))
        self.out = os.path.join(self.directory, "dump.csv")

    def tearDown(self):
        self.connection.close()

    def source(self, query=JOIN_QUERY):
        return Source(connection=self.connection, query=query, dialect="sqlite")

    def read(self, path):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", newline="", encoding="utf-8") as handle:
            return list(csv.reader(handle))

    def test_header_renames_the_repeated_join_columns(self):
        result = export_query_to_csv(self.source(), self.out, batch_size=13)
        self.assertEqual(result.rows, 100)
        rows = self.read(self.out)
        self.assertEqual(
            rows[0],
            ["id", "name", "created_at", "id_2", "customer_id", "name_2", "amount", "created_at_2"],
        )
        self.assertEqual(len(rows), 101)
        self.assertGreaterEqual(result.rows_per_second, 0)

    def test_nulls_use_the_configured_marker(self):
        export_query_to_csv(
            self.source(query="SELECT NULL AS a, 1 AS b"), self.out, null_string="\\N"
        )
        self.assertEqual(self.read(self.out)[1], ["\\N", "1"])

    def test_bytes_are_hex_encoded_and_bools_become_digits(self):
        export_query_to_csv(self.source(query="SELECT x'0aff' AS a, 1 = 1 AS b"), self.out)
        self.assertEqual(self.read(self.out)[1], ["0aff", "1"])

    def test_splitting_produces_loadable_parts(self):
        result = export_query_to_csv(self.source(), self.out, max_rows_per_file=30)
        self.assertEqual(len(result.files), 4)
        self.assertEqual(result.rows, 100)
        counts = [len(self.read(path)) - 1 for path in result.files]
        self.assertEqual(counts, [30, 30, 30, 10])
        for path in result.files:
            self.assertEqual(self.read(path)[0][0], "id")

    def test_gzip_output(self):
        result = export_query_to_csv(self.source(), self.out, compress=True)
        self.assertEqual(len(self.read(result.files[0])), 101)

    def test_a_custom_delimiter_is_honoured(self):
        export_query_to_csv(self.source(query="SELECT 1 AS a, 2 AS b"), self.out, delimiter="|")
        with open(self.out, encoding="utf-8") as handle:
            self.assertIn("1|2", handle.read())

    def test_progress_is_reported(self):
        seen = []
        export_query_to_csv(self.source(), self.out, batch_size=40, progress=seen.append)
        self.assertEqual([event.rows_copied for event in seen], [40, 80, 100])


class LoadCommandTests(unittest.TestCase):
    def test_each_engine_gets_its_own_loader(self):
        self.assertIn("\\copy", bulk_load_command("postgresql", "dump", "/tmp/a.csv"))
        self.assertIn("LOAD DATA LOCAL INFILE", bulk_load_command("mysql", "dump", "/tmp/a.csv"))
        self.assertIn("bcp", bulk_load_command("sqlserver", "dump", "/tmp/a.csv"))
        self.assertIn("sqlldr", bulk_load_command("oracle", "dump", "/tmp/a.csv"))
        self.assertIn(".import", bulk_load_command("sqlite", "dump", "/tmp/a.csv"))

    def test_header_handling_is_reflected_in_the_command(self):
        self.assertIn("HEADER true", bulk_load_command("postgresql", "d", "/tmp/a.csv"))
        self.assertNotIn(
            "HEADER true", bulk_load_command("postgresql", "d", "/tmp/a.csv", header=False)
        )
        self.assertIn("IGNORE 1 LINES", bulk_load_command("mysql", "d", "/tmp/a.csv"))
        self.assertIn("SKIP=1", bulk_load_command("oracle", "d", "/tmp/a.csv"))

    def test_the_schema_is_qualified(self):
        self.assertIn(
            '"stage"."d"', bulk_load_command("postgresql", "d", "/tmp/a.csv", schema="stage")
        )


if __name__ == "__main__":
    unittest.main()
