--------------------------------------------------------------------------------
-- Back the install out.
--
-- Everything except the loaded data goes without asking. Dropping
-- PGIS_POLICY_DTL itself and its load history is left commented out, because
-- those are the two things you cannot get back without re-running every month.
--
--     sqlplus user/password@db @99_uninstall.sql
--------------------------------------------------------------------------------
SET SERVEROUTPUT ON
WHENEVER SQLERROR CONTINUE

-- Stop the schedule first, so nothing starts a load while objects disappear.
BEGIN
    DBMS_SCHEDULER.DROP_JOB('PGIS_POLICY_DTL_MONTHLY', force => TRUE);
    DBMS_OUTPUT.PUT_LINE('dropped job PGIS_POLICY_DTL_MONTHLY');
EXCEPTION
    WHEN OTHERS THEN
        DBMS_OUTPUT.PUT_LINE('job not present');
END;
/

DROP PACKAGE PGIS_POLICY_DTL_BULK;
DROP PACKAGE PGIS_POLICY_DTL_LOAD;
DROP CONTEXT PGIS_RPT_CTX;
DROP VIEW PGIS_POLICY_DTL_V;

--------------------------------------------------------------------------------
-- The data. Uncomment deliberately.
--------------------------------------------------------------------------------
-- DROP TABLE PGIS_POLICY_DTL PURGE;
-- DROP TABLE PGIS_POLICY_DTL_LOG PURGE;
-- DROP SEQUENCE PGIS_POLICY_DTL_LOG_SEQ;
-- DROP TABLE ERR$_PGIS_POLICY_DTL PURGE;   -- only if you created it

PROMPT
PROMPT Code removed. PGIS_POLICY_DTL and its load history were left in place;
PROMPT uncomment the drops above if you want those gone too.
PROMPT
