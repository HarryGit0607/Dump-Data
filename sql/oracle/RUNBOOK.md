# PGIS_POLICY_DTL — runbook

The whole activity is three commands. Everything below that is detail for when
something needs attention.

```sql
sqlplus user/password@db

SQL> @00_preflight.sql                                      -- once, ~5 seconds
SQL> @install.sql                                           -- once, ~1 minute
SQL> EXEC PGIS_POLICY_DTL_LOAD.load_month(DATE '2026-03-31'); -- each month
```

---

## Deploy

### 1. Pre-flight

```sql
@00_preflight.sql
```

Read two things from the output:

- **Any privilege marked `*** MISSING ***`.** `CREATE ANY CONTEXT` is the one a
  schema usually lacks; a DBA runs `02_context.sql`, which is a single statement,
  and the rest installs as you.
- **`Partitioning`.** If it says `TRUE`, carry on. If it says `FALSE` — Standard
  Edition, or the option is not licensed — edit `install.sql` to source
  `04a_table_no_partitioning.sql` in place of `04_table.sql`. Nothing else
  changes; the load detects which shape the table is and clears a month the
  right way for it.

Also check the source objects are all `ok`, and that the default tablespace has
room. If the table should live somewhere specific, add `TABLESPACE <name>` to the
`CREATE TABLE` in `04_table.sql` before installing.

### 2. Install

```sql
@install.sql
```

Creates the load package, the valuation-date context, the report view
`PGIS_POLICY_DTL_V`, the table `PGIS_POLICY_DTL` and its load log. It does not
load any data and does not run the report, so it takes about a minute.

If it stops, it stops on the failing statement (`WHENEVER SQLERROR EXIT`) and
nothing after that point was created. Fix and re-run — every script is
re-runnable except the `CREATE TABLE`s in `04`, which will say the object
already exists.

### 3. Load and check one month by hand

```sql
EXEC PGIS_POLICY_DTL_LOAD.load_month(DATE '2026-03-31');
@07_verify.sql
```

This is the run that takes real time. Reconcile the row count and
`TOTAL_PREMIUM` against whatever you check the report against today before going
any further.

### 4. Backfill, if you want history

```sql
BEGIN
    PGIS_POLICY_DTL_LOAD.backfill(DATE '2025-04-30', DATE '2026-02-28');
END;
/
```

Each month commits on its own, so an interruption keeps the months it finished.

### 5. Schedule it

```sql
@06_schedule.sql
```

Runs at 02:00 on the 1st of each month and loads the month that has just ended.

---

## Operate

| I want to | Do |
| --- | --- |
| Load last month now | `EXEC PGIS_POLICY_DTL_LOAD.load_month;` |
| Load one specific month | `EXEC PGIS_POLICY_DTL_LOAD.load_month(DATE '2026-04-30');` |
| Re-run a month | The same command again. It replaces that month. |
| Run gently on a busy box | `EXEC PGIS_POLICY_DTL_LOAD.load_month(DATE '2026-04-30', p_parallel => 2);` |
| See what has run | `SELECT * FROM PGIS_POLICY_DTL_LOG ORDER BY RUN_ID DESC;` |
| Check a load | `@07_verify.sql` |
| Query the report live, no load | `EXEC PGIS_POLICY_DTL_LOAD.set_valuation_date(DATE '2026-04-30');` then `SELECT * FROM PGIS_POLICY_DTL_V;` |
| Pause the schedule | `EXEC DBMS_SCHEDULER.DISABLE('PGIS_POLICY_DTL_MONTHLY');` |
| Remove everything | `@99_uninstall.sql` |

**Re-running a month is safe.** `load_month` clears that month before loading it
— truncating its partition, or deleting its rows on an unpartitioned table — so
running it twice gives you one copy, not two. Other months are untouched.

---

## When something goes wrong

**Is it still running, or stuck?**

```sql
SELECT * FROM USER_SCHEDULER_RUNNING_JOBS;

SELECT SID, SERIAL#, EVENT, SECONDS_IN_WAIT, SQL_ID
  FROM V$SESSION WHERE MODULE LIKE 'DBMS_SCHEDULER%';

SELECT SID, MESSAGE, ROUND(SOFAR/NULLIF(TOTALWORK,0)*100) AS PCT, TIME_REMAINING
  FROM V$SESSION_LONGOPS WHERE TIME_REMAINING > 0;
```

**A run failed.** The reason is in the log, which is written outside the load
transaction and so survives the rollback:

```sql
SELECT VALUATION_DATE, STATUS, ERROR_TEXT
  FROM PGIS_POLICY_DTL_LOG WHERE STATUS = 'FAILED' ORDER BY RUN_ID DESC;
```

A failed run leaves the month empty, not half loaded. Fix the cause and run
`load_month` for that date again.

| Error | Cause | Fix |
| --- | --- | --- |
| `ORA-01843: not a valid month` | A date string reached `TO_DATE` in the wrong language | Pass a `DATE` literal, not a string: `load_month(DATE '2026-03-31')` |
| `ORA-01536: space quota exceeded` | No room in the tablespace | Quota or space; the load is restartable |
| `ORA-12838: cannot read/modify after modification` | Something read the table inside the direct-path transaction | Only `load_month` should write; it commits straight after |
| `ORA-14400: inserted key does not map to any partition` | `VALUATION_DATE` came out NULL — the context was not set | Always go through `load_month`, never `INSERT` from the view by hand |
| `ORA-01555: snapshot too old` | Long-running query against a busy source | Larger undo retention, or run it when the source is quieter |
| Loads, but returns no rows | The valuation date context was not set for the session | Only reachable by selecting from the view directly; use `set_valuation_date` first |

**The load got much slower.** Compare against the history — `PGIS_POLICY_DTL_LOG`
records the duration of every run, so a regression is visible rather than
suspected. Then check statistics on the source tables, and that the indexes
listed at the top of `03_report_view.sql` still exist.

---

## What to watch each month

1. `PGIS_POLICY_DTL_LOG` has one `SUCCESS` row for the month.
2. The row count is in line with previous months — `07_verify.sql` prints the
   percentage change against the month before, which is what catches a source
   feed that arrived late or half-loaded.
3. The duration is in line with previous months.

The one thing to keep an eye on over a longer horizon: `CAY_ACNT_YEAR = '24'` in
the report is fixed and does not roll forward with the valuation date. Confirm
that is intended before the first run of the next accounting year.
