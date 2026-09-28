-- Office 771001 slice of PGIPH_STG_TPA_UPLOAD (Qc).
-- Office code = the token before the first '/' in PSTU_POL_NO.
-- Example: 771001/48/2013/371 → 771001
--
-- Run after connecting:
--   sqlplus P10_DEMO@//10.0.0.18:1532/Qc
-- or:
--   python -m dumpdata dashboard

SELECT
    SUBSTR(PSTU_POL_NO, 1, INSTR(PSTU_POL_NO || '/', '/') - 1) AS office_code,
    COUNT(*) AS claim_rows,
    COUNT(DISTINCT PSTU_POL_NO) AS policies
FROM PGIPH_STG_TPA_UPLOAD
WHERE SUBSTR(PSTU_POL_NO, 1, INSTR(PSTU_POL_NO || '/', '/') - 1) = '771001'
GROUP BY SUBSTR(PSTU_POL_NO, 1, INSTR(PSTU_POL_NO || '/', '/') - 1);

-- Year-wise (3rd slash token of the policy number)
SELECT
    REGEXP_SUBSTR(PSTU_POL_NO, '[^/]+', 1, 3) AS pol_year,
    COUNT(*) AS claims,
    COUNT(DISTINCT PSTU_POL_NO) AS policies
FROM PGIPH_STG_TPA_UPLOAD
WHERE SUBSTR(PSTU_POL_NO, 1, INSTR(PSTU_POL_NO || '/', '/') - 1) = '771001'
GROUP BY REGEXP_SUBSTR(PSTU_POL_NO, '[^/]+', 1, 3)
ORDER BY 1;

-- Repeated claims: same policy number more than once
SELECT PSTU_POL_NO, COUNT(*) AS claims
FROM PGIPH_STG_TPA_UPLOAD
WHERE SUBSTR(PSTU_POL_NO, 1, INSTR(PSTU_POL_NO || '/', '/') - 1) = '771001'
GROUP BY PSTU_POL_NO
HAVING COUNT(*) > 1
ORDER BY COUNT(*) DESC;

-- Column inventory for this Qc build
SELECT column_id, column_name, data_type, nullable
FROM user_tab_columns
WHERE table_name = 'PGIPH_STG_TPA_UPLOAD'
ORDER BY column_id;
