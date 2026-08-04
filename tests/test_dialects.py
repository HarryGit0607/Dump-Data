import unittest

from support import *  # noqa: F401,F403  (path setup)

from dumpdata.dialects import (
    LOGICAL_TYPES,
    MYSQL,
    ORACLE,
    POSTGRESQL,
    SQLITE,
    SQLSERVER,
    dialect_names,
    get_dialect,
)


class DialectTests(unittest.TestCase):
    def test_every_dialect_maps_every_logical_type(self):
        for dialect in (SQLITE, POSTGRESQL, MYSQL, ORACLE, SQLSERVER):
            for logical in LOGICAL_TYPES:
                self.assertIn(logical, dialect.type_map, f"{dialect.name} misses {logical}")

    def test_lookup_by_alias_is_case_insensitive(self):
        self.assertIs(get_dialect("PG"), POSTGRESQL)
        self.assertIs(get_dialect(" MsSql "), SQLSERVER)
        self.assertIs(get_dialect(ORACLE), ORACLE)

    def test_unknown_dialect_lists_the_known_ones(self):
        with self.assertRaises(ValueError) as caught:
            get_dialect("db2")
        self.assertIn("oracle", str(caught.exception))
        self.assertIn("db2", str(caught.exception))

    def test_placeholders_follow_the_paramstyle(self):
        self.assertEqual(SQLITE.placeholders(3), "?, ?, ?")
        self.assertEqual(POSTGRESQL.placeholders(2), "%s, %s")
        self.assertEqual(ORACLE.placeholders(3), ":1, :2, :3")
        self.assertEqual(ORACLE.placeholder(4), ":5")

    def test_row_limits_use_each_engine_syntax(self):
        self.assertTrue(POSTGRESQL.apply_limit("SELECT 1", 10).endswith("LIMIT 10"))
        self.assertTrue(ORACLE.apply_limit("SELECT 1", 10).endswith("FETCH FIRST 10 ROWS ONLY"))
        self.assertTrue(
            SQLSERVER.apply_limit("SELECT 1", 10).endswith("OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY")
        )
        with self.assertRaises(ValueError):
            POSTGRESQL.apply_limit("SELECT 1", 0)

    def test_identifier_quoting_escapes_the_closing_quote(self):
        self.assertEqual(POSTGRESQL.quote('we"ird'), '"we""ird"')
        self.assertEqual(MYSQL.quote("we`ird"), "`we``ird`")
        self.assertEqual(SQLSERVER.quote("we]ird"), "[we]]ird]")

    def test_qualify_adds_the_schema_when_given(self):
        self.assertEqual(POSTGRESQL.qualify("t", "s"), '"s"."t"')
        self.assertEqual(POSTGRESQL.qualify("t"), '"t"')

    def test_dialect_names_are_sorted(self):
        self.assertEqual(list(dialect_names()), sorted(dialect_names()))


if __name__ == "__main__":
    unittest.main()
