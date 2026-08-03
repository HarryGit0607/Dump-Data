--------------------------------------------------------------------------------
-- PGIS_POLICY_DTL_BULK - the same load written as a cursor with BULK COLLECT
-- and FORALL, so it can be timed against the plain INSERT ... SELECT.
--
-- It is here to be measured, not to be used. Expect it to be slower, because it
-- adds work that INSERT ... SELECT simply does not do:
--
--   * every row is carried out of the SQL engine into PL/SQL memory (PGA),
--     converted into PL/SQL types, and handed back to the SQL engine to be
--     written. INSERT ... SELECT never leaves SQL;
--   * BULK COLLECT and FORALL reduce the context switches between the two
--     engines from one per row to one per array -- which is exactly why they
--     beat a row-by-row loop, and exactly why they still lose to no switching
--     at all;
--   * a FORALL is serial. The query feeding it can run in parallel, but the
--     insert itself is one process. INSERT ... SELECT can be parallel on both
--     sides, so on a box with cores to spare the gap widens with the degree of
--     parallelism;
--   * the array lives in the PGA, so the memory cost is real and the array size
--     has to be tuned. INSERT ... SELECT has nothing to tune.
--
-- The mantra it runs into (Tom Kyte's, and still true): do it in a single SQL
-- statement if you possibly can; if you cannot, do it in PL/SQL. This load can
-- be done in a single SQL statement.
--
-- There are two honest reasons to reach for this shape, and neither applies to
-- a straight dump of a report:
--
--   1. per-row transformation that genuinely cannot be written in SQL;
--   2. carrying on past bad rows instead of losing the whole statement.
--
-- For (2), note the trap encoded below: SAVE EXCEPTIONS and APPEND_VALUES are
-- mutually exclusive. Asking for both raises ORA-38910, so the tolerant version
-- is also the conventional-path one -- you choose either direct-path speed or
-- per-row tolerance. Pure SQL does not force that choice on you:
--
--     INSERT INTO PGIS_POLICY_DTL
--     SELECT * FROM PGIS_POLICY_DTL_V
--     LOG ERRORS INTO ERR$_PGIS_POLICY_DTL ('2026-03-31') REJECT LIMIT UNLIMITED;
--
--     -- error table first:
--     -- EXEC DBMS_ERRLOG.CREATE_ERROR_LOG('PGIS_POLICY_DTL');
--
--   with the caveat that a direct-path INSERT which raises a unique constraint
--   or index violation fails and rolls back instead of logging the row, so
--   error logging and /*+ APPEND */ only combine when there is no unique
--   constraint in play. There is none on PGIS_POLICY_DTL.
--
-- And if what you actually want is chunked, restartable, genuinely parallel
-- PL/SQL over a huge volume, the tool for that is DBMS_PARALLEL_EXECUTE, which
-- splits the work by ROWID or by a key range and runs the chunks concurrently.
-- Hand-rolled BULK COLLECT with intermediate commits is a slower imitation of
-- it that also leaves you half-loaded when it fails.
--
-- Needs 01-05 installed first.
--------------------------------------------------------------------------------
CREATE OR REPLACE PACKAGE PGIS_POLICY_DTL_BULK AS

    -- p_limit         rows per BULK COLLECT fetch. 100-1000 is the useful
    --                 range; beyond that the PGA cost grows faster than the
    --                 saving on context switches.
    -- p_direct        TRUE  -> FORALL with /*+ APPEND_VALUES */ (direct path)
    --                 FALSE -> FORALL with SAVE EXCEPTIONS (tolerates bad rows)
    -- p_commit_every  commit every N arrays; 0 commits once at the end.
    --                 Committing inside the loop shortens undo but gives up
    --                 read consistency for the still-open cursor, which is how
    --                 ORA-01555 is usually earned.
    PROCEDURE load_month_bulk (
        p_val_dt       IN DATE        DEFAULT NULL,
        p_limit        IN PLS_INTEGER DEFAULT 1000,
        p_direct       IN BOOLEAN     DEFAULT TRUE,
        p_replace      IN BOOLEAN     DEFAULT TRUE,
        p_commit_every IN PLS_INTEGER DEFAULT 0
    );

END PGIS_POLICY_DTL_BULK;
/
SHOW ERRORS PACKAGE PGIS_POLICY_DTL_BULK


CREATE OR REPLACE PACKAGE BODY PGIS_POLICY_DTL_BULK AS

    TYPE t_report IS TABLE OF PGIS_POLICY_DTL%ROWTYPE INDEX BY PLS_INTEGER;

    e_bulk_errors EXCEPTION;
    PRAGMA EXCEPTION_INIT(e_bulk_errors, -24381);


    PROCEDURE load_month_bulk (
        p_val_dt       IN DATE        DEFAULT NULL,
        p_limit        IN PLS_INTEGER DEFAULT 1000,
        p_direct       IN BOOLEAN     DEFAULT TRUE,
        p_replace      IN BOOLEAN     DEFAULT TRUE,
        p_commit_every IN PLS_INTEGER DEFAULT 0
    ) IS
        v_val_dt  DATE := TRUNC(NVL(p_val_dt, TRUNC(SYSDATE, 'MM') - 1));
        v_run_id  NUMBER;
        v_rows    t_report;
        v_total   NUMBER      := 0;
        v_rejects NUMBER      := 0;
        v_bad     PLS_INTEGER := 0;
        v_arrays  PLS_INTEGER := 0;
        v_cursor  SYS_REFCURSOR;
    BEGIN
        v_run_id := PGIS_POLICY_DTL_LOAD.log_start(v_val_dt, 'BULK');

        PGIS_POLICY_DTL_LOAD.set_valuation_date(v_val_dt);

        IF p_replace THEN
            PGIS_POLICY_DTL_LOAD.replace_month(v_val_dt);
        END IF;

        OPEN v_cursor FOR SELECT * FROM PGIS_POLICY_DTL_V;

        LOOP
            FETCH v_cursor BULK COLLECT INTO v_rows LIMIT p_limit;
            EXIT WHEN v_rows.COUNT = 0;

            v_bad := 0;

            IF p_direct THEN
                -- Direct path, and therefore all-or-nothing: APPEND_VALUES
                -- cannot be combined with SAVE EXCEPTIONS (ORA-38910).
                FORALL i IN 1 .. v_rows.COUNT
                    INSERT /*+ APPEND_VALUES */ INTO PGIS_POLICY_DTL VALUES v_rows(i);
            ELSE
                BEGIN
                    FORALL i IN 1 .. v_rows.COUNT SAVE EXCEPTIONS
                        INSERT INTO PGIS_POLICY_DTL VALUES v_rows(i);
                EXCEPTION
                    WHEN e_bulk_errors THEN
                        -- ORA-24381: some rows of the array failed, the rest
                        -- went in. Count them and carry on.
                        v_bad := SQL%BULK_EXCEPTIONS.COUNT;
                        FOR i IN 1 .. LEAST(v_bad, 10) LOOP
                            DBMS_OUTPUT.PUT_LINE(
                                'array ' || (v_arrays + 1) ||
                                ' row '  || SQL%BULK_EXCEPTIONS(i).ERROR_INDEX ||
                                ': '     || SQLERRM(-SQL%BULK_EXCEPTIONS(i).ERROR_CODE)
                            );
                        END LOOP;
                END;
            END IF;

            v_rejects := v_rejects + v_bad;
            v_total   := v_total + v_rows.COUNT - v_bad;
            v_arrays  := v_arrays + 1;

            IF p_commit_every > 0 AND MOD(v_arrays, p_commit_every) = 0 THEN
                COMMIT;
            END IF;

            EXIT WHEN v_rows.COUNT < p_limit;
        END LOOP;

        CLOSE v_cursor;
        COMMIT;

        PGIS_POLICY_DTL_LOAD.log_end(
            v_run_id,
            CASE WHEN v_rejects > 0 THEN 'PARTIAL' ELSE 'SUCCESS' END,
            v_total,
            CASE WHEN v_rejects > 0 THEN v_rejects || ' rows rejected' END
        );
    EXCEPTION
        WHEN OTHERS THEN
            IF v_cursor%ISOPEN THEN
                CLOSE v_cursor;
            END IF;
            ROLLBACK;
            PGIS_POLICY_DTL_LOAD.log_end(
                v_run_id,
                'FAILED',
                NULL,
                SQLERRM || CHR(10) || DBMS_UTILITY.FORMAT_ERROR_BACKTRACE
            );
            RAISE;
    END load_month_bulk;

END PGIS_POLICY_DTL_BULK;
/
SHOW ERRORS PACKAGE BODY PGIS_POLICY_DTL_BULK
