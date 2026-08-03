--------------------------------------------------------------------------------
-- PGIS_POLICY_DTL_V - Earned / Unearned exposure, premium and commission report
--
-- This is the tuned report query, unchanged except for one thing: the valuation
-- date is no longer the literal '31-MAR-2026'. It is read from the application
-- context, so the same definition serves every month end:
--
--     EXEC PGIS_POLICY_DTL_LOAD.set_valuation_date(DATE '2026-03-31');
--     SELECT * FROM PGIS_POLICY_DTL_V;
--
-- The context value is stored as YYYY-MM-DD, which keeps the date free of
-- NLS_DATE_LANGUAGE. The original TO_DATE('31-MAR-2026','DD-MON-YYYY') would
-- have failed with ORA-01843 in any session whose date language is not English
-- -- a real hazard once this runs unattended from a scheduler job.
--
-- If the context is not set, SYS_CONTEXT returns NULL, the date is NULL and the
-- view returns no rows. PGIS_POLICY_DTL_LOAD always sets it before loading.
--
-- Everything below this line is the query as tuned, and the notes are the
-- original ones:
--
--  1. MAX(POLH_END_NO_IDX) latest-endorsement filter
--     Was: POLH_END_NO_IDX = (SELECT MAX(P2.POLH_END_NO_IDX) FROM PGITH_POLICY P2
--                             WHERE P2.POLH_SYS_ID = PGITH_POLICY.POLH_SYS_ID)
--     A correlated aggregate re-executed for every candidate row.
--     Now: an equivalent NOT EXISTS anti-join ("no sibling row with a bigger
--     index exists"). The driving row itself belongs to PGITH_POLICY, so the
--     MAX set is never empty and equality holds exactly when no greater index
--     exists; POLH_END_NO_IDX can never be NULL here because it is an equality
--     join key against AD_END_NO_IDX. Oracle can resolve an anti-join with a
--     single hash/merge pass instead of one lookup per row.
--
--  2. Commission lookups (PGIT_BULK_ACNT_DTL)
--     Was: two correlated scalar sub-queries per row, i.e. two visits to the
--     bulk accounting table for every policy - one for "not YA%" (commission)
--     and one for "YA%" (TPA commission).
--     Now: one pre-aggregated set, keyed on
--     (BAD_POL_SYS_ID, BAD_END_NO_IDX, BAD_CVR_IND_CODE), scoped to the driving
--     policy keys and LEFT JOINed on the very same OD/TP CASE expression.
--     NULL semantics are preserved: a policy with no matching rows, or an
--     AD_ANLY_CODE_1 that is neither %MOTOD% nor %MOTTP% (CASE -> NULL, which
--     matches nothing), still yields NULL, exactly like the scalar sub-query.
--
--  3. Descriptive lookups in the outer SELECT (PGIM_PRODUCT, PCOM_CUSTOMER x2,
--     FM_DIVISION, PGIT_POL_RISK_ADDL_INFO, PGIT_POL_RISK_COVER)
--     Was: six correlated scalar sub-queries executed once per reported row.
--     Now: LEFT OUTER JOINs / pre-aggregated single-row lookups. A scalar
--     sub-query must already return at most one row (otherwise the original
--     raises ORA-01427), so collapsing with MAX() over the same predicates
--     returns that identical single value, and a missing row still gives NULL.
--     All lookup keys (POLH_PROD_CODE, POLH_SRC_CODE, POLH_TPA_CODE,
--     POLH_DIVN_CODE, POLH_SYS_ID) are already GROUP BY columns, so the looked
--     up values are functionally dependent on the grouping and adding them to
--     the GROUP BY cannot change the number of groups.
--
--  4. EXISTS on PGIM_LIVE_STATUS
--     AD_DIVN_CODE is pinned to '411600' by the WHERE clause, so the EXISTS was
--     correlated on a constant. It is now uncorrelated (evaluated once) and the
--     TRUNC(LS_LIVE_DATE) <= valuation date predicate is expressed as the
--     equivalent SARGable range LS_LIVE_DATE < valuation date + 1 so a plain
--     index on LS_LIVE_DATE can be used.
--
--  5. The valuation date was evaluated ~35 times per row. It is now evaluated
--     once per aggregated row (VAL_DT) and referenced everywhere above.
--     VALUATION_DATE is a constant, so carrying it in the GROUP BY does not
--     change the grouping.
--
--  6. ACCNT_STRT - a scalar sub-query on FM_COMP_ACNT_YEAR that was selected in
--     the innermost block but never referenced by any outer block - removed.
--     Old-style comma joins replaced by explicit ANSI joins, and duplicate
--     GROUP BY entries (POLH_END_NO_IDX, POLH_UW_YEAR, POLH_ASSR_NAME) removed.
--     None of these affect the result set.
--
-- Indexes that make the biggest difference for this report (create only if the
-- equivalents do not already exist):
--     PGIT_ACNT_DOC     (AD_DIVN_CODE, AD_POST_YN, AD_MAIN_ACNT_CODE, AD_DOC_DT,
--                        AD_POL_SYS_ID, AD_END_NO_IDX, AD_END_SR_NO)
--     PGITH_POLICY      (POLH_SYS_ID, POLH_END_NO_IDX, POLH_END_SR_NO)
--     PGITH_POLICY      (POLH_TO_DT)   -- now the monthly driving predicate
--     PGIT_BULK_ACNT_DTL(BAD_POL_SYS_ID, BAD_END_NO_IDX, BAD_CVR_IND_CODE)
--     PGIT_POL_RISK_COVER    (PRC_POL_SYS_ID, PRC_CODE)
--     PGIT_POL_RISK_ADDL_INFO(PRAI_POL_SYS_ID, PRAI_RISK_LVL_NO, PRAI_LVL1_SR_NO)
--
-- Report parameters still hard-coded below, listed here so they are not a
-- surprise when this runs every month unattended:
--     AD_DIVN_CODE / LS_OFFICE_CODE = '411600'  (single division)
--     CAY_ACNT_YEAR                 = '24'      (accounting-year cut-off for
--                                                AD_DOC_DT; anchored to AY24,
--                                                it does not roll forward)
--     POLH_DEPT_CODE BETWEEN '11' AND '48'
--------------------------------------------------------------------------------
CREATE OR REPLACE VIEW PGIS_POLICY_DTL_V AS
WITH policy_agg AS (
    -- MATERIALIZE: this block is referenced three times (policy_days,
    -- policy_keys, detail_keys). Oracle usually materialises a multiply
    -- referenced WITH block on its own, the hint just makes it deterministic so
    -- the expensive PGITH_POLICY/PGIT_ACNT_DOC aggregation is done exactly once.
    SELECT /*+ MATERIALIZE */
        h.POLH_SYS_ID,
        h.POLH_NO,
        h.POLH_END_NO_IDX,
        h.POLH_FM_DT,
        -- Raw policy end date: UNEARNED_DAYS below is deliberately based on the
        -- untouched column, exactly as in the original inner block.
        h.POLH_TO_DT AS RAW_POLH_TO_DT,
        CASE
            WHEN h.POLH_PROD_CODE IN ('MOT-POS-012','MOT-PRD-012') AND d.AD_ANLY_CODE_1 = 'OICL_MOTTP'
                 THEN ADD_MONTHS(h.POLH_FM_DT,36)
            WHEN h.POLH_PROD_CODE IN ('MOT-POS-013','MOT-PRD-013') AND d.AD_ANLY_CODE_1 = 'OICL_MOTTP'
                 THEN ADD_MONTHS(h.POLH_FM_DT,60)
            ELSE h.POLH_TO_DT
        END AS POLH_TO_DT,
        h.POLH_END_EFF_FM_DT,
        h.POLH_END_TYPE,
        h.POLH_DEPT_CODE,
        h.POLH_ASSR_CODE,
        h.POLH_ASSR_NAME,
        h.POLH_DIVN_CODE,
        h.POLH_PREM_LC_1,
        h.POLH_ORG_PREM_LC_1,
        h.POLH_PROD_CODE,
        h.POLH_SRC_TYPE,
        h.POLH_SRC_CODE,
        h.POLH_TPA_CODE,
        h.POLH_UW_YEAR,
        h.POLH_BUS_TYPE,
        h.POLH_INST_YN,
        h.POLH_INSTL_METHOD,
        d.AD_ACNT_YEAR,
        d.AD_ANLY_CODE_1,
        d.AD_ANLY_CODE_2,
        TO_DATE(SYS_CONTEXT('PGIS_RPT_CTX','VAL_DT'),'YYYY-MM-DD') AS VAL_DT,
        CASE
            WHEN h.POLH_END_NO_IDX <> 0 THEN SUM(h.POLH_PREM_LC_1)
            ELSE 0
        END AS EP_EXPECTED_PREM_CURR_MONTH,
        SUM(NVL(d.AD_AMT_LC_1,0) * DECODE(d.AD_DRCR_FLAG,'D',-1,'C',1)) AS TOT_PREM
    FROM PGITH_POLICY h
    JOIN PGIT_ACNT_DOC d
      ON d.AD_POL_SYS_ID = h.POLH_SYS_ID
     AND d.AD_END_NO_IDX = h.POLH_END_NO_IDX
     AND d.AD_END_SR_NO  = h.POLH_END_SR_NO
    WHERE d.AD_DIVN_CODE = '411600'
      AND d.AD_POST_YN   = '1'
      AND d.AD_MAIN_ACNT_CODE IN ('1101','1171','1107','5482')
      AND d.AD_DOC_DT >= (SELECT CAY_FRM_DT
                            FROM FM_COMP_ACNT_YEAR
                           WHERE CAY_ACNT_YEAR = '24')
      AND h.POLH_TO_DT      >= TO_DATE(SYS_CONTEXT('PGIS_RPT_CTX','VAL_DT'),'YYYY-MM-DD')
      AND h.POLH_DEPT_CODE  BETWEEN '11' AND '48'
      AND h.POLH_CLASS_CODE BETWEEN '0' AND 'ZZZZZZZZZZZZ'
      AND h.POLH_PROD_CODE  BETWEEN '0' AND 'ZZZZZZZZZZZZ'
      AND h.POLH_BUS_TYPE   BETWEEN '0' AND 'ZZZZZZZZZZZZ'
      -- AD_DIVN_CODE is fixed at '411600' above, so this check is constant for
      -- the whole statement, and is the range form of the original
      -- TRUNC(LS_LIVE_DATE) <= valuation date.
      AND EXISTS (
              SELECT 1
                FROM PGIM_LIVE_STATUS
               WHERE LS_OFFICE_CODE = '411600'
                 AND LS_LIVE_DATE < TRUNC(TO_DATE(SYS_CONTEXT('PGIS_RPT_CTX','VAL_DT'),'YYYY-MM-DD')) + 1
          )
      -- Equivalent to POLH_END_NO_IDX = (SELECT MAX(POLH_END_NO_IDX) ...)
      AND NOT EXISTS (
              SELECT 1
                FROM PGITH_POLICY p2
               WHERE p2.POLH_SYS_ID     = h.POLH_SYS_ID
                 AND p2.POLH_END_NO_IDX > h.POLH_END_NO_IDX
          )
    GROUP BY
        h.POLH_SYS_ID,
        h.POLH_NO,
        h.POLH_END_NO_IDX,
        h.POLH_FM_DT,
        h.POLH_DEPT_CODE,
        h.POLH_TO_DT,
        h.POLH_PREM_LC_1,
        h.POLH_ORG_PREM_LC_1,
        h.POLH_UW_YEAR,
        h.POLH_DIVN_CODE,
        h.POLH_END_EFF_FM_DT,
        d.AD_ANLY_CODE_1,
        d.AD_ANLY_CODE_2,
        h.POLH_END_TYPE,
        h.POLH_ASSR_CODE,
        h.POLH_PROD_CODE,
        h.POLH_ASSR_NAME,
        h.POLH_SRC_TYPE,
        h.POLH_SRC_CODE,
        h.POLH_TPA_CODE,
        d.AD_ACNT_YEAR,
        h.POLH_BUS_TYPE,
        h.POLH_INST_YN,
        h.POLH_INSTL_METHOD
),
policy_days AS (
    SELECT
        p.*,
        FLOOR(
            p.POLH_TO_DT
            - DECODE(p.POLH_END_NO_IDX, 0, TRUNC(p.POLH_FM_DT), TRUNC(p.POLH_END_EFF_FM_DT))
        ) + 1 AS TOT_NO_OF_DAYS,
        CASE
            WHEN p.VAL_DT <= TRUNC(p.RAW_POLH_TO_DT)
                 AND p.VAL_DT >= DECODE(p.POLH_END_NO_IDX, 0, TRUNC(p.POLH_FM_DT), TRUNC(p.POLH_END_EFF_FM_DT))
            THEN ROUND(FLOOR(p.VAL_DT - DECODE(p.POLH_END_NO_IDX, 0, TRUNC(p.POLH_FM_DT), TRUNC(p.POLH_END_EFF_FM_DT))) + 1)
            WHEN p.VAL_DT >= TRUNC(p.RAW_POLH_TO_DT)
            THEN ROUND(FLOOR(TRUNC(p.RAW_POLH_TO_DT) - DECODE(p.POLH_END_NO_IDX, 0, TRUNC(p.POLH_FM_DT), TRUNC(p.POLH_END_EFF_FM_DT))) + 1)
            ELSE 0
        END AS UNEARNED_DAYS
    FROM policy_agg p
),
policy_keys AS (
    SELECT DISTINCT
        POLH_SYS_ID,
        POLH_END_NO_IDX
    FROM policy_agg
),
-- One pass over PGIT_BULK_ACNT_DTL for both commission buckets instead of two
-- correlated scalar sub-queries per policy. Grouping on BAD_CVR_IND_CODE keeps
-- the OD / TP selection (and its NULL behaviour) identical to the original.
commission_by_cover AS (
    SELECT
        b.BAD_POL_SYS_ID,
        b.BAD_END_NO_IDX,
        b.BAD_CVR_IND_CODE,
        SUM(CASE WHEN b.BAD_CUST_CODE NOT LIKE 'YA%' THEN b.BAD_AMT_LC_1 END) AS COMMISSION_1,
        SUM(CASE WHEN b.BAD_CUST_CODE     LIKE 'YA%' THEN b.BAD_AMT_LC_1 END) AS TPA_COMMISSION_1
    FROM PGIT_BULK_ACNT_DTL b
    JOIN policy_keys k
      ON k.POLH_SYS_ID     = b.BAD_POL_SYS_ID
     AND k.POLH_END_NO_IDX = b.BAD_END_NO_IDX
    GROUP BY
        b.BAD_POL_SYS_ID,
        b.BAD_END_NO_IDX,
        b.BAD_CVR_IND_CODE
),
detail AS (
    SELECT
        p.POLH_SYS_ID,
        p.POLH_NO,
        p.POLH_FM_DT,
        p.POLH_TO_DT,
        p.POLH_DEPT_CODE,
        p.POLH_ASSR_CODE,
        p.POLH_ASSR_NAME,
        p.TOT_PREM,
        p.POLH_SRC_TYPE,
        p.POLH_UW_YEAR,
        p.POLH_SRC_CODE,
        p.POLH_TPA_CODE,
        p.POLH_DIVN_CODE,
        p.POLH_PREM_LC_1,
        p.AD_ACNT_YEAR,
        p.POLH_ORG_PREM_LC_1,
        p.POLH_END_NO_IDX,
        p.POLH_PROD_CODE,
        p.POLH_BUS_TYPE,
        p.POLH_INST_YN,
        p.POLH_INSTL_METHOD,
        p.AD_ANLY_CODE_1,
        p.AD_ANLY_CODE_2,
        p.EP_EXPECTED_PREM_CURR_MONTH,
        p.VAL_DT AS VALUATION_DATE,
        FLOOR(CASE
                  WHEN p.VAL_DT < p.POLH_FM_DT
                  THEN 0
                  ELSE LEAST(
                           (p.VAL_DT - p.POLH_FM_DT + 2),
                           p.POLH_TO_DT - p.POLH_FM_DT + 1
                       )
              END) AS EARNED_EXPOSURE,
        CASE
            WHEN FLOOR(p.POLH_TO_DT - p.POLH_FM_DT + 1) > 1
            THEN
                ROUND(
                    (
                        p.TOT_PREM
                        / NULLIF(FLOOR(p.POLH_TO_DT - p.POLH_FM_DT + 1),0)
                    )
                    *
                    GREATEST(
                        0,
                        LEAST(
                            p.VAL_DT,
                            p.POLH_TO_DT
                        )
                        -
                        GREATEST(
                            p.POLH_FM_DT,
                            TRUNC(p.VAL_DT,'MM')
                        )
                        + 1
                    )
                ,2)
            ELSE 0
        END AS CURR_MONTH_PREM_EP,
        FLOOR(p.POLH_TO_DT - p.POLH_FM_DT + 1)
        - (CASE
               WHEN p.VAL_DT < p.POLH_FM_DT
               THEN 0
               ELSE LEAST(
                        (p.VAL_DT - p.POLH_FM_DT + 1),
                        (p.POLH_TO_DT - p.POLH_FM_DT + 1)
                    )
           END) AS UNEARNED_EXPOSURE,
        -(p.POLH_FM_DT - (p.VAL_DT + 1)) AS VALUATION_DAYS,
        CASE
            WHEN FLOOR(p.POLH_TO_DT - p.POLH_FM_DT + 1) > 1
            THEN FLOOR(
                     ((c.COMMISSION_1 / NULLIF(FLOOR(p.POLH_TO_DT - p.POLH_FM_DT + 1),0))
                      * -(p.POLH_FM_DT - ((p.VAL_DT + 2))))
                 )
            ELSE 0
        END AS COMMISSION,
        CASE
            WHEN FLOOR(p.POLH_TO_DT - p.POLH_FM_DT + 1) > 1
            THEN ROUND(
                     ((c.TPA_COMMISSION_1 / NULLIF(FLOOR(p.POLH_TO_DT - p.POLH_FM_DT + 1),0))
                      * (-(p.POLH_FM_DT - (p.VAL_DT + 2))))
                 ,2)
            ELSE 0
        END AS TPA_COMMISSION,
        c.COMMISSION_1,
        c.TPA_COMMISSION_1,
        CASE
            WHEN p.TOT_NO_OF_DAYS > 365
            THEN ROUND((p.TOT_PREM / p.TOT_NO_OF_DAYS) * 365 ,2)
            ELSE p.TOT_PREM
        END AS LONG_TERM_PREMIUM,
        CASE
            WHEN p.TOT_NO_OF_DAYS > 365
            THEN ROUND(p.TOT_PREM - ((p.TOT_PREM / p.TOT_NO_OF_DAYS) * 365) ,2)
            ELSE 0
        END AS ADVANCE_PREMIUM,
        CASE
            WHEN p.POLH_DEPT_CODE = '21'
                 AND p.POLH_TO_DT IS NULL
            THEN 0
            WHEN p.POLH_DEPT_CODE = '22'
            THEN 0
            ELSE ROUND(
                     (
                       p.TOT_PREM *
                       LEAST(
                             GREATEST(
                                      365 - p.UNEARNED_DAYS,
                                      0
                                     ),
                              365
                            )
                     )
                     / DECODE(p.TOT_NO_OF_DAYS,NULL,1,0,1,p.TOT_NO_OF_DAYS)
                 ,2)
        END AS CURR_PREM_1,
        CASE
            WHEN p.POLH_DEPT_CODE = '21'
                 AND p.POLH_TO_DT IS NULL
            THEN 0
            WHEN p.POLH_DEPT_CODE = '22'
            THEN 0
            WHEN p.TOT_NO_OF_DAYS <= 365
            THEN 0
            ELSE ROUND(
                     (
                       p.TOT_PREM *
                       LEAST(
                             GREATEST(
                                      730 - p.UNEARNED_DAYS,
                                      0
                                     ),
                              365
                            )
                     )
                     / DECODE(p.TOT_NO_OF_DAYS,NULL,1,0,1,p.TOT_NO_OF_DAYS)
                 ,2)
        END AS CURR_PREM_2,
        CASE
            WHEN p.POLH_DEPT_CODE = '21'
                 AND p.POLH_TO_DT IS NULL
            THEN 0
            WHEN p.POLH_DEPT_CODE = '22'
            THEN 0
            WHEN p.TOT_NO_OF_DAYS <= 730
            THEN 0
            ELSE ROUND(
                     (
                       p.TOT_PREM *
                       LEAST(
                             GREATEST(
                                      1095 - p.UNEARNED_DAYS,
                                      0
                                     ),
                              365
                            )
                     )
                     / DECODE(p.TOT_NO_OF_DAYS,NULL,1,0,1,p.TOT_NO_OF_DAYS)
                 ,2)
        END AS CURR_PREM_3,
        CASE
            WHEN p.POLH_DEPT_CODE = '21'
                 AND p.POLH_TO_DT IS NULL
            THEN 0
            WHEN p.POLH_DEPT_CODE = '22'
            THEN 0
            WHEN p.TOT_NO_OF_DAYS <= 1095
            THEN 0
            ELSE ROUND(
                     (
                       p.TOT_PREM *
                       GREATEST(
                                p.TOT_NO_OF_DAYS - 1095,
                                0
                               )
                     )
                     / DECODE(p.TOT_NO_OF_DAYS,NULL,1,0,1,p.TOT_NO_OF_DAYS)
                 ,2)
        END AS CURR_PREM_4,
        CASE
            WHEN p.POLH_DEPT_CODE = '21' AND p.POLH_TO_DT IS NULL
            THEN p.TOT_PREM
            WHEN p.POLH_DEPT_CODE = '22'
            THEN p.TOT_PREM
            WHEN p.POLH_DEPT_CODE = '43' AND p.POLH_END_TYPE = '021'
            THEN p.TOT_PREM
            ELSE ROUND(p.TOT_PREM * p.UNEARNED_DAYS
                       / DECODE(p.TOT_NO_OF_DAYS,NULL,1,0,1,p.TOT_NO_OF_DAYS))
        END AS EARNED_PREMIUM,
        p.VAL_DT
        -
        GREATEST(
            ADD_MONTHS(
                TRUNC(
                    ADD_MONTHS(p.VAL_DT,-3),
                    'YYYY'
                ),3
            ),
            p.POLH_FM_DT
        ) + 1 AS FY_EARNED_PREMIUM
    FROM policy_days p
    LEFT JOIN commission_by_cover c
      ON c.BAD_POL_SYS_ID   = p.POLH_SYS_ID
     AND c.BAD_END_NO_IDX   = p.POLH_END_NO_IDX
     AND c.BAD_CVR_IND_CODE = CASE
                                  WHEN p.AD_ANLY_CODE_1 LIKE '%MOTOD%' THEN 'OD'
                                  WHEN p.AD_ANLY_CODE_1 LIKE '%MOTTP%' THEN 'TP'
                              END
),
detail_keys AS (
    SELECT DISTINCT POLH_SYS_ID FROM policy_agg
),
-- Replaces the CPA_FLAG scalar sub-query. The source lookup can only return a
-- single row per policy (the scalar sub-query would raise ORA-01427 otherwise),
-- so MAX() simply collapses that one row.
cpa_flag AS (
    SELECT
        r.PRAI_POL_SYS_ID,
        MAX(r.PRAI_YN_24) AS PRAI_YN_24
    FROM PGIT_POL_RISK_ADDL_INFO r
    JOIN detail_keys k
      ON k.POLH_SYS_ID = r.PRAI_POL_SYS_ID
    WHERE r.PRAI_RISK_LVL_NO = '1'
      AND r.PRAI_LVL1_SR_NO  = '1'
    GROUP BY r.PRAI_POL_SYS_ID
),
-- Replaces the PA_PREMIUM scalar sub-query, same single-row reasoning.
pa_premium AS (
    SELECT
        c.PRC_POL_SYS_ID,
        MAX(c.PRC_PREM_LC_1) AS PRC_PREM_LC_1
    FROM PGIT_POL_RISK_COVER c
    JOIN detail_keys k
      ON k.POLH_SYS_ID = c.PRC_POL_SYS_ID
    WHERE c.PRC_CODE = 'MOT-CVR-010'
    GROUP BY c.PRC_POL_SYS_ID
)
SELECT
    d.VALUATION_DATE                    AS VALUATION_DATE,
    FLOOR(SUM(d.EARNED_EXPOSURE))       AS EARNED_EXPOSURE,
    FLOOR(SUM(d.UNEARNED_EXPOSURE))     AS UNEARNED_EXPOSURE,
    FLOOR(
         d.POLH_TO_DT
        - d.POLH_FM_DT + 1
    )                                   AS TOTAL,
    ROUND((((CASE
                 WHEN d.VALUATION_DATE < d.POLH_FM_DT
                 THEN 0
                 ELSE LEAST(
                          (d.VALUATION_DATE - d.POLH_FM_DT + 1),
                          (d.POLH_TO_DT - d.POLH_FM_DT + 1)
                      )
             END)/NULLIF(FLOOR(d.POLH_TO_DT - d.POLH_FM_DT + 1),0) )*100),2)||'%' AS EARNED_EXPOSURE_RATIO,
    d.POLH_SYS_ID                       AS SYS_ID,
    d.POLH_NO                           AS POL_NO,
    d.POLH_PROD_CODE                    AS PRODUCT_CODE,
    prod.PROD_DESC                      AS PROD_NAME,
    d.POLH_DEPT_CODE                    AS DEPT_CODE,
    d.AD_ANLY_CODE_1                    AS ANLY_CODE_1,
    d.AD_ANLY_CODE_2                    AS ANLY_CODE_2,
    d.POLH_SRC_CODE                     AS SRC_CODE,
    src_cust.CUST_NAME                  AS SOURCE_NAME,
    d.POLH_SRC_TYPE                     AS SRC_TYP,
    DECODE(d.POLH_SRC_TYPE,'1','DIRECT','2','AGENT','BROKER') AS SOURCE_TYPE,
    d.POLH_BUS_TYPE                     AS BUS_TYPE,
    CASE WHEN d.POLH_BUS_TYPE = '1' THEN 'Direct without coinsurance'
         WHEN d.POLH_BUS_TYPE = '1' THEN 'Direct without coinsurance'
         END                            AS BUS_TYPE_VALUES,
    d.POLH_INST_YN                      AS INSTALLMENT_FLAG,
    d.POLH_INSTL_METHOD                 AS INSTALLMENT_METHOD,
    CASE WHEN d.POLH_INSTL_METHOD = '1' OR d.POLH_INSTL_METHOD = '01' THEN 'Monthly'
         WHEN d.POLH_INSTL_METHOD = '2' OR d.POLH_INSTL_METHOD = '02' THEN 'Bi-Monthly'
         WHEN d.POLH_INSTL_METHOD = '3' OR d.POLH_INSTL_METHOD = '03' THEN 'Quarterly'
         WHEN d.POLH_INSTL_METHOD = '4' OR d.POLH_INSTL_METHOD = '04' THEN 'Half-Yearly'
         WHEN d.POLH_INSTL_METHOD = '5' OR d.POLH_INSTL_METHOD = '05' THEN 'Annual'
         WHEN d.POLH_INSTL_METHOD = '6' OR d.POLH_INSTL_METHOD = '06' THEN 'Others'
         END                            AS INSTALLMENT_METHOD_VALUE,
    d.POLH_UW_YEAR                      AS UNDERWRITING_YEAR,
    d.AD_ACNT_YEAR                      AS ACCOUNTING_YEAR,
    d.POLH_TPA_CODE                     AS TPA_CODE,
    tpa_cust.CUST_NAME                  AS TPA_NAME,
    d.POLH_DIVN_CODE                    AS DIV_CODE,
    divn.DIVN_NAME                      AS DIV_NAME,
    d.POLH_FM_DT                        AS POL_FM_DATE,
    d.POLH_TO_DT                        AS POL_TO_DATE,
    (ROUND(((d.POLH_TO_DT - d.POLH_FM_DT + 1) / 365),0)) AS TERM_OF_POLICY,
    d.POLH_ASSR_CODE                    AS ASSR_CODE,
    d.POLH_ASSR_NAME                    AS ASSURED_NAME,
    CASE
        WHEN SUM(d.EARNED_EXPOSURE) <= 0
        THEN 0
        ELSE
            ROUND(
                (
                    d.COMMISSION_1
                    / NULLIF(FLOOR(d.POLH_TO_DT - d.POLH_FM_DT + 1), 4)
                )
                *
                GREATEST(
                    0,
                    LEAST(
                        d.VALUATION_DATE,
                        d.POLH_TO_DT
                    )
                    -
                    GREATEST(
                        d.POLH_FM_DT,
                        TRUNC(d.VALUATION_DATE,'MM')
                    )
                    + 1
                )
            ,2)
    END                                 AS CURR_MONTH_COMMISION,
    CASE
        WHEN SUM(d.EARNED_EXPOSURE) <= '0'
             THEN 0
        ELSE d.COMMISSION
    END                                 AS CUM_EARNED_COMMISION,
    CASE
        WHEN SUM(d.EARNED_EXPOSURE) <= '0'
             THEN d.COMMISSION_1
        ELSE (d.COMMISSION_1 - d.COMMISSION)
    END                                 AS CUM_UNEARNED_COMMISION,
    d.COMMISSION_1                      AS TOTAL_COMMISION,
    CASE
        WHEN SUM(d.EARNED_EXPOSURE) <= 0
        THEN 0
        ELSE
            ROUND(
                (
                    d.TPA_COMMISSION_1
                    / NULLIF(FLOOR(d.POLH_TO_DT - d.POLH_FM_DT + 1), 0)
                )
                *
                GREATEST(
                    0,
                    LEAST(
                        d.VALUATION_DATE,
                        d.POLH_TO_DT
                    )
                    -
                    GREATEST(
                        d.POLH_FM_DT,
                        TRUNC(d.VALUATION_DATE,'MM')
                    )
                    + 1
                )
            ,2)
    END                                 AS CURR_MONTH_TPA_COMMISION,
    CASE
        WHEN SUM(d.EARNED_EXPOSURE) <= '0'
             THEN 0
        ELSE d.TPA_COMMISSION
    END                                 AS CUM_EARNED_TPA_COMMISSION,
    CASE
        WHEN SUM(d.EARNED_EXPOSURE) <= '0'
             THEN d.TPA_COMMISSION_1
        ELSE (d.TPA_COMMISSION_1 - d.TPA_COMMISSION)
    END                                 AS CUM_UNEARNED_TPA_COMMISSION,
    d.TPA_COMMISSION_1                  AS TOTAL_TPA_COMMISION,
    SUM(d.CURR_PREM_1)                  AS UNEARNED_PREM_YEAR_1,
    SUM(d.CURR_PREM_2)                  AS UNEARNED_PREM_YEAR_2,
    SUM(d.CURR_PREM_3)                  AS UNEARNED_PREM_YEAR_3,
    SUM(d.CURR_PREM_4)                  AS UNEARNED_PREM_YEAR_4_PLUS,
    SUM(d.CURR_MONTH_PREM_EP)           AS CURR_MONTH_PREM_EP,
    CASE
        WHEN SUM(d.EARNED_EXPOSURE) <= '0'
             THEN 0
        ELSE ROUND((SUM(d.TOT_PREM)/FLOOR(d.POLH_TO_DT - d.POLH_FM_DT + 1))*d.FY_EARNED_PREMIUM,2)
    END                                 AS CURR_FY_EP,
    SUM(d.EARNED_PREMIUM)               AS CUM_EARNED_PREMIUM,
    SUM((d.CURR_PREM_1 + d.CURR_PREM_2 + d.CURR_PREM_3 + d.CURR_PREM_4)) AS CUM_UNEARNED_PREMIUM_TOTAL,
    SUM(d.TOT_PREM)                     AS TOTAL_PREMIUM,
    CASE WHEN d.POLH_PROD_CODE IN ('MOT-POS-012','MOT-POS-013','MOT-PRD-012','MOT-PRD-013') AND d.AD_ANLY_CODE_1 = 'OICL_MOTOD' THEN
         0
         ELSE
         d.LONG_TERM_PREMIUM
         END                            AS LONG_TERM_PREMIUM,
    CASE WHEN d.POLH_PROD_CODE IN ('MOT-POS-012','MOT-POS-013','MOT-PRD-012','MOT-PRD-013') AND d.AD_ANLY_CODE_1 = 'OICL_MOTOD' THEN
         0
         ELSE
         d.ADVANCE_PREMIUM
         END                            AS ADVANCE_PREMIUM,
    CASE WHEN d.POLH_PROD_CODE IN ('MOT-POS-012','MOT-POS-013','MOT-PRD-012','MOT-PRD-013') AND d.AD_ANLY_CODE_1 = 'OICL_MOTOD' THEN
         SUM(d.TOT_PREM)
         ELSE
         (NVL(d.POLH_PREM_LC_1,0) +
          NVL(d.POLH_ORG_PREM_LC_1,0))
         END                            AS EP_CUM_EXPECTED_PREM,
    CASE WHEN d.POLH_PROD_CODE IN ('MOT-POS-012','MOT-POS-013','MOT-PRD-012','MOT-PRD-013') AND d.AD_ANLY_CODE_1 = 'OICL_MOTTP' THEN
         cpa.PRAI_YN_24
         ELSE '0'
         END                            AS CPA_FLAG,
    CASE WHEN d.POLH_PROD_CODE IN ('MOT-POS-012','MOT-POS-013','MOT-PRD-012','MOT-PRD-013') AND d.AD_ANLY_CODE_1 = 'OICL_MOTTP' THEN
         pa.PRC_PREM_LC_1
         ELSE 0
         END                            AS PA_PREMIUM
FROM detail d
LEFT JOIN PGIM_PRODUCT prod
  ON prod.PROD_CODE = d.POLH_PROD_CODE
LEFT JOIN PCOM_CUSTOMER src_cust
  ON src_cust.CUST_CODE = d.POLH_SRC_CODE
LEFT JOIN PCOM_CUSTOMER tpa_cust
  ON tpa_cust.CUST_CODE = d.POLH_TPA_CODE
LEFT JOIN FM_DIVISION divn
  ON divn.DIVN_CODE = d.POLH_DIVN_CODE
LEFT JOIN cpa_flag cpa
  ON cpa.PRAI_POL_SYS_ID = d.POLH_SYS_ID
LEFT JOIN pa_premium pa
  ON pa.PRC_POL_SYS_ID = d.POLH_SYS_ID
GROUP BY
    d.POLH_SYS_ID,
    d.POLH_NO,
    d.POLH_END_NO_IDX,
    d.POLH_DEPT_CODE,
    d.AD_ANLY_CODE_1,
    d.AD_ANLY_CODE_2,
    d.POLH_FM_DT,
    d.POLH_TO_DT,
    d.POLH_ASSR_CODE,
    d.POLH_DIVN_CODE,
    d.POLH_PREM_LC_1,
    d.POLH_ORG_PREM_LC_1,
    d.POLH_PROD_CODE,
    d.POLH_ASSR_NAME,
    d.POLH_SRC_TYPE,
    d.POLH_UW_YEAR,
    d.POLH_SRC_CODE,
    d.POLH_TPA_CODE,
    d.AD_ACNT_YEAR,
    d.POLH_BUS_TYPE,
    d.POLH_INST_YN,
    d.POLH_INSTL_METHOD,
    d.COMMISSION_1,
    d.TPA_COMMISSION_1,
    d.LONG_TERM_PREMIUM,
    d.ADVANCE_PREMIUM,
    d.COMMISSION,
    d.TPA_COMMISSION,
    d.VALUATION_DAYS,
    d.EP_EXPECTED_PREM_CURR_MONTH,
    d.FY_EARNED_PREMIUM,
    -- Constant for the whole statement, and the six lookups below are
    -- functionally dependent on grouping columns already listed, so none of
    -- these entries can change the grouping.
    d.VALUATION_DATE,
    prod.PROD_DESC,
    src_cust.CUST_NAME,
    tpa_cust.CUST_NAME,
    divn.DIVN_NAME,
    cpa.PRAI_YN_24,
    pa.PRC_PREM_LC_1;

SHOW ERRORS VIEW PGIS_POLICY_DTL_V
