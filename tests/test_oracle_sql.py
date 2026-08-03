"""Static checks on the Oracle scripts.

There is no Oracle instance in CI, so these do what can be done without one:
parse the report view with an Oracle-dialect parser, confirm the column list the
table inherits, and check the shape of the PL/SQL that a parser will not read.
"""

import os
import re
import unittest

SQL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sql", "oracle")

EXPECTED_COLUMNS = [
    "VALUATION_DATE",
    "EARNED_EXPOSURE",
    "UNEARNED_EXPOSURE",
    "TOTAL",
    "EARNED_EXPOSURE_RATIO",
    "SYS_ID",
    "POL_NO",
    "PRODUCT_CODE",
    "PROD_NAME",
    "DEPT_CODE",
    "ANLY_CODE_1",
    "ANLY_CODE_2",
    "SRC_CODE",
    "SOURCE_NAME",
    "SRC_TYP",
    "SOURCE_TYPE",
    "BUS_TYPE",
    "BUS_TYPE_VALUES",
    "INSTALLMENT_FLAG",
    "INSTALLMENT_METHOD",
    "INSTALLMENT_METHOD_VALUE",
    "UNDERWRITING_YEAR",
    "ACCOUNTING_YEAR",
    "TPA_CODE",
    "TPA_NAME",
    "DIV_CODE",
    "DIV_NAME",
    "POL_FM_DATE",
    "POL_TO_DATE",
    "TERM_OF_POLICY",
    "ASSR_CODE",
    "ASSURED_NAME",
    "CURR_MONTH_COMMISION",
    "CUM_EARNED_COMMISION",
    "CUM_UNEARNED_COMMISION",
    "TOTAL_COMMISION",
    "CURR_MONTH_TPA_COMMISION",
    "CUM_EARNED_TPA_COMMISSION",
    "CUM_UNEARNED_TPA_COMMISSION",
    "TOTAL_TPA_COMMISION",
    "UNEARNED_PREM_YEAR_1",
    "UNEARNED_PREM_YEAR_2",
    "UNEARNED_PREM_YEAR_3",
    "UNEARNED_PREM_YEAR_4_PLUS",
    "CURR_MONTH_PREM_EP",
    "CURR_FY_EP",
    "CUM_EARNED_PREMIUM",
    "CUM_UNEARNED_PREMIUM_TOTAL",
    "TOTAL_PREMIUM",
    "LONG_TERM_PREMIUM",
    "ADVANCE_PREMIUM",
    "EP_CUM_EXPECTED_PREM",
    "CPA_FLAG",
    "PA_PREMIUM",
]


def read(name):
    with open(os.path.join(SQL_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def strip_comments(text):
    """Drop comments but keep optimizer hints, which are comments that matter."""
    text = re.sub(r"/\*(?!\+).*?\*/", " ", text, flags=re.S)
    return re.sub(r"--[^\n]*", "", text)


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("03_report_view.sql")
        start = cls.text.index("CREATE OR REPLACE VIEW")
        cls.statement = cls.text[start : cls.text.index("SHOW ERRORS")].strip()

    def test_it_parses_as_oracle(self):
        import sqlglot

        parsed = sqlglot.parse_one(self.statement.rstrip(";"), dialect="oracle")
        self.assertEqual(parsed.key, "create")

    def test_the_view_produces_the_reports_54_columns_in_order(self):
        import sqlglot

        parsed = sqlglot.parse_one(self.statement.rstrip(";"), dialect="oracle")
        select = parsed.expression
        while select.key != "select":
            select = select.this
        names = [projection.alias_or_name for projection in select.expressions]
        self.assertEqual(names, EXPECTED_COLUMNS)

    def test_no_repeated_column_name_would_break_the_ctas(self):
        folded = [name.casefold() for name in EXPECTED_COLUMNS]
        self.assertEqual(len(set(folded)), len(folded))

    def test_every_name_fits_the_pre_12_2_identifier_limit(self):
        self.assertEqual([name for name in EXPECTED_COLUMNS if len(name) > 30], [])

    def test_the_valuation_date_comes_from_the_context_everywhere(self):
        body = strip_comments(self.statement)
        self.assertNotIn("31-MAR-2026", body)
        self.assertNotIn("DD-MON-YYYY", body)
        self.assertEqual(body.count("SYS_CONTEXT('PGIS_RPT_CTX','VAL_DT')"), 3)

    def test_the_tuned_shape_of_the_query_is_intact(self):
        body = strip_comments(self.statement)
        self.assertIn("/*+ MATERIALIZE */", self.statement)
        self.assertIn("NOT EXISTS", body)
        self.assertNotIn("MAX(P2.POLH_END_NO_IDX)", body.upper())
        for cte in (
            "policy_agg",
            "policy_days",
            "policy_keys",
            "commission_by_cover",
            "detail",
            "detail_keys",
            "cpa_flag",
            "pa_premium",
        ):
            self.assertIn(f"{cte} AS (", body)

    def test_the_deliberate_oddities_survived_the_move_into_a_view(self):
        # These are the things most likely to be tidied up by accident, and the
        # ones that would change the numbers if they were.
        body = strip_comments(self.statement)
        self.assertIn("NULLIF(FLOOR(d.POLH_TO_DT - d.POLH_FM_DT + 1), 4)", body)
        self.assertEqual(body.count("WHEN d.POLH_BUS_TYPE = '1' THEN 'Direct without coinsurance'"), 2)
        self.assertEqual(body.count("SUM(d.EARNED_EXPOSURE) <= '0'"), 5)
        self.assertEqual(body.count("SUM(d.EARNED_EXPOSURE) <= 0"), 2)
        self.assertIn("(p.VAL_DT - p.POLH_FM_DT + 2)", body)
        self.assertIn("* -(p.POLH_FM_DT - ((p.VAL_DT + 2)))", body)

    def test_sqlplus_will_not_cut_the_statement_short(self):
        # A bare ';' inside the statement, or (with SQLBLANKLINES off) a blank
        # line, ends the buffer early and Oracle sees a truncated view.
        lines = self.statement.splitlines()
        self.assertEqual([n for n, line in enumerate(lines, 1) if not line.strip()], [])
        code = strip_comments(self.statement)
        self.assertEqual(code.count(";"), 1)


class EquivalenceTests(unittest.TestCase):
    """The view must be the handed-over query, with only the date swapped out.

    Put the literal back where the context lookup is, drop comments and
    whitespace, and the two have to be the same text. Anything else means a
    formula moved while the query was being wrapped in a view.
    """

    context_expression = "TO_DATE(SYS_CONTEXT('PGIS_RPT_CTX','VAL_DT'),'YYYY-MM-DD')"
    literal_expression = "TO_DATE('31-MAR-2026','DD-MON-YYYY')"

    @staticmethod
    def normalise(sql):
        return re.sub(r"\s+", " ", strip_comments(sql)).strip().rstrip(";").strip()

    def test_the_view_is_the_original_query_with_only_the_date_replaced(self):
        original = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "original_report_query.sql"
        )
        with open(original, encoding="utf-8") as handle:
            expected = self.normalise(handle.read())

        view = read("03_report_view.sql")
        statement = view[view.index("CREATE OR REPLACE VIEW") : view.index("SHOW ERRORS")]
        statement = statement[statement.index(" AS\n") + 4 :]
        actual = self.normalise(statement).replace(
            self.context_expression, self.literal_expression
        )

        self.assertEqual(actual, expected)


class TableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("04_table.sql")

    def test_the_table_is_built_from_the_view_without_running_it(self):
        self.assertIn("FROM PGIS_POLICY_DTL_V", self.text)
        self.assertIn("WHERE 1 = 0", self.text)

    def test_it_is_interval_partitioned_by_valuation_month(self):
        self.assertIn("PARTITION BY RANGE (VALUATION_DATE)", self.text)
        self.assertIn("INTERVAL (NUMTOYMINTERVAL(1,'MONTH'))", self.text)

    def test_the_partition_key_cannot_be_null(self):
        self.assertIn("MODIFY (VALUATION_DATE NOT NULL)", self.text)

    def test_indexes_on_a_partitioned_table_are_local(self):
        for statement in re.findall(r"CREATE INDEX .*?;", self.text, flags=re.S):
            if "PGIS_POLICY_DTL_LOG" in statement:
                continue
            self.assertIn("LOCAL", statement)

    def test_statistics_are_incremental(self):
        self.assertIn("'INCREMENTAL', 'TRUE'", self.text)

    def test_the_log_table_and_its_sequence_exist(self):
        self.assertIn("CREATE TABLE PGIS_POLICY_DTL_LOG", self.text)
        self.assertIn("CREATE SEQUENCE PGIS_POLICY_DTL_LOG_SEQ", self.text)


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = read("01_package_spec.sql")
        cls.body = read("05_package_body.sql")

    def test_every_declared_procedure_is_implemented(self):
        declared = set(
            re.findall(r"^\s*(?:PROCEDURE|FUNCTION)\s+(\w+)", strip_comments(self.spec), re.M)
        )
        implemented = set(
            re.findall(r"^\s*(?:PROCEDURE|FUNCTION)\s+(\w+)", strip_comments(self.body), re.M)
        )
        self.assertEqual(
            declared,
            {
                "set_valuation_date",
                "valuation_date",
                "replace_month",
                "load_month",
                "backfill",
                "log_start",
                "log_end",
            },
        )
        self.assertTrue(declared <= implemented, declared - implemented)

    def test_each_program_unit_is_closed(self):
        code = strip_comments(self.body)
        names = re.findall(r"^\s*(?:PROCEDURE|FUNCTION)\s+(\w+)", code, re.M)
        for name in names:
            self.assertRegex(code, rf"END {name};", f"{name} is not closed by name")

    def test_the_insert_is_direct_path_and_server_side(self):
        code = strip_comments(self.body)
        self.assertIn("INSERT /*+ APPEND */ INTO PGIS_POLICY_DTL", self.body)
        self.assertIn("SELECT * FROM PGIS_POLICY_DTL_V", code)

    def test_parallel_dml_is_enabled_before_the_insert(self):
        code = strip_comments(self.body)
        self.assertIn("ALTER SESSION ENABLE PARALLEL DML", code)
        self.assertLess(code.index("set_parallelism(p_parallel)"), code.index("INSERT /*+ APPEND */"))

    def test_direct_path_is_committed_before_the_table_is_read_again(self):
        code = strip_comments(self.body)
        insert_at = code.index("INSERT /*+ APPEND */")
        commit_at = code.index("COMMIT;", insert_at)
        stats_at = code.index("GATHER_TABLE_STATS")
        self.assertLess(commit_at, stats_at)

    def test_a_rerun_replaces_only_its_own_month(self):
        code = strip_comments(self.body)
        self.assertIn("TRUNCATE PARTITION FOR (DATE ", code)
        # The unpartitioned fallback deletes, but only ever one month of it.
        for statement in re.findall(r"DELETE FROM[^;]*;", code):
            self.assertIn("WHERE VALUATION_DATE = p_val_dt", statement)

    def test_a_first_load_of_a_month_tolerates_the_missing_partition(self):
        self.assertIn("SQLCODE IN (-2149, -14758)", self.body)

    def test_the_log_survives_a_failed_load(self):
        code = strip_comments(self.body)
        self.assertEqual(code.count("PRAGMA AUTONOMOUS_TRANSACTION;"), 2)
        failure = code.index("'FAILED'")
        self.assertIn("ROLLBACK;", code[:failure])
        self.assertIn("RAISE;", code[failure:])

    def test_the_valuation_date_is_stored_without_nls_dependence(self):
        self.assertIn("'YYYY-MM-DD'", self.body)
        self.assertNotIn("DD-MON-YYYY", self.body)

    def test_the_default_month_is_the_one_that_just_ended(self):
        self.assertIn("TRUNC(SYSDATE, 'MM') - 1", self.body)


class BulkVariantTests(unittest.TestCase):
    """The BULK COLLECT alternative exists to be benchmarked, so it has to be a
    fair implementation of the idea rather than a straw man."""

    @classmethod
    def setUpClass(cls):
        cls.text = read("09_bulk_collect_variant.sql")
        cls.code = strip_comments(cls.text)

    def test_it_fetches_in_bounded_arrays(self):
        # An unbounded BULK COLLECT would pull every row into the PGA at once,
        # which is the classic way to make this comparison meaningless.
        self.assertIn("BULK COLLECT INTO v_rows LIMIT p_limit", self.code)
        self.assertIn("p_limit        IN PLS_INTEGER DEFAULT 1000", self.code)

    def test_it_uses_forall_rather_than_a_row_by_row_loop(self):
        self.assertIn("FORALL i IN 1 .. v_rows.COUNT", self.code)
        self.assertNotIn("FOR i IN 1 .. v_rows.COUNT LOOP", self.code)

    def test_the_direct_path_form_uses_append_values(self):
        self.assertIn("INSERT /*+ APPEND_VALUES */ INTO PGIS_POLICY_DTL", self.text)

    def test_append_values_and_save_exceptions_are_kept_apart(self):
        # Together they raise ORA-38910, so they have to be separate branches.
        statements = re.findall(r"FORALL[^;]*;", self.code, re.S)
        self.assertEqual(len(statements), 2)
        direct = [s for s in statements if "APPEND_VALUES" in s]
        tolerant = [s for s in statements if "SAVE EXCEPTIONS" in s]
        self.assertEqual(len(direct), 1)
        self.assertEqual(len(tolerant), 1)
        self.assertNotIn("SAVE EXCEPTIONS", direct[0])
        self.assertNotIn("APPEND_VALUES", tolerant[0])
        self.assertIn("ELSE", self.code[self.code.index(direct[0]) : self.code.index(tolerant[0])])

    def test_rejected_rows_are_counted_per_array_not_cumulatively(self):
        self.assertIn("v_bad := SQL%BULK_EXCEPTIONS.COUNT;", self.code)
        self.assertIn("v_rejects := v_rejects + v_bad;", self.code)
        self.assertIn("v_total   := v_total + v_rows.COUNT - v_bad;", self.code)

    def test_the_cursor_is_closed_on_both_paths(self):
        self.assertEqual(self.code.count("CLOSE v_cursor;"), 2)
        self.assertIn("IF v_cursor%ISOPEN THEN", self.code)

    def test_both_methods_log_to_the_same_history(self):
        self.assertIn("PGIS_POLICY_DTL_LOAD.log_start(v_val_dt, 'BULK')", self.code)
        self.assertIn("PGIS_POLICY_DTL_LOAD.log_end(", self.code)

    def test_the_honest_alternatives_are_named(self):
        for pointer in ("LOG ERRORS INTO", "DBMS_ERRLOG.CREATE_ERROR_LOG", "DBMS_PARALLEL_EXECUTE"):
            self.assertIn(pointer, self.text)


class ComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("10_compare_methods.sql")

    def test_it_times_all_three_shapes(self):
        self.assertIn("PGIS_POLICY_DTL_LOAD.load_month(", self.text)
        self.assertEqual(self.text.count("PGIS_POLICY_DTL_BULK.load_month_bulk("), 2)
        self.assertIn("p_direct => TRUE", self.text)
        self.assertIn("p_direct => FALSE", self.text)

    def test_substitution_variables_will_actually_substitute(self):
        # install.sql leaves DEFINE OFF, which would pass '&val_dt' through
        # to Oracle verbatim.
        self.assertIn("SET DEFINE ON", self.text)
        self.assertLess(self.text.index("SET DEFINE ON"), self.text.index("&val_dt"))

    def test_each_run_starts_from_the_same_state(self):
        # p_replace defaults to TRUE, so each call truncates the month first.
        self.assertNotIn("p_replace => FALSE", self.text)


class LogTests(unittest.TestCase):
    def test_the_log_distinguishes_the_two_methods(self):
        self.assertIn("METHOD         VARCHAR2(10)  NOT NULL", read("04_table.sql"))
        self.assertIn("v_run_id := log_start(v_val_dt, 'DIRECT');", read("05_package_body.sql"))
        self.assertIn("METHOD,", read("07_verify.sql"))

    def test_the_logging_api_is_public_so_both_packages_can_use_it(self):
        spec = strip_comments(read("01_package_spec.sql"))
        self.assertIn("FUNCTION log_start (p_val_dt IN DATE, p_method IN VARCHAR2) RETURN NUMBER;", spec)
        self.assertIn("PROCEDURE log_end (", spec)


class ReplaceMonthTests(unittest.TestCase):
    """Clearing a month is the one place a wrong branch silently doubles data."""

    @classmethod
    def setUpClass(cls):
        cls.body = strip_comments(read("05_package_body.sql"))

    def test_it_asks_whether_the_table_is_partitioned(self):
        self.assertIn("SELECT PARTITIONED", self.body)
        self.assertIn("FROM USER_TABLES", self.body)

    def test_partitioned_tables_truncate_and_plain_tables_delete(self):
        start = self.body.index("PROCEDURE replace_month")
        end = self.body.index("END replace_month;")
        procedure = self.body[start:end]
        self.assertIn("IF v_partitioned = 'YES' THEN", procedure)
        self.assertIn("TRUNCATE PARTITION FOR (DATE ", procedure)
        self.assertIn("DELETE FROM PGIS_POLICY_DTL WHERE VALUATION_DATE = p_val_dt;", procedure)

    def test_not_partitioned_is_no_longer_swallowed_as_a_missing_partition(self):
        # ORA-14501 used to be ignored here, which on a plain table meant the
        # month was never cleared and a re-run appended a second copy.
        self.assertIn("SQLCODE IN (-2149, -14758)", self.body)
        self.assertNotIn("-14501", self.body)

    def test_the_bulk_variant_shares_it_rather_than_copying_it(self):
        bulk = strip_comments(read("09_bulk_collect_variant.sql"))
        self.assertIn("PGIS_POLICY_DTL_LOAD.replace_month(v_val_dt)", bulk)
        self.assertNotIn("PROCEDURE replace_month", bulk)


class PreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("00_preflight.sql")

    def test_it_checks_the_partitioning_option(self):
        self.assertIn("V$OPTION", self.text)
        self.assertIn("'Partitioning'", self.text)
        self.assertIn("04a_table_no_partitioning.sql", self.text)

    def test_it_checks_every_privilege_the_install_needs(self):
        for privilege in (
            "CREATE TABLE",
            "CREATE VIEW",
            "CREATE SEQUENCE",
            "CREATE PROCEDURE",
            "CREATE ANY CONTEXT",
            "CREATE JOB",
        ):
            self.assertIn(f"'{privilege}'", self.text)

    def test_it_checks_every_source_object_the_report_reads(self):
        view = read("03_report_view.sql")
        sources = set(re.findall(r"(?:FROM|JOIN)\s+([A-Z][A-Z0-9_]{4,})\b", strip_comments(view)))
        sources -= {"DUAL"}
        # The CTE names are lower case in the view, so what is left is real tables.
        for source in sources:
            self.assertIn(f"'{source}'", self.text, f"{source} is not pre-flighted")

    def test_it_changes_nothing(self):
        forbidden = ("CREATE ", "DROP ", "ALTER ", "INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ")
        code = strip_comments(self.text)
        for statement in forbidden:
            for line in code.splitlines():
                stripped = line.strip().upper()
                if stripped.startswith(statement):
                    self.fail(f"pre-flight would modify something: {line.strip()}")


class NoPartitioningVariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("04a_table_no_partitioning.sql")
        cls.partitioned = read("04_table.sql")

    def test_it_creates_the_same_table_without_partitions(self):
        self.assertIn("FROM PGIS_POLICY_DTL_V", self.text)
        self.assertIn("WHERE 1 = 0", self.text)
        self.assertNotIn("PARTITION BY", self.text)

    def test_it_indexes_the_valuation_date_that_pruning_would_have_handled(self):
        self.assertIn("ON PGIS_POLICY_DTL (VALUATION_DATE)", self.text)

    def test_it_provides_the_same_log_objects_as_the_partitioned_version(self):
        for obj in (
            "CREATE TABLE PGIS_POLICY_DTL_LOG",
            "CREATE SEQUENCE PGIS_POLICY_DTL_LOG_SEQ",
            "CREATE INDEX PGIS_POLICY_DTL_LOG_IX1",
        ):
            self.assertIn(obj, self.text)
            self.assertIn(obj, self.partitioned)

    def test_the_two_log_tables_have_identical_columns(self):
        def columns(text):
            start = text.index("CREATE TABLE PGIS_POLICY_DTL_LOG")
            block = text[start : text.index(";", start)]
            return re.findall(r"^\s{4}(\w+)\s+\w", block, re.M)

        self.assertEqual(columns(self.text), columns(self.partitioned))
        self.assertIn("METHOD", columns(self.text))


class UninstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("99_uninstall.sql")

    def test_the_code_objects_are_dropped(self):
        for statement in (
            "DROP PACKAGE PGIS_POLICY_DTL_LOAD;",
            "DROP VIEW PGIS_POLICY_DTL_V;",
            "DROP CONTEXT PGIS_RPT_CTX;",
        ):
            self.assertIn(statement, self.text)

    def test_the_job_is_stopped_before_anything_is_dropped(self):
        self.assertLess(self.text.index("DROP_JOB"), self.text.index("DROP PACKAGE"))

    def test_the_data_is_not_dropped_without_a_deliberate_edit(self):
        code = strip_comments(self.text)
        self.assertNotIn("DROP TABLE PGIS_POLICY_DTL", code)
        self.assertIn("-- DROP TABLE PGIS_POLICY_DTL PURGE;", self.text)


class InstallTests(unittest.TestCase):
    def test_scripts_are_sourced_in_dependency_order(self):
        text = read("install.sql")
        order = re.findall(r"@@(\d+_\w+\.sql)", text)
        self.assertEqual(
            order,
            [
                "01_package_spec.sql",
                "02_context.sql",
                "03_report_view.sql",
                "04_table.sql",
                "05_package_body.sql",
            ],
        )

    def test_sqlplus_is_configured_not_to_mangle_the_scripts(self):
        text = read("install.sql")
        self.assertIn("SET SQLBLANKLINES ON", text)
        self.assertIn("SET DEFINE OFF", text)
        self.assertIn("WHENEVER SQLERROR EXIT SQL.SQLCODE", text)

    def test_the_job_and_the_backfill_are_not_run_by_the_installer(self):
        text = read("install.sql")
        self.assertNotIn("@@06", text)
        self.assertNotIn("@@07", text)
        self.assertNotIn("@@08", text)


class PlSqlBlockTests(unittest.TestCase):
    def test_every_anonymous_block_is_terminated_with_a_slash(self):
        for name in ("04_table.sql", "05_package_body.sql", "06_schedule.sql", "08_backfill.sql"):
            text = strip_comments(read(name))
            blocks = len(re.findall(r"^BEGIN$", text, re.M))
            if not blocks:
                continue
            self.assertGreaterEqual(
                len(re.findall(r"^/$", text, re.M)), 1, f"{name} has a block but no /"
            )


if __name__ == "__main__":
    unittest.main()
