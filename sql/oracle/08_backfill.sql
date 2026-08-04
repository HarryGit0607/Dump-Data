--------------------------------------------------------------------------------
-- Load the months that are already behind you.
--
-- Each month is a separate transaction, so an interrupted backfill keeps every
-- month it finished and you simply restart from the first one missing. Watch
-- progress in PGIS_POLICY_DTL_LOG from another session.
--------------------------------------------------------------------------------
SET SERVEROUTPUT ON

-- Every month end from April 2025 to March 2026:
BEGIN
    PGIS_POLICY_DTL_LOAD.backfill(
        p_from_month_end => DATE '2025-04-30',
        p_to_month_end   => DATE '2026-03-31',
        p_parallel       => 8
    );
END;
/

--------------------------------------------------------------------------------
-- Restart a backfill after an interruption: skip what is already there.
--------------------------------------------------------------------------------
-- DECLARE
--     v_dt   DATE := DATE '2025-04-30';
--     v_last DATE := DATE '2026-03-31';
--     v_done PLS_INTEGER;
-- BEGIN
--     WHILE v_dt <= v_last LOOP
--         SELECT COUNT(*) INTO v_done
--           FROM PGIS_POLICY_DTL_LOG
--          WHERE VALUATION_DATE = v_dt
--            AND STATUS = 'SUCCESS';
--
--         IF v_done = 0 THEN
--             DBMS_OUTPUT.PUT_LINE('loading ' || TO_CHAR(v_dt, 'YYYY-MM-DD'));
--             PGIS_POLICY_DTL_LOAD.load_month(v_dt);
--         ELSE
--             DBMS_OUTPUT.PUT_LINE('skipping ' || TO_CHAR(v_dt, 'YYYY-MM-DD'));
--         END IF;
--
--         v_dt := TRUNC(LAST_DAY(ADD_MONTHS(v_dt, 1)));
--     END LOOP;
-- END;
-- /

--------------------------------------------------------------------------------
-- Retire old months. Dropping a partition is instant and reclaims the space;
-- deleting the same rows would take far longer and reclaim nothing.
--------------------------------------------------------------------------------
-- ALTER TABLE PGIS_POLICY_DTL DROP PARTITION FOR (DATE '2025-04-30') UPDATE INDEXES;
