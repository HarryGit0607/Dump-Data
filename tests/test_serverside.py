import os
import tempfile
import unittest

from support import JOIN_QUERY, make_source  # noqa: F401  (also sets sys.path)

from dumpdata.serverside import (
    aliased_select_list,
    check_duplicate_columns,
    ctas_sql,
    insert_select_sql,
    result_columns,
    rewrite_for_ctas,
)

QUERY = "SELECT a, b FROM t WHERE x = 1"


class CtasTests(unittest.TestCase):
    def test_each_engine_gets_its_own_form(self):
        self.assertTrue(ctas_sql("dump", QUERY, "postgresql").startswith('CREATE TABLE "dump" AS'))
        self.assertTrue(ctas_sql("dump", QUERY, "mysql").startswith("CREATE TABLE `dump` AS"))
        self.assertTrue(ctas_sql("dump", QUERY, "oracle").startswith('CREATE TABLE "dump" AS'))
        self.assertIn("INTO [dump]", ctas_sql("dump", QUERY, "sqlserver"))

    def test_sqlserver_uses_select_into_over_a_derived_table(self):
        sql = ctas_sql("dump", QUERY, "mssql")
        self.assertIn("SELECT src.*", sql)
        self.assertIn(") AS src", sql)

    def test_trailing_semicolons_are_dropped(self):
        self.assertNotIn(";", ctas_sql("dump", QUERY + ";", "postgresql"))

    def test_logging_and_parallel_options(self):
        self.assertIn("CREATE UNLOGGED TABLE", ctas_sql("d", QUERY, "postgresql", unlogged=True))
        oracle = ctas_sql("d", QUERY, "oracle", nologging=True, parallel=8)
        self.assertIn("NOLOGGING", oracle)
        self.assertIn("PARALLEL 8", oracle)

    def test_options_are_rejected_on_engines_that_lack_them(self):
        with self.assertRaises(ValueError):
            ctas_sql("d", QUERY, "oracle", unlogged=True)
        with self.assertRaises(ValueError):
            ctas_sql("d", QUERY, "postgresql", nologging=True)
        with self.assertRaises(ValueError):
            ctas_sql("d", QUERY, "mysql", parallel=4)

    def test_schema_is_qualified(self):
        self.assertIn('"stage"."dump"', ctas_sql("dump", QUERY, "postgresql", schema="stage"))


class InsertSelectTests(unittest.TestCase):
    def test_plain_form(self):
        sql = insert_select_sql("dump", QUERY, "postgresql", columns=["a", "b"])
        self.assertTrue(sql.startswith('INSERT INTO "dump" ("a", "b")'))
        self.assertIn(QUERY, sql)

    def test_direct_path_hints(self):
        self.assertIn("/*+ APPEND */", insert_select_sql("d", QUERY, "oracle", direct_path=True))
        self.assertIn(
            "WITH (TABLOCK)", insert_select_sql("d", QUERY, "sqlserver", direct_path=True)
        )

    def test_direct_path_is_rejected_where_there_is_no_hint(self):
        with self.assertRaises(ValueError):
            insert_select_sql("d", QUERY, "postgresql", direct_path=True)


class DuplicateColumnTests(unittest.TestCase):
    names = ["id", "name", "id", "AMOUNT", "amount"]

    def test_repeats_are_found_case_insensitively(self):
        self.assertEqual(
            check_duplicate_columns(self.names),
            {"id": [0, 2], "AMOUNT": [3, 4]},
        )

    def test_a_clean_query_reports_nothing(self):
        self.assertEqual(check_duplicate_columns(["a", "b"]), {})

    def test_the_alias_list_keeps_every_column(self):
        select_list = aliased_select_list(self.names, "postgresql")
        self.assertIn('src."id" AS "id_2"', select_list)
        self.assertIn('src."amount" AS "amount_2"', select_list)
        self.assertEqual(select_list.count("src."), len(self.names))

    def test_unrepeated_columns_are_not_aliased(self):
        self.assertEqual(aliased_select_list(["a"], "postgresql"), 'src."a"')

    def test_rewrite_wraps_the_query_and_aliases_the_repeats(self):
        sql = rewrite_for_ctas("dump", JOIN_QUERY, self.names, "oracle", nologging=True)
        self.assertIn('CREATE TABLE "dump" NOLOGGING AS', sql)
        self.assertIn('src."id" AS "id_2"', sql)
        self.assertIn(") src", sql)


class ResultColumnsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.connection = make_source(os.path.join(self.directory, "s.db"), customers=2)

    def tearDown(self):
        self.connection.close()

    def test_probe_returns_names_without_rows(self):
        names = result_columns(self.connection, "SELECT id, name FROM customers", "sqlite")
        self.assertEqual(names, ["id", "name"])

    def test_probe_survives_a_join_with_repeated_names(self):
        names = result_columns(self.connection, JOIN_QUERY, "sqlite")
        self.assertEqual(len(names), 8)


if __name__ == "__main__":
    unittest.main()
