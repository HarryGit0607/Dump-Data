--------------------------------------------------------------------------------
-- PGIS_POLICY_DTL_LOAD - specification
--
-- Monthly load of the earned / unearned exposure report into PGIS_POLICY_DTL.
-- The report query itself lives in the view PGIS_POLICY_DTL_V (script 03); this
-- package supplies the valuation date it runs for and does the loading.
--
-- Run order: 01 (this) -> 02 context -> 03 view -> 04 table -> 05 body.
--------------------------------------------------------------------------------
CREATE OR REPLACE PACKAGE PGIS_POLICY_DTL_LOAD AS

    c_context CONSTANT VARCHAR2(30) := 'PGIS_RPT_CTX';
    c_table   CONSTANT VARCHAR2(30) := 'PGIS_POLICY_DTL';
    c_view    CONSTANT VARCHAR2(30) := 'PGIS_POLICY_DTL_V';

    -- Publishes the valuation date to the session so PGIS_POLICY_DTL_V can see
    -- it. Set this before selecting from the view by hand; load_month does it
    -- for you.
    PROCEDURE set_valuation_date (p_val_dt IN DATE);

    -- The valuation date the current session is reporting on, NULL if unset.
    FUNCTION valuation_date RETURN DATE;

    -- Loads one month.
    --   p_val_dt   month-end valuation date; defaults to the end of last month
    --   p_parallel degree of parallelism, 1 to run serially
    --   p_replace  TRUE re-loads the month from scratch (the usual case, and
    --              what makes a re-run safe); FALSE appends to whatever is
    --              already there
    PROCEDURE load_month (
        p_val_dt   IN DATE        DEFAULT NULL,
        p_parallel IN PLS_INTEGER DEFAULT 8,
        p_replace  IN BOOLEAN     DEFAULT TRUE
    );

    -- Loads every month end in a range, oldest first. Each month is committed
    -- on its own, so an interrupted backfill keeps the months it finished.
    PROCEDURE backfill (
        p_from_month_end IN DATE,
        p_to_month_end   IN DATE,
        p_parallel       IN PLS_INTEGER DEFAULT 8
    );

    ----------------------------------------------------------------------------
    -- Writing to PGIS_POLICY_DTL_LOG. Public so that the BULK COLLECT variant
    -- in script 09 records its runs the same way and the two can be compared
    -- on equal terms. Both commit in their own transaction, so the record of a
    -- failed run survives its rollback.
    ----------------------------------------------------------------------------
    FUNCTION log_start (p_val_dt IN DATE, p_method IN VARCHAR2) RETURN NUMBER;

    PROCEDURE log_end (
        p_run_id IN NUMBER,
        p_status IN VARCHAR2,
        p_rows   IN NUMBER   DEFAULT NULL,
        p_error  IN VARCHAR2 DEFAULT NULL
    );

END PGIS_POLICY_DTL_LOAD;
/
SHOW ERRORS
