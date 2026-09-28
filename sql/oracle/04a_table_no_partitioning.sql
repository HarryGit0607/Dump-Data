--------------------------------------------------------------------------------
-- PGIS_POLICY_DTL without partitioning.
--
-- Use this INSTEAD of 04_table.sql when 00_preflight.sql reports the
-- Partitioning option as FALSE. Partitioning is separately licensed and is not
-- available on Standard Edition, so this is not a rare case.
--
-- Everything else in the install works unchanged. The one behavioural
-- difference is how a month is cleared for a re-run:
--
--     partitioned  ->  ALTER TABLE ... TRUNCATE PARTITION   instant, no undo
--     plain        ->  DELETE ... WHERE VALUATION_DATE = ?  slower, generates undo
--
-- PGIS_POLICY_DTL_LOAD.replace_month asks the data dictionary which shape the
-- table is and does the right one, so a re-run stays safe either way -- it just
-- costs more here. The index on VALUATION_DATE is what keeps that DELETE, and
-- every per-month query, from scanning the whole table.
--------------------------------------------------------------------------------

CREATE TABLE PGIS_POLICY_DTL
NOLOGGING
AS
SELECT *
  FROM PGIS_POLICY_DTL_V
 WHERE 1 = 0;

ALTER TABLE PGIS_POLICY_DTL MODIFY (VALUATION_DATE NOT NULL);

-- Does the work partition pruning would have done.
CREATE INDEX PGIS_POLICY_DTL_IX0 ON PGIS_POLICY_DTL (VALUATION_DATE) NOLOGGING;
CREATE INDEX PGIS_POLICY_DTL_IX1 ON PGIS_POLICY_DTL (SYS_ID) NOLOGGING;

--------------------------------------------------------------------------------
-- Load history: one row per attempt, written outside the load transaction so a
-- failed run still leaves its trace.
--------------------------------------------------------------------------------
CREATE TABLE PGIS_POLICY_DTL_LOG (
    RUN_ID         NUMBER        NOT NULL,
    VALUATION_DATE DATE          NOT NULL,
    METHOD         VARCHAR2(10)  NOT NULL,
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

BEGIN
    DBMS_STATS.SET_TABLE_PREFS(USER, 'PGIS_POLICY_DTL', 'DEGREE', 'DBMS_STATS.AUTO_DEGREE');
END;
/

--------------------------------------------------------------------------------
-- Retiring old months without partitions to drop: delete in bounded chunks so a
-- year of clearing out does not become one enormous transaction.
--------------------------------------------------------------------------------
-- BEGIN
--     LOOP
--         DELETE FROM PGIS_POLICY_DTL
--          WHERE VALUATION_DATE < ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -36)
--            AND ROWNUM <= 100000;
--         EXIT WHEN SQL%ROWCOUNT = 0;
--         COMMIT;
--     END LOOP;
-- END;
-- /
