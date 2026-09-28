--------------------------------------------------------------------------------
-- Time INSERT ... SELECT against cursor + BULK COLLECT + FORALL, on your data.
--
-- Both write the same month into the same table, one after the other, each
-- truncating the month's partition first so they start from the same place and
-- neither inherits the other's rows. Both record themselves in
-- PGIS_POLICY_DTL_LOG, which the summary at the bottom reads.
--
-- Run it on a quiet system, or the numbers measure the other load on the box.
-- Run it twice and use the second pass: the first one warms the buffer cache
-- for whichever method happens to go first, which flatters the second.
--
-- Needs 09_bulk_collect_variant.sql installed.
--
--     @10_compare_methods.sql
--------------------------------------------------------------------------------
SET SERVEROUTPUT ON SIZE UNLIMITED
SET LINESIZE 200
SET TIMING ON
SET DEFINE ON

DEFINE val_dt = "2026-03-31"

PROMPT
PROMPT === 1/3  INSERT /*+ APPEND */ ... SELECT, parallel 8 ===========================
BEGIN
    PGIS_POLICY_DTL_LOAD.load_month(
        p_val_dt   => DATE '&val_dt',
        p_parallel => 8
    );
END;
/

PROMPT
PROMPT === 2/3  BULK COLLECT LIMIT 1000 + FORALL with APPEND_VALUES ==================
BEGIN
    PGIS_POLICY_DTL_BULK.load_month_bulk(
        p_val_dt => DATE '&val_dt',
        p_limit  => 1000,
        p_direct => TRUE
    );
END;
/

PROMPT
PROMPT === 3/3  BULK COLLECT LIMIT 1000 + FORALL with SAVE EXCEPTIONS ================
PROMPT (conventional path: the tolerant form cannot also be direct path)
BEGIN
    PGIS_POLICY_DTL_BULK.load_month_bulk(
        p_val_dt => DATE '&val_dt',
        p_limit  => 1000,
        p_direct => FALSE
    );
END;
/

SET TIMING OFF

PROMPT
PROMPT === Result ===================================================================
WITH runs AS (
    SELECT RUN_ID,
           METHOD,
           STATUS,
           ROWS_LOADED,
           ROUND(EXTRACT(DAY    FROM (ENDED_AT - STARTED_AT)) * 86400
               + EXTRACT(HOUR   FROM (ENDED_AT - STARTED_AT)) * 3600
               + EXTRACT(MINUTE FROM (ENDED_AT - STARTED_AT)) * 60
               + EXTRACT(SECOND FROM (ENDED_AT - STARTED_AT)), 2) AS SECONDS
      FROM PGIS_POLICY_DTL_LOG
     WHERE VALUATION_DATE = DATE '&val_dt'
       AND ENDED_AT IS NOT NULL
     ORDER BY RUN_ID DESC
     FETCH FIRST 3 ROWS ONLY
)
SELECT METHOD,
       STATUS,
       ROWS_LOADED,
       SECONDS,
       ROUND(ROWS_LOADED / NULLIF(SECONDS, 0))                       AS ROWS_PER_SECOND,
       ROUND(SECONDS / NULLIF(MIN(SECONDS) OVER (), 0), 2) || 'x'    AS VS_FASTEST
  FROM runs
 ORDER BY SECONDS;

PROMPT
PROMPT === Where each method spent its time =========================================
PROMPT (a large 'PL/SQL lock timer' or high CPU with few physical reads on the
PROMPT  BULK runs is the SQL-to-PL/SQL round trip showing up)
SELECT SQL_ID,
       ROUND(ELAPSED_TIME / 1e6, 2)  AS ELAPSED_SECONDS,
       ROUND(CPU_TIME / 1e6, 2)      AS CPU_SECONDS,
       EXECUTIONS,
       ROWS_PROCESSED,
       SUBSTR(SQL_TEXT, 1, 70)       AS SQL_TEXT
  FROM V$SQL
 WHERE UPPER(SQL_TEXT) LIKE '%PGIS_POLICY_DTL%'
   AND UPPER(SQL_TEXT) NOT LIKE '%V$SQL%'
 ORDER BY ELAPSED_TIME DESC
 FETCH FIRST 10 ROWS ONLY;

UNDEFINE val_dt

--------------------------------------------------------------------------------
-- Leave the table holding the month loaded by the method you intend to keep.
--------------------------------------------------------------------------------
-- EXEC PGIS_POLICY_DTL_LOAD.load_month(DATE '2026-03-31');
