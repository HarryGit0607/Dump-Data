--------------------------------------------------------------------------------
-- PGIS_POLICY_DTL_CHUNKED - a restartable load.
--
-- Cursor + BULK COLLECT ... LIMIT + FORALL, as asked for, but with the piece
-- that makes it survive an interruption. BULK COLLECT with a LIMIT bounds
-- *memory*; on its own it does not bound *loss*. If the connection drops at
-- array 900 of 1000, the cursor dies with it, and the next run re-opens the
-- same cursor and recomputes the whole report from the beginning -- with no
-- record of what was already inserted, so a re-run either duplicates rows or
-- starts again from nothing.
--
-- What makes a load resumable is a checkpoint: a durable note of what is
-- already done, written where a lost session cannot take it with it. So the
-- month is cut into chunks, each chunk is its own transaction, and each chunk
-- records itself in PGIS_POLICY_DTL_CHUNK as it completes. A restart looks at
-- that table and does only what is left.
--
--     chunk 0..n     |###|###|###|###| . | . | . | . |
--                      done          ^ interrupted here
--     restart resumes -------------- ^
--
-- The month is sliced by ORA_HASH(POLH_SYS_ID), which is safe because every row
-- of the report depends on exactly one policy: POLH_SYS_ID is in the outer
-- GROUP BY, every CTE is keyed by it, the latest-endorsement NOT EXISTS
-- correlates within it, and the remaining subqueries are constants. The chunks
-- therefore add up to precisely the whole report, no more and no less.
--
-- What it costs: each chunk re-reads the driving tables, so twelve chunks means
-- roughly twelve passes over PGITH_POLICY and PGIT_ACNT_DOC, each joining a
-- twelfth of the rows. The exchange is a longer total run for a load you never
-- have to start over. Size the chunks so one takes 10-20 minutes -- long enough
-- that the per-chunk overhead is noise, short enough that losing one does not
-- hurt.
--
-- Before reaching for this: if the worry is specifically that a *client
-- connection* drops, 06_schedule.sql already answers it. A DBMS_SCHEDULER job
-- runs inside the database, with no session to lose; you can close SQL*Plus, go
-- home, and it carries on. Chunking earns its keep against a different problem
-- -- losing two hours of work to a failure at minute 110, and holding undo and
-- temp for one enormous transaction.
--
-- Needs 01-05 installed first.
--------------------------------------------------------------------------------

CREATE TABLE PGIS_POLICY_DTL_CHUNK (
    VALUATION_DATE DATE          NOT NULL,
    CHUNK_NO       NUMBER        NOT NULL,
    CHUNK_COUNT    NUMBER        NOT NULL,
    STATUS         VARCHAR2(10)  NOT NULL,
    ROWS_LOADED    NUMBER,
    ARRAYS         NUMBER,
    STARTED_AT     TIMESTAMP,
    ENDED_AT       TIMESTAMP,
    ERROR_TEXT     VARCHAR2(4000),
    CONSTRAINT PGIS_POLICY_DTL_CHUNK_PK PRIMARY KEY (VALUATION_DATE, CHUNK_NO),
    CONSTRAINT PGIS_POLICY_DTL_CHUNK_CK CHECK (STATUS IN ('PENDING','RUNNING','DONE','FAILED'))
);


CREATE OR REPLACE PACKAGE PGIS_POLICY_DTL_CHUNKED AS

    ----------------------------------------------------------------------------
    -- Load one month in chunks, resuming if there is anything to resume.
    --
    --   p_val_dt    month-end valuation date, defaults to the month just ended
    --   p_chunks    how many slices to cut the month into. Ignored when
    --               resuming: the slices have to be the same ones, or the hash
    --               buckets move and rows are loaded twice or not at all
    --   p_limit     rows per BULK COLLECT fetch. 50000 rows of this report is
    --               roughly 15 MB of PGA per fetch; drop it if PGA is tight
    --   p_restart   TRUE throws away any existing progress for the month and
    --               starts it again from empty
    --
    -- Safe to call repeatedly. Each call does whatever is still outstanding, so
    -- "run it again" is the answer to almost everything that goes wrong.
    ----------------------------------------------------------------------------
    PROCEDURE load_month_chunked (
        p_val_dt  IN DATE        DEFAULT NULL,
        p_chunks  IN PLS_INTEGER DEFAULT 12,
        p_limit   IN PLS_INTEGER DEFAULT 50000,
        p_restart IN BOOLEAN     DEFAULT FALSE
    );

    -- One line per chunk: what is done, what is left, what failed.
    PROCEDURE show_progress (p_val_dt IN DATE DEFAULT NULL);

    -- TRUE when every chunk of the month is DONE.
    FUNCTION is_complete (p_val_dt IN DATE) RETURN BOOLEAN;

END PGIS_POLICY_DTL_CHUNKED;
/
SHOW ERRORS PACKAGE PGIS_POLICY_DTL_CHUNKED


CREATE OR REPLACE PACKAGE BODY PGIS_POLICY_DTL_CHUNKED AS

    TYPE t_report IS TABLE OF PGIS_POLICY_DTL%ROWTYPE INDEX BY PLS_INTEGER;


    FUNCTION month_of (p_val_dt IN DATE) RETURN DATE IS
    BEGIN
        RETURN TRUNC(NVL(p_val_dt, TRUNC(SYSDATE, 'MM') - 1));
    END month_of;


    ----------------------------------------------------------------------------
    -- Chunk bookkeeping. Autonomous, so a chunk's progress is durable the
    -- moment it is written and survives the rollback of a failing chunk.
    ----------------------------------------------------------------------------
    PROCEDURE mark_chunk (
        p_val_dt   IN DATE,
        p_chunk_no IN PLS_INTEGER,
        p_status   IN VARCHAR2,
        p_rows     IN NUMBER   DEFAULT NULL,
        p_arrays   IN NUMBER   DEFAULT NULL,
        p_error    IN VARCHAR2 DEFAULT NULL
    ) IS
        PRAGMA AUTONOMOUS_TRANSACTION;
    BEGIN
        UPDATE PGIS_POLICY_DTL_CHUNK
           SET STATUS      = p_status,
               ROWS_LOADED = NVL(p_rows, ROWS_LOADED),
               ARRAYS      = NVL(p_arrays, ARRAYS),
               STARTED_AT  = CASE WHEN p_status = 'RUNNING' THEN SYSTIMESTAMP ELSE STARTED_AT END,
               ENDED_AT    = CASE WHEN p_status IN ('DONE','FAILED') THEN SYSTIMESTAMP END,
               ERROR_TEXT  = SUBSTR(p_error, 1, 4000)
         WHERE VALUATION_DATE = p_val_dt
           AND CHUNK_NO       = p_chunk_no;

        COMMIT;
    END mark_chunk;


    ----------------------------------------------------------------------------
    -- Decide what this call has to do: start the month from scratch, or pick up
    -- an interrupted one. Returns the chunk count actually in force.
    ----------------------------------------------------------------------------
    FUNCTION plan_month (
        p_val_dt  IN DATE,
        p_chunks  IN PLS_INTEGER,
        p_restart IN BOOLEAN
    ) RETURN PLS_INTEGER IS
        v_planned PLS_INTEGER;
        v_count   PLS_INTEGER;
    BEGIN
        SELECT MAX(CHUNK_COUNT), COUNT(*)
          INTO v_planned, v_count
          FROM PGIS_POLICY_DTL_CHUNK
         WHERE VALUATION_DATE = p_val_dt;

        IF v_count > 0 AND NOT p_restart THEN
            -- Resuming. The plan on disk wins: re-slicing with a different
            -- chunk count would move every policy to a different bucket, and
            -- the chunks already loaded would no longer line up with the ones
            -- still to come.
            IF p_chunks IS NOT NULL AND p_chunks <> v_planned THEN
                DBMS_OUTPUT.PUT_LINE(
                    'resuming ' || TO_CHAR(p_val_dt, 'YYYY-MM-DD') ||
                    ' with its original ' || v_planned || ' chunks, not ' || p_chunks ||
                    ' -- pass p_restart => TRUE to re-slice from empty'
                );
            END IF;
            RETURN v_planned;
        END IF;

        -- Starting fresh: clear the month's data and its chunk plan together,
        -- so the table and the bookkeeping cannot disagree.
        PGIS_POLICY_DTL_LOAD.replace_month(p_val_dt);

        DELETE FROM PGIS_POLICY_DTL_CHUNK WHERE VALUATION_DATE = p_val_dt;

        INSERT INTO PGIS_POLICY_DTL_CHUNK (
            VALUATION_DATE, CHUNK_NO, CHUNK_COUNT, STATUS
        )
        SELECT p_val_dt, LEVEL - 1, p_chunks, 'PENDING'
          FROM DUAL
        CONNECT BY LEVEL <= p_chunks;

        COMMIT;
        RETURN p_chunks;
    END plan_month;


    ----------------------------------------------------------------------------
    -- One chunk: fetch it in arrays of p_limit and insert each array.
    --
    -- The whole chunk is one transaction. A chunk either lands completely or
    -- leaves nothing behind, which is what lets a restart trust the chunk table
    -- -- there is no such thing as a half-loaded chunk to reconcile.
    ----------------------------------------------------------------------------
    PROCEDURE load_chunk (
        p_val_dt      IN  DATE,
        p_chunk_no    IN  PLS_INTEGER,
        p_chunk_count IN  PLS_INTEGER,
        p_limit       IN  PLS_INTEGER,
        p_rows        OUT NUMBER,
        p_arrays      OUT NUMBER
    ) IS
        v_rows   t_report;
        v_cursor SYS_REFCURSOR;
    BEGIN
        p_rows   := 0;
        p_arrays := 0;

        mark_chunk(p_val_dt, p_chunk_no, 'RUNNING');

        PGIS_POLICY_DTL_LOAD.set_chunk(p_chunk_no, p_chunk_count);

        OPEN v_cursor FOR SELECT * FROM PGIS_POLICY_DTL_V;
        LOOP
            FETCH v_cursor BULK COLLECT INTO v_rows LIMIT p_limit;
            EXIT WHEN v_rows.COUNT = 0;

            FORALL i IN 1 .. v_rows.COUNT
                INSERT /*+ APPEND_VALUES */ INTO PGIS_POLICY_DTL VALUES v_rows(i);

            p_rows   := p_rows + v_rows.COUNT;
            p_arrays := p_arrays + 1;

            EXIT WHEN v_rows.COUNT < p_limit;
        END LOOP;
        CLOSE v_cursor;

        -- Ends the chunk's transaction and releases the direct-path lock on the
        -- table before the next chunk asks for it.
        COMMIT;

        mark_chunk(p_val_dt, p_chunk_no, 'DONE', p_rows, p_arrays);
    EXCEPTION
        WHEN OTHERS THEN
            IF v_cursor%ISOPEN THEN
                CLOSE v_cursor;
            END IF;
            ROLLBACK;
            mark_chunk(
                p_val_dt, p_chunk_no, 'FAILED', 0, p_arrays,
                SQLERRM || CHR(10) || DBMS_UTILITY.FORMAT_ERROR_BACKTRACE
            );
            RAISE;
    END load_chunk;


    ----------------------------------------------------------------------------
    -- The month.
    ----------------------------------------------------------------------------
    PROCEDURE load_month_chunked (
        p_val_dt  IN DATE        DEFAULT NULL,
        p_chunks  IN PLS_INTEGER DEFAULT 12,
        p_limit   IN PLS_INTEGER DEFAULT 50000,
        p_restart IN BOOLEAN     DEFAULT FALSE
    ) IS
        v_val_dt   DATE        := month_of(p_val_dt);
        v_chunks   PLS_INTEGER;
        v_run_id   NUMBER;
        v_rows     NUMBER;
        v_arrays   NUMBER;
        v_total    NUMBER      := 0;
        v_done     PLS_INTEGER := 0;
        v_started  NUMBER      := DBMS_UTILITY.GET_TIME;
    BEGIN
        IF p_limit IS NULL OR p_limit < 1 THEN
            RAISE_APPLICATION_ERROR(-20004, 'p_limit must be 1 or more');
        END IF;

        IF p_chunks IS NULL OR p_chunks < 1 THEN
            RAISE_APPLICATION_ERROR(-20005, 'p_chunks must be 1 or more');
        END IF;

        v_run_id := PGIS_POLICY_DTL_LOAD.log_start(v_val_dt, 'CHUNKED');

        PGIS_POLICY_DTL_LOAD.set_valuation_date(v_val_dt);
        v_chunks := plan_month(v_val_dt, p_chunks, p_restart);

        FOR c IN (
            SELECT CHUNK_NO
              FROM PGIS_POLICY_DTL_CHUNK
             WHERE VALUATION_DATE = v_val_dt
               AND STATUS IN ('PENDING', 'RUNNING', 'FAILED')
             ORDER BY CHUNK_NO
        ) LOOP
            -- A chunk left RUNNING or FAILED by an earlier attempt rolled back,
            -- so there is nothing of it in the table and it is simply redone.
            load_chunk(v_val_dt, c.CHUNK_NO, v_chunks, p_limit, v_rows, v_arrays);

            v_total := v_total + v_rows;
            v_done  := v_done + 1;

            DBMS_OUTPUT.PUT_LINE(
                'chunk ' || c.CHUNK_NO || '/' || (v_chunks - 1) ||
                ': ' || v_rows || ' rows in ' || v_arrays || ' arrays, ' ||
                ROUND((DBMS_UTILITY.GET_TIME - v_started) / 100) || 's elapsed'
            );
        END LOOP;

        PGIS_POLICY_DTL_LOAD.clear_chunk;

        IF is_complete(v_val_dt) THEN
            DBMS_STATS.GATHER_TABLE_STATS(
                ownname => USER,
                tabname => 'PGIS_POLICY_DTL',
                degree  => DBMS_STATS.AUTO_DEGREE,
                cascade => TRUE
            );
            PGIS_POLICY_DTL_LOAD.log_end(v_run_id, 'SUCCESS', v_total);
        ELSE
            PGIS_POLICY_DTL_LOAD.log_end(v_run_id, 'PARTIAL', v_total);
        END IF;
    EXCEPTION
        WHEN OTHERS THEN
            PGIS_POLICY_DTL_LOAD.clear_chunk;
            -- The chunks that finished are committed and stay finished. Calling
            -- this procedure again picks up from the one that failed.
            PGIS_POLICY_DTL_LOAD.log_end(
                v_run_id, 'FAILED', v_total,
                SQLERRM || CHR(10) || DBMS_UTILITY.FORMAT_ERROR_BACKTRACE
            );
            RAISE;
    END load_month_chunked;


    FUNCTION is_complete (p_val_dt IN DATE) RETURN BOOLEAN IS
        v_outstanding PLS_INTEGER;
    BEGIN
        SELECT COUNT(*)
          INTO v_outstanding
          FROM PGIS_POLICY_DTL_CHUNK
         WHERE VALUATION_DATE = month_of(p_val_dt)
           AND STATUS <> 'DONE';

        RETURN v_outstanding = 0;
    END is_complete;


    PROCEDURE show_progress (p_val_dt IN DATE DEFAULT NULL) IS
        v_val_dt DATE := month_of(p_val_dt);
    BEGIN
        DBMS_OUTPUT.PUT_LINE('valuation date ' || TO_CHAR(v_val_dt, 'YYYY-MM-DD'));

        FOR c IN (
            SELECT STATUS,
                   COUNT(*)         AS CHUNKS,
                   SUM(ROWS_LOADED) AS ROWS_LOADED
              FROM PGIS_POLICY_DTL_CHUNK
             WHERE VALUATION_DATE = v_val_dt
             GROUP BY STATUS
             ORDER BY STATUS
        ) LOOP
            DBMS_OUTPUT.PUT_LINE(
                RPAD(c.STATUS, 10) || LPAD(c.CHUNKS, 5) || ' chunks' ||
                LPAD(NVL(c.ROWS_LOADED, 0), 12) || ' rows'
            );
        END LOOP;

        FOR c IN (
            SELECT CHUNK_NO, ERROR_TEXT
              FROM PGIS_POLICY_DTL_CHUNK
             WHERE VALUATION_DATE = v_val_dt
               AND STATUS = 'FAILED'
             ORDER BY CHUNK_NO
        ) LOOP
            DBMS_OUTPUT.PUT_LINE('chunk ' || c.CHUNK_NO || ': ' || SUBSTR(c.ERROR_TEXT, 1, 200));
        END LOOP;
    END show_progress;

END PGIS_POLICY_DTL_CHUNKED;
/
SHOW ERRORS PACKAGE BODY PGIS_POLICY_DTL_CHUNKED


--------------------------------------------------------------------------------
-- Using it
--------------------------------------------------------------------------------
-- SET SERVEROUTPUT ON SIZE UNLIMITED
--
-- -- start (or continue) March 2026 in twelve chunks of 50000-row arrays
-- EXEC PGIS_POLICY_DTL_CHUNKED.load_month_chunked(DATE '2026-03-31', 12, 50000);
--
-- -- it stopped. See where it got to:
-- EXEC PGIS_POLICY_DTL_CHUNKED.show_progress(DATE '2026-03-31');
--
-- -- carry on from there. Same command; it does only what is left:
-- EXEC PGIS_POLICY_DTL_CHUNKED.load_month_chunked(DATE '2026-03-31');
--
-- -- throw the month away and start it again:
-- EXEC PGIS_POLICY_DTL_CHUNKED.load_month_chunked(DATE '2026-03-31', 12, 50000, TRUE);
--
-- Run it as a job rather than from SQL*Plus and a dropped connection stops
-- being a consideration at all:
--
-- BEGIN
--     DBMS_SCHEDULER.CREATE_JOB(
--         job_name   => 'PGIS_POLICY_DTL_CHUNKED_RUN',
--         job_type   => 'PLSQL_BLOCK',
--         job_action => 'BEGIN PGIS_POLICY_DTL_CHUNKED.load_month_chunked(DATE ''2026-03-31''); END;',
--         enabled    => TRUE
--     );
-- END;
-- /
--------------------------------------------------------------------------------
