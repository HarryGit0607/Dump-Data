--------------------------------------------------------------------------------
-- Checks to run after a load, and while operating the monthly dump.
--------------------------------------------------------------------------------
SET LINESIZE 200
SET PAGESIZE 100

PROMPT
PROMPT === Load history ==============================================================
SELECT RUN_ID,
       TO_CHAR(VALUATION_DATE, 'YYYY-MM-DD')                       AS VAL_DT,
       METHOD,
       STATUS,
       ROWS_LOADED,
       TO_CHAR(STARTED_AT, 'YYYY-MM-DD HH24:MI:SS')                AS STARTED,
       ROUND(EXTRACT(DAY    FROM (ENDED_AT - STARTED_AT)) * 1440
           + EXTRACT(HOUR   FROM (ENDED_AT - STARTED_AT)) * 60
           + EXTRACT(MINUTE FROM (ENDED_AT - STARTED_AT))
           + EXTRACT(SECOND FROM (ENDED_AT - STARTED_AT)) / 60, 1) AS MINUTES,
       SUBSTR(ERROR_TEXT, 1, 60)                                   AS ERROR_TEXT
  FROM PGIS_POLICY_DTL_LOG
 ORDER BY RUN_ID DESC
 FETCH FIRST 24 ROWS ONLY;

PROMPT
PROMPT === Rows and premium per month, with the change on the month before ===========
SELECT TO_CHAR(VALUATION_DATE, 'YYYY-MM')                                    AS MONTH,
       COUNT(*)                                                              AS ROWS_LOADED,
       COUNT(DISTINCT SYS_ID)                                                AS POLICIES,
       ROUND(SUM(TOTAL_PREMIUM), 2)                                          AS TOTAL_PREMIUM,
       ROUND(SUM(CUM_EARNED_PREMIUM), 2)                                     AS EARNED_PREMIUM,
       ROUND(100 * (COUNT(*) - LAG(COUNT(*)) OVER (ORDER BY TRUNC(VALUATION_DATE, 'MM')))
             / NULLIF(LAG(COUNT(*)) OVER (ORDER BY TRUNC(VALUATION_DATE, 'MM')), 0), 1)
                                                                             AS PCT_ROW_CHANGE
  FROM PGIS_POLICY_DTL
 GROUP BY TRUNC(VALUATION_DATE, 'MM'), TO_CHAR(VALUATION_DATE, 'YYYY-MM')
 ORDER BY 1;

PROMPT
PROMPT === One partition per month, and nothing stranded in P_INITIAL ================
SELECT PARTITION_NAME,
       NUM_ROWS,
       TO_CHAR(LAST_ANALYZED, 'YYYY-MM-DD HH24:MI') AS LAST_ANALYZED
  FROM USER_TAB_PARTITIONS
 WHERE TABLE_NAME = 'PGIS_POLICY_DTL'
 ORDER BY PARTITION_POSITION;

PROMPT
PROMPT === A month must appear exactly once: no duplicated load =====================
PROMPT (expects no rows)
SELECT VALUATION_DATE, SYS_ID, ANLY_CODE_1, ANLY_CODE_2, COUNT(*) AS COPIES
  FROM PGIS_POLICY_DTL
 GROUP BY VALUATION_DATE, SYS_ID, ANLY_CODE_1, ANLY_CODE_2
HAVING COUNT(*) > 1
 ORDER BY COPIES DESC
 FETCH FIRST 20 ROWS ONLY;

PROMPT
PROMPT === Table and view still have the same columns ================================
PROMPT (expects no rows; a mismatch means the view changed and the table did not)
SELECT NVL(t.COLUMN_NAME, v.COLUMN_NAME) AS COLUMN_NAME,
       t.COLUMN_ID   AS TABLE_POSITION,
       v.COLUMN_ID   AS VIEW_POSITION,
       t.DATA_TYPE   AS TABLE_TYPE,
       v.DATA_TYPE   AS VIEW_TYPE
  FROM (SELECT COLUMN_NAME, COLUMN_ID, DATA_TYPE
          FROM USER_TAB_COLUMNS WHERE TABLE_NAME = 'PGIS_POLICY_DTL') t
  FULL JOIN
       (SELECT COLUMN_NAME, COLUMN_ID, DATA_TYPE
          FROM USER_TAB_COLUMNS WHERE TABLE_NAME = 'PGIS_POLICY_DTL_V') v
    ON t.COLUMN_NAME = v.COLUMN_NAME
 WHERE t.COLUMN_NAME IS NULL
    OR v.COLUMN_NAME IS NULL
    OR t.COLUMN_ID  <> v.COLUMN_ID
    OR t.DATA_TYPE  <> v.DATA_TYPE
 ORDER BY NVL(t.COLUMN_ID, v.COLUMN_ID);

PROMPT
PROMPT === Reconcile one stored month against a fresh run of the report ==============
PROMPT (set the date below first; expects a single row of zeros)
-- EXEC PGIS_POLICY_DTL_LOAD.set_valuation_date(DATE '2026-03-31');
--
-- WITH stored AS (
--     SELECT COUNT(*) AS ROWS_STORED, SUM(TOTAL_PREMIUM) AS PREMIUM_STORED
--       FROM PGIS_POLICY_DTL
--      WHERE VALUATION_DATE = DATE '2026-03-31'
-- ),
-- live AS (
--     SELECT COUNT(*) AS ROWS_LIVE, SUM(TOTAL_PREMIUM) AS PREMIUM_LIVE
--       FROM PGIS_POLICY_DTL_V
-- )
-- SELECT stored.ROWS_STORED - live.ROWS_LIVE          AS ROW_DIFFERENCE,
--        stored.PREMIUM_STORED - live.PREMIUM_LIVE    AS PREMIUM_DIFFERENCE
--   FROM stored CROSS JOIN live;
