--------------------------------------------------------------------------------
-- PGIS_POLICY_DTL - the dump table, and the log of loads into it.
--
-- The table is created straight from the report view, so its columns are the
-- report's columns: same names, same order, same datatypes, with nothing typed
-- out by hand to drift out of step later. WHERE 1 = 0 is a constant-false
-- predicate, which Oracle prunes away entirely -- the report is not executed,
-- only described.
--
-- Interval partitioning by month on VALUATION_DATE is what makes a monthly dump
-- pleasant to live with:
--   * each run writes one partition, so a re-run truncates that month alone and
--     leaves every other month untouched -- re-running March is safe at any
--     time, and there is no DELETE of millions of rows to undo a bad load;
--   * queries for one month read one partition;
--   * old months can be dropped or moved to cheap storage one partition at a
--     time;
--   * partitions appear by themselves as new months arrive, so nothing has to
--     be created ahead of time.
--
-- NOLOGGING plus the direct-path INSERT in the load package means the load
-- generates almost no redo. The table is then unrecoverable from an archive-log
-- restore until the next backup -- which is the right trade here, because the
-- content can always be rebuilt by re-running the month. Note that a database
-- in FORCE LOGGING (usual with a physical standby) ignores NOLOGGING, so on
-- those systems the load is fully logged and correspondingly slower.
--------------------------------------------------------------------------------

CREATE TABLE PGIS_POLICY_DTL
NOLOGGING
PARTITION BY RANGE (VALUATION_DATE)
INTERVAL (NUMTOYMINTERVAL(1,'MONTH'))
(
    PARTITION P_INITIAL VALUES LESS THAN (DATE '2015-01-01')
)
AS
SELECT *
  FROM PGIS_POLICY_DTL_V
 WHERE 1 = 0;

-- A NULL valuation date would mean the context was never set, i.e. a report of
-- nothing in particular. Rejecting it here turns that mistake into an error at
-- load time instead of a silently mislabelled partition.
ALTER TABLE PGIS_POLICY_DTL MODIFY (VALUATION_DATE NOT NULL);

-- Local, so it is maintained per partition and a monthly truncate does not
-- disturb the other months.
CREATE INDEX PGIS_POLICY_DTL_IX1 ON PGIS_POLICY_DTL (SYS_ID) NOLOGGING LOCAL;

-- Only re-gather statistics for the partitions that actually changed, instead
-- of rescanning the whole growing table every month.
BEGIN
    DBMS_STATS.SET_TABLE_PREFS(USER, 'PGIS_POLICY_DTL', 'INCREMENTAL', 'TRUE');
    DBMS_STATS.SET_TABLE_PREFS(USER, 'PGIS_POLICY_DTL', 'GRANULARITY', 'AUTO');
END;
/

--------------------------------------------------------------------------------
-- Load history: one row per attempt, written outside the load transaction so a
-- failed run still leaves its trace.
--------------------------------------------------------------------------------
CREATE TABLE PGIS_POLICY_DTL_LOG (
    RUN_ID         NUMBER        NOT NULL,
    VALUATION_DATE DATE          NOT NULL,
    STATUS         VARCHAR2(10)  NOT NULL,
    STARTED_AT     TIMESTAMP     NOT NULL,
    ENDED_AT       TIMESTAMP,
    ROWS_LOADED    NUMBER,
    DB_USER        VARCHAR2(128),
    SESSION_ID     NUMBER,
    ERROR_TEXT     VARCHAR2(4000),
    CONSTRAINT PGIS_POLICY_DTL_LOG_PK PRIMARY KEY (RUN_ID)
);

CREATE INDEX PGIS_POLICY_DTL_LOG_IX1 ON PGIS_POLICY_DTL_LOG (VALUATION_DATE, STARTED_AT);

CREATE SEQUENCE PGIS_POLICY_DTL_LOG_SEQ START WITH 1 INCREMENT BY 1 NOCACHE;

--------------------------------------------------------------------------------
-- Review the DDL Oracle derived from the view before loading anything into it.
--------------------------------------------------------------------------------
-- SET LONG 200000 PAGESIZE 0
-- SELECT DBMS_METADATA.GET_DDL('TABLE','PGIS_POLICY_DTL') FROM DUAL;
