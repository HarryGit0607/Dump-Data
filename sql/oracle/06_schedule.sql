--------------------------------------------------------------------------------
-- Monthly job.
--
-- Runs in the small hours of the 1st and, with no argument, loads the month
-- that has just ended (TRUNC(SYSDATE,'MM') - 1). Running after midnight rather
-- than during the last evening of the month also means late postings dated on
-- the last day are already in.
--
-- Needs CREATE JOB.
--------------------------------------------------------------------------------
BEGIN
    DBMS_SCHEDULER.CREATE_JOB(
        job_name        => 'PGIS_POLICY_DTL_MONTHLY',
        job_type        => 'PLSQL_BLOCK',
        job_action      => 'BEGIN PGIS_POLICY_DTL_LOAD.load_month(p_parallel => 8); END;',
        start_date      => SYSTIMESTAMP,
        repeat_interval => 'FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=2;BYMINUTE=0;BYSECOND=0',
        enabled         => TRUE,
        comments        => 'Monthly dump of PGIS_POLICY_DTL_V into PGIS_POLICY_DTL'
    );
END;
/

-- A month-end load is worth being told about rather than discovering later.
BEGIN
    DBMS_SCHEDULER.SET_ATTRIBUTE('PGIS_POLICY_DTL_MONTHLY', 'max_run_duration', INTERVAL '6' HOUR);
    DBMS_SCHEDULER.SET_ATTRIBUTE('PGIS_POLICY_DTL_MONTHLY', 'raise_events',
                                 DBMS_SCHEDULER.JOB_FAILED
                               + DBMS_SCHEDULER.JOB_OVER_MAX_DUR);
END;
/

--------------------------------------------------------------------------------
-- Handy while operating it
--------------------------------------------------------------------------------
-- Run it now, for last month:
--     EXEC PGIS_POLICY_DTL_LOAD.load_month;
--
-- Run it for one specific month end:
--     EXEC PGIS_POLICY_DTL_LOAD.load_month(DATE '2026-03-31');
--
-- Serially, e.g. on a busy box:
--     EXEC PGIS_POLICY_DTL_LOAD.load_month(DATE '2026-03-31', p_parallel => 1);
--
-- Is it running, and where has it got to:
--     SELECT * FROM USER_SCHEDULER_RUNNING_JOBS;
--     SELECT * FROM PGIS_POLICY_DTL_LOG ORDER BY RUN_ID DESC FETCH FIRST 10 ROWS ONLY;
--     SELECT SID, SOFAR, TOTALWORK, ELAPSED_SECONDS, TIME_REMAINING
--       FROM V$SESSION_LONGOPS WHERE TIME_REMAINING > 0;
--
-- Stop the schedule without dropping anything:
--     EXEC DBMS_SCHEDULER.DISABLE('PGIS_POLICY_DTL_MONTHLY');
