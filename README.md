# Dump-Data

Dumping the result of a large, slow query into a table.

Two things live here:

- **[`sql/oracle/`](sql/oracle)** — the monthly load of the earned / unearned
  exposure report into `PGIS_POLICY_DTL`. This is the answer to the question
  that prompted the repository.
- **[`dumpdata/`](dumpdata)** — a small Python package for the general case,
  when the query and the table are *not* in the same database.

---

## The short answer

Your query and your new table are both in Oracle, so **no row should ever leave
the database**. Let the server write the result set itself:

```sql
INSERT /*+ APPEND */ INTO PGIS_POLICY_DTL
SELECT * FROM PGIS_POLICY_DTL_V;
```

That single change is where most of the two hours goes. Fetching a few lakh
rows into a client and inserting them back is a round trip per array of rows,
a datatype conversion per column and a network hop for each — none of which
happens when the engine does the writing. What remains is the cost of the join
itself.

The column names look after themselves too. `CREATE TABLE ... AS SELECT` gives
the new table the select list's own names and datatypes, so `PGIS_POLICY_DTL`
comes out with the report's columns and nothing has to be typed out by hand.

Your query is already in good shape for this: all **54 output columns are
uniquely aliased** (`VALUATION_DATE`, `EARNED_EXPOSURE`, ... `PA_PREMIUM`) and
none exceeds 30 characters. That matters because a *result set* is allowed to
repeat a column name but a *table* is not — a `SELECT c.*, o.*` across several
tables would have failed here with `ORA-00957: duplicate column name`. Yours
will not.

## What is in `sql/oracle/`

| Script | What it does |
| --- | --- |
| `01_package_spec.sql` | `PGIS_POLICY_DTL_LOAD` — the load API |
| `02_context.sql` | application context carrying the valuation date |
| `03_report_view.sql` | `PGIS_POLICY_DTL_V` — your query, date parameterised |
| `04_table.sql` | `PGIS_POLICY_DTL`, partitioned by month, plus the load log |
| `05_package_body.sql` | the load itself |
| `06_schedule.sql` | monthly `DBMS_SCHEDULER` job |
| `07_verify.sql` | post-load checks and reconciliation |
| `08_backfill.sql` | load the months already behind you |

```bash
sqlplus user/password@db @sql/oracle/install.sql
```

```sql
EXEC PGIS_POLICY_DTL_LOAD.load_month(DATE '2026-03-31');   -- one month
EXEC PGIS_POLICY_DTL_LOAD.load_month;                      -- the month just ended
```

Then `@sql/oracle/07_verify.sql`, and once a month looks right,
`@sql/oracle/06_schedule.sql` to put it on the calendar.

### The three decisions worth knowing about

**The query became a view, and the valuation date comes from a context.**
You are going to run this every month end, so `'31-MAR-2026'` cannot stay in the
text. A view cannot take a parameter, but `SYS_CONTEXT` is a constant the
optimizer folds into the plan exactly like a literal, so
`POLH_TO_DT >= <valuation date>` remains an ordinary, index-usable predicate.
The date is stored as `YYYY-MM-DD`, which also removes a live hazard:
`TO_DATE('31-MAR-2026','DD-MON-YYYY')` raises `ORA-01843` in any session whose
`NLS_DATE_LANGUAGE` is not English, and a scheduler job does not inherit your
session's settings.

The single definition is then used everywhere — the table is created from it,
the monthly insert reads it, and so can you:

```sql
EXEC PGIS_POLICY_DTL_LOAD.set_valuation_date(DATE '2026-03-31');
SELECT * FROM PGIS_POLICY_DTL_V;
```

**The table is interval-partitioned by month on `VALUATION_DATE`.** Each run
writes one partition. Re-running March truncates the March partition alone and
leaves every other month untouched, which makes a re-run safe at any time and
turns "undo a bad load" into an instant operation rather than a `DELETE` of
millions of rows. Partitions appear on their own as new months arrive, queries
for one month read one partition, and old months can be dropped one at a time.

**The load is direct path.** `INSERT /*+ APPEND */` writes formatted blocks
straight above the segment's high water mark: no search for free space, no
buffer cache, and with the table `NOLOGGING`, almost no redo. Parallel DML is
enabled explicitly, because it is off in every session by default and without it
the insert runs single-threaded however parallel the query underneath is.

Two consequences to be aware of. A direct-path table cannot be read again in the
same transaction (`ORA-12838`), which is why the package commits immediately
after the insert. And the table is unrecoverable from an archive-log restore
until the next backup — the right trade here, since the content can always be
rebuilt by re-running the month, but note that a database in `FORCE LOGGING`
(usual with a physical standby) ignores `NOLOGGING` and the load will be fully
logged.

### Your query is unchanged

The view is your tuned query character-for-character, with only the date
expression swapped. That is not a claim, it is
[a test](tests/test_oracle_sql.py): it puts the literal back in place of the
context lookup, strips comments and whitespace from both, and requires the two
to be identical. The quirks you flagged as deliberate — `NULLIF(..., 4)`, the
repeated `POLH_BUS_TYPE = '1'` branch, the `<= '0'` string comparisons, the
`+ 2` earned-exposure variant — are asserted individually as well, because they
are exactly the things that get tidied up by accident.

### Three things in the query worth a second look

None of these were changed, because they are business decisions rather than
performance ones. They are worth confirming before the job runs unattended:

- **`CAY_ACNT_YEAR = '24'`** anchors the `AD_DOC_DT` cut-off to accounting year
  24. It does not roll forward, so a run for March 2027 still reads from the
  AY24 start date. If that is meant to be cumulative, it is correct as it
  stands; if it is meant to follow the valuation date, it needs deriving.
- **`AD_DIVN_CODE` / `LS_OFFICE_CODE = '411600'`** restricts the report to one
  division.
- **`POLH_TO_DT >= <valuation date>`** keeps only policies still in force at the
  valuation date, so each month's partition is a snapshot of the live book
  rather than of everything ever written.

### If the join itself is what takes two hours

Once the client round trip is gone, whatever is left is the query. In order of
usual payoff:

1. Get the plan for one month —
   `SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, 'ALLSTATS LAST'))`
   after running with `/*+ GATHER_PLAN_STATISTICS */` — and compare estimated
   against actual rows. The first place they diverge badly is the problem.
2. Create the indexes listed at the top of `03_report_view.sql` if their
   equivalents do not exist. The one to add for the monthly run specifically is
   `PGITH_POLICY (POLH_TO_DT)`, which is now the driving predicate.
3. Raise the degree of parallelism: `load_month(DATE '2026-03-31', p_parallel => 16)`.
4. Check that statistics on the six source tables are current. A two-hour join
   is very often a nested loop the optimizer chose from a stale row estimate.

And a scheduled job that takes two hours at 2 a.m. is not the same problem as an
interactive query that takes two hours. Getting it off a person's screen may be
most of the fix.

---

## The general case: `dumpdata`

For when the target table is in a *different* database from the query, where
`INSERT ... SELECT` is not available. Pure standard library; bring your own
DB-API driver.

```bash
# What will this query do to a table? Any repeated column names?
python -m dumpdata columns --driver oracledb --dsn user/pw@db \
    --dialect oracle --query @report.sql

# Print the same-database statement rather than moving anything
python -m dumpdata plan --dialect oracle --query @report.sql \
    --table PGIS_POLICY_DTL --nologging --parallel 8

# Copy between two databases, in resumable batches
python -m dumpdata copy \
    --source-driver oracledb  --source-dsn user/pw@db  --source-dialect oracle \
    --target-driver psycopg2  --target-dsn "host=dw dbname=reporting" --target-dialect postgresql \
    --query @report.sql --table pgis_policy_dtl \
    --mode keyset --key-column sys_id --batch-size 50000 \
    --checkpoint /var/tmp/pgis.checkpoint.json

# Or export once and let the target's own bulk loader take it
python -m dumpdata csv --driver oracledb --dsn user/pw@db --dialect oracle \
    --query @report.sql --out /var/tmp/pgis.csv --rows-per-file 1000000 \
    --table pgis_policy_dtl --load-dialect postgresql
```

The same three points the Oracle scripts are built on apply, only by hand:

- **Repeated column names.** A join returning `c.*, o.*` gives you two `id`
  columns. `dumpdata` keeps both, renaming the second to `id_2`, and reports
  what it renamed. Pass `--strict-columns` to be told to alias them yourself
  instead.
- **Reading.** `--mode stream` runs the query once and pulls it with
  `fetchmany`, which is what you want when the query is the slow part, but it
  cannot resume. `--mode keyset` pages by a unique key and *can* resume from a
  checkpoint, but re-runs the query per batch — only sane when one batch is
  cheap. For a slow join, materialise it into a staging table first, then page
  off that. Non-unique paging keys are rejected, because they silently skip rows
  that straddle a batch boundary.
- **Writing.** Rows go in batches with a commit each, so a multi-hour load never
  builds one enormous transaction, and a dropped connection is retried rather
  than fatal.

```python
from dumpdata import CopyOptions, Source, Target, copy_query_to_table

result = copy_query_to_table(
    Source(oracle_connection, query, dialect="oracle"),
    Target(postgres_connection, "pgis_policy_dtl", dialect="postgresql"),
    CopyOptions(batch_size=50_000, progress=print),
)
print(f"{result.rows_copied:,} rows at {result.rows_per_second:,.0f}/s")
```

## Tests

```bash
cd tests && python3 -m unittest discover
```

129 tests, no database required: the copier runs against SQLite, and the Oracle
scripts are parsed with an Oracle-dialect parser and checked structurally.
`sqlglot` is needed for the Oracle tests (`pip install sqlglot`); everything
else is standard library.
