--------------------------------------------------------------------------------
-- Install the monthly PGIS_POLICY_DTL dump.
--
--     sqlplus user/password@db @00_preflight.sql    -- check first
--     sqlplus user/password@db @install.sql
--
-- Creates, in order: the load package spec, the valuation-date context, the
-- report view, the partitioned table and its load log, and the package body.
-- It loads no data and does not run the report, so it takes about a minute.
-- The scheduler job (06) and the backfill (08) are deliberately left out --
-- run those once you have loaded and checked a month by hand.
--
-- If 00_preflight.sql reports Partitioning as FALSE, change the 04 line below
-- to 04a_table_no_partitioning.sql. Nothing else needs to change.
--
-- Privileges needed in the target schema:
--     CREATE TABLE, CREATE VIEW, CREATE SEQUENCE, CREATE PROCEDURE
--     CREATE ANY CONTEXT   (script 02 only; a DBA can run that one statement)
--     CREATE JOB           (script 06 only)
-- plus SELECT on the report's source tables.
--
-- Stops on the first error and leaves everything after it uncreated, so a
-- failed install is fixed and re-run rather than unpicked. To back the whole
-- thing out, use 99_uninstall.sql.
--------------------------------------------------------------------------------
SET ECHO ON
SET SQLBLANKLINES ON
SET DEFINE OFF
WHENEVER SQLERROR EXIT SQL.SQLCODE

@@01_package_spec.sql
@@02_context.sql
@@03_report_view.sql
@@04_table.sql
-- Partitioning not available? Use this line instead of the one above:
-- @@04a_table_no_partitioning.sql
@@05_package_body.sql

WHENEVER SQLERROR CONTINUE
SET ECHO OFF

PROMPT
PROMPT Installed. Load and check one month before scheduling anything:
PROMPT
PROMPT     EXEC PGIS_POLICY_DTL_LOAD.load_month(DATE '2026-03-31');
PROMPT     @07_verify.sql
PROMPT
PROMPT Then, for the recurring run:
PROMPT
PROMPT     @06_schedule.sql
PROMPT
PROMPT Day to day: see RUNBOOK.md
PROMPT
