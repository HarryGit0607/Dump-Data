--------------------------------------------------------------------------------
-- Run this before installing anything. It answers, in order:
--
--   1. can this schema create what the install creates?
--   2. is the Partitioning option available? (it is separately licensed, and
--      decides whether you install 04_table.sql or 04a_table_no_partitioning.sql)
--   3. can this schema see all ten tables the report reads?
--   4. is anything already there under these names?
--   5. is there room for the table?
--
-- Nothing here changes anything.
--
--     sqlplus user/password@db @00_preflight.sql
--------------------------------------------------------------------------------
SET LINESIZE 200
SET PAGESIZE 200
SET FEEDBACK OFF
SET VERIFY OFF

PROMPT
PROMPT === Where am I ===============================================================
SELECT USER                                            AS SCHEMA_NAME,
       SYS_CONTEXT('USERENV','DB_NAME')                AS DATABASE_NAME,
       SYS_CONTEXT('USERENV','SERVER_HOST')            AS HOST,
       (SELECT BANNER FROM V$VERSION WHERE ROWNUM = 1) AS VERSION
  FROM DUAL;

PROMPT
PROMPT === 1. Privileges ============================================================
PROMPT (CREATE ANY CONTEXT is needed by 02 only, CREATE JOB by 06 only)
WITH needed AS (
    SELECT 'CREATE TABLE'      AS PRIVILEGE, 'install'  AS NEEDED_BY FROM DUAL UNION ALL
    SELECT 'CREATE VIEW',           'install'                        FROM DUAL UNION ALL
    SELECT 'CREATE SEQUENCE',       'install'                        FROM DUAL UNION ALL
    SELECT 'CREATE PROCEDURE',      'install'                        FROM DUAL UNION ALL
    SELECT 'CREATE ANY CONTEXT',    'script 02'                      FROM DUAL UNION ALL
    SELECT 'CREATE JOB',            'script 06'                      FROM DUAL
)
SELECT n.PRIVILEGE,
       n.NEEDED_BY,
       CASE WHEN s.PRIVILEGE IS NULL THEN '*** MISSING ***' ELSE 'ok' END AS STATUS
  FROM needed n
  LEFT JOIN SESSION_PRIVS s ON s.PRIVILEGE = n.PRIVILEGE
 ORDER BY CASE WHEN s.PRIVILEGE IS NULL THEN 0 ELSE 1 END, n.PRIVILEGE;

PROMPT
PROMPT === 2. Partitioning option ===================================================
PROMPT (this decides which table script you install)
SELECT PARAMETER,
       VALUE,
       CASE VALUE
           WHEN 'TRUE'  THEN 'install 04_table.sql  (interval partitioned)'
           ELSE              'install 04a_table_no_partitioning.sql instead'
       END AS USE_THIS
  FROM V$OPTION
 WHERE PARAMETER = 'Partitioning';

PROMPT
PROMPT === 3. Source objects the report reads =======================================
WITH sources AS (
    SELECT 'PGITH_POLICY'            AS OBJECT_NAME FROM DUAL UNION ALL
    SELECT 'PGIT_ACNT_DOC'                          FROM DUAL UNION ALL
    SELECT 'FM_COMP_ACNT_YEAR'                      FROM DUAL UNION ALL
    SELECT 'PGIM_LIVE_STATUS'                       FROM DUAL UNION ALL
    SELECT 'PGIT_BULK_ACNT_DTL'                     FROM DUAL UNION ALL
    SELECT 'PGIT_POL_RISK_ADDL_INFO'                FROM DUAL UNION ALL
    SELECT 'PGIT_POL_RISK_COVER'                    FROM DUAL UNION ALL
    SELECT 'PGIM_PRODUCT'                           FROM DUAL UNION ALL
    SELECT 'PCOM_CUSTOMER'                          FROM DUAL UNION ALL
    SELECT 'FM_DIVISION'                            FROM DUAL
)
SELECT s.OBJECT_NAME,
       NVL(a.OWNER, '-')                                          AS OWNER,
       NVL(a.OBJECT_TYPE, '-')                                    AS OBJECT_TYPE,
       CASE WHEN a.OBJECT_NAME IS NULL THEN '*** NOT VISIBLE ***' ELSE 'ok' END AS STATUS
  FROM sources s
  LEFT JOIN ALL_OBJECTS a
    ON a.OBJECT_NAME = s.OBJECT_NAME
   AND a.OBJECT_TYPE IN ('TABLE', 'VIEW', 'SYNONYM')
 ORDER BY CASE WHEN a.OBJECT_NAME IS NULL THEN 0 ELSE 1 END, s.OBJECT_NAME;

PROMPT
PROMPT === 4. Names the install will use ============================================
PROMPT (anything listed here already exists and would be replaced or clash)
SELECT OBJECT_NAME, OBJECT_TYPE, STATUS
  FROM USER_OBJECTS
 WHERE OBJECT_NAME IN ('PGIS_POLICY_DTL',
                       'PGIS_POLICY_DTL_V',
                       'PGIS_POLICY_DTL_LOG',
                       'PGIS_POLICY_DTL_LOG_SEQ',
                       'PGIS_POLICY_DTL_LOAD',
                       'PGIS_POLICY_DTL_BULK',
                       'PGIS_POLICY_DTL_CHUNK',
                       'PGIS_POLICY_DTL_CHUNKED',
                       'PGIS_POLICY_DTL_IX1')
 ORDER BY OBJECT_TYPE, OBJECT_NAME;

PROMPT
PROMPT === 5. Room in the default tablespace ========================================
PROMPT (as a rule of thumb, 54 columns of report is roughly 300-400 bytes a row,
PROMPT  so a lakh of rows a month is well under a GB a year -- but check)
SELECT t.TABLESPACE_NAME,
       ROUND(SUM(f.BYTES) / 1024 / 1024) AS FREE_MB,
       t.AUTOEXTENSIBLE
  FROM USER_USERS u
  JOIN DBA_FREE_SPACE f  ON f.TABLESPACE_NAME = u.DEFAULT_TABLESPACE
  JOIN (SELECT TABLESPACE_NAME, MAX(AUTOEXTENSIBLE) AS AUTOEXTENSIBLE
          FROM DBA_DATA_FILES GROUP BY TABLESPACE_NAME) t
    ON t.TABLESPACE_NAME = f.TABLESPACE_NAME
 GROUP BY t.TABLESPACE_NAME, t.AUTOEXTENSIBLE;
-- No access to DBA_FREE_SPACE? Ask your DBA for the same figure; the query
-- above is the only one here that needs more than your own schema.

PROMPT
PROMPT === Next ====================================================================
PROMPT   everything ok, Partitioning TRUE  ->  @install.sql
PROMPT   Partitioning FALSE                ->  edit install.sql to source
PROMPT                                         04a_table_no_partitioning.sql
PROMPT                                         in place of 04_table.sql
PROMPT

SET FEEDBACK ON
