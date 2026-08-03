--------------------------------------------------------------------------------
-- PGIS_POLICY_DTL_LOAD - body
--
-- One month per call. The whole result set is written by the database itself:
-- no row ever leaves the server, which is the difference between a load that
-- takes minutes and one that takes hours because a client is fetching millions
-- of rows over SQL*Net one array at a time.
--------------------------------------------------------------------------------
CREATE OR REPLACE PACKAGE BODY PGIS_POLICY_DTL_LOAD AS

    ----------------------------------------------------------------------------
    -- Logging, in its own transaction so that the record of a failed run
    -- survives the rollback of that run.
    ----------------------------------------------------------------------------
    FUNCTION log_start (p_val_dt IN DATE, p_method IN VARCHAR2) RETURN NUMBER IS
        PRAGMA AUTONOMOUS_TRANSACTION;
        v_run_id NUMBER;
    BEGIN
        SELECT PGIS_POLICY_DTL_LOG_SEQ.NEXTVAL INTO v_run_id FROM DUAL;

        INSERT INTO PGIS_POLICY_DTL_LOG (
            RUN_ID, VALUATION_DATE, METHOD, STATUS, STARTED_AT, DB_USER, SESSION_ID
        ) VALUES (
            v_run_id, p_val_dt, p_method, 'RUNNING', SYSTIMESTAMP, USER,
            SYS_CONTEXT('USERENV','SID')
        );

        COMMIT;
        RETURN v_run_id;
    END log_start;


    PROCEDURE log_end (
        p_run_id IN NUMBER,
        p_status IN VARCHAR2,
        p_rows   IN NUMBER   DEFAULT NULL,
        p_error  IN VARCHAR2 DEFAULT NULL
    ) IS
        PRAGMA AUTONOMOUS_TRANSACTION;
    BEGIN
        UPDATE PGIS_POLICY_DTL_LOG
           SET STATUS      = p_status,
               ENDED_AT    = SYSTIMESTAMP,
               ROWS_LOADED = p_rows,
               ERROR_TEXT  = SUBSTR(p_error, 1, 4000)
         WHERE RUN_ID = p_run_id;

        COMMIT;
    END log_end;


    ----------------------------------------------------------------------------
    -- Valuation date
    ----------------------------------------------------------------------------
    PROCEDURE set_valuation_date (p_val_dt IN DATE) IS
    BEGIN
        IF p_val_dt IS NULL THEN
            RAISE_APPLICATION_ERROR(-20001, 'valuation date must not be NULL');
        END IF;

        -- Stored as YYYY-MM-DD: unambiguous, and independent of the session's
        -- NLS date settings, which a scheduler job does not inherit from you.
        DBMS_SESSION.SET_CONTEXT(c_context, 'VAL_DT', TO_CHAR(TRUNC(p_val_dt), 'YYYY-MM-DD'));
    END set_valuation_date;


    FUNCTION valuation_date RETURN DATE IS
    BEGIN
        RETURN TO_DATE(SYS_CONTEXT(c_context, 'VAL_DT'), 'YYYY-MM-DD');
    END valuation_date;


    -- End of last month: what a month-end job run on the 1st is reporting on.
    FUNCTION default_valuation_date RETURN DATE IS
    BEGIN
        RETURN TRUNC(SYSDATE, 'MM') - 1;
    END default_valuation_date;


    ----------------------------------------------------------------------------
    -- Clear one month so the load can be repeated.
    --
    -- TRUNCATE PARTITION is instantaneous and generates no undo, unlike a
    -- DELETE of the month's rows. It is only available if the table is
    -- partitioned, which needs the Partitioning option; where that is not
    -- licensed, 04a creates a plain table and this deletes the month instead.
    -- Getting this branch wrong is how a re-run silently doubles a month, so it
    -- asks the data dictionary rather than assuming.
    ----------------------------------------------------------------------------
    PROCEDURE replace_month (p_val_dt IN DATE) IS
        v_partitioned VARCHAR2(3);
    BEGIN
        SELECT PARTITIONED
          INTO v_partitioned
          FROM USER_TABLES
         WHERE TABLE_NAME = c_table;

        IF v_partitioned = 'YES' THEN
            BEGIN
                EXECUTE IMMEDIATE
                    'ALTER TABLE ' || c_table ||
                    ' TRUNCATE PARTITION FOR (DATE ''' || TO_CHAR(p_val_dt, 'YYYY-MM-DD') || ''')' ||
                    ' UPDATE INDEXES';
            EXCEPTION
                WHEN OTHERS THEN
                    -- Interval partitions only exist once something has been
                    -- stored in them, so the first load of a month finds
                    -- nothing to truncate. That is not an error.
                    --   ORA-02149  specified partition does not exist
                    --   ORA-14758  last partition in the range section
                    IF SQLCODE IN (-2149, -14758) THEN
                        NULL;
                    ELSE
                        RAISE;
                    END IF;
            END;
        ELSE
            DELETE FROM PGIS_POLICY_DTL WHERE VALUATION_DATE = p_val_dt;
            COMMIT;
        END IF;
    END replace_month;


    PROCEDURE set_parallelism (p_parallel IN PLS_INTEGER) IS
    BEGIN
        IF NVL(p_parallel, 1) > 1 THEN
            -- Parallel DML is off by default in every session and has to be
            -- asked for explicitly; without this the direct-path insert runs
            -- on a single process however parallel the query underneath is.
            EXECUTE IMMEDIATE 'ALTER SESSION ENABLE PARALLEL DML';
            EXECUTE IMMEDIATE 'ALTER SESSION FORCE PARALLEL QUERY PARALLEL ' || TO_CHAR(p_parallel);
            EXECUTE IMMEDIATE 'ALTER SESSION FORCE PARALLEL DML PARALLEL '   || TO_CHAR(p_parallel);
        ELSE
            EXECUTE IMMEDIATE 'ALTER SESSION DISABLE PARALLEL DML';
        END IF;
    END set_parallelism;


    ----------------------------------------------------------------------------
    -- Load one month.
    ----------------------------------------------------------------------------
    PROCEDURE load_month (
        p_val_dt   IN DATE        DEFAULT NULL,
        p_parallel IN PLS_INTEGER DEFAULT 8,
        p_replace  IN BOOLEAN     DEFAULT TRUE
    ) IS
        v_val_dt DATE := TRUNC(NVL(p_val_dt, default_valuation_date));
        v_run_id NUMBER;
        v_rows   NUMBER;
    BEGIN
        v_run_id := log_start(v_val_dt, 'DIRECT');

        set_valuation_date(v_val_dt);
        set_parallelism(p_parallel);

        IF p_replace THEN
            replace_month(v_val_dt);
        END IF;

        -- APPEND writes formatted blocks straight above the segment's high
        -- water mark: no search for free space, no buffer cache, and with the
        -- table NOLOGGING, almost no redo.
        --
        -- SELECT * is deliberate. The table was created from this view, so the
        -- two column lists are the same by construction; naming 54 columns here
        -- would only add a second place to keep in step. 07_verify.sql checks
        -- that they have not diverged.
        INSERT /*+ APPEND */ INTO PGIS_POLICY_DTL
        SELECT * FROM PGIS_POLICY_DTL_V;

        v_rows := SQL%ROWCOUNT;

        -- A table written by direct path cannot be read again in the same
        -- transaction (ORA-12838), so this commit is not optional.
        COMMIT;

        DBMS_STATS.GATHER_TABLE_STATS(
            ownname => USER,
            tabname => c_table,
            degree  => DBMS_STATS.AUTO_DEGREE,
            cascade => TRUE
        );

        log_end(v_run_id, 'SUCCESS', v_rows);
    EXCEPTION
        WHEN OTHERS THEN
            ROLLBACK;
            log_end(
                v_run_id,
                'FAILED',
                NULL,
                SQLERRM || CHR(10) || DBMS_UTILITY.FORMAT_ERROR_BACKTRACE
            );
            RAISE;
    END load_month;


    ----------------------------------------------------------------------------
    -- Load a range of month ends, oldest first.
    ----------------------------------------------------------------------------
    PROCEDURE backfill (
        p_from_month_end IN DATE,
        p_to_month_end   IN DATE,
        p_parallel       IN PLS_INTEGER DEFAULT 8
    ) IS
        v_val_dt DATE := TRUNC(LAST_DAY(p_from_month_end));
        v_last   DATE := TRUNC(LAST_DAY(p_to_month_end));
    BEGIN
        WHILE v_val_dt <= v_last LOOP
            load_month(v_val_dt, p_parallel, TRUE);
            v_val_dt := TRUNC(LAST_DAY(ADD_MONTHS(v_val_dt, 1)));
        END LOOP;
    END backfill;

END PGIS_POLICY_DTL_LOAD;
/
SHOW ERRORS PACKAGE BODY PGIS_POLICY_DTL_LOAD
