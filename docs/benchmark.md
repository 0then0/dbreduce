# Benchmark

For opt-in schema runs, record `initial_sql_bytes`, `final_sql_bytes`,
`schema_reduction.initial` and `.final`, and
`schema_reduction.candidates` from the JSON report. These SQL byte counts use
the same plain dump format. `performance.phases.schema_reduction` measures the
added schema phase; nested planning, restore, and oracle timings must not be
summed with it. The v0.3 measurements below use data-only mode.

## v0.4 opt-in schema runs

On 2026-09-28, complete clone-backend runs with `--reduce-schema` and two
structured-oracle confirmations were performed on PostgreSQL 17.11. Both
published SQL files were separately restored with `psql` and reproduced the
same application oracle signature.

| Case          |        Rows |  Tables |    Plain SQL bytes | Schema phase |    Total |
| ------------- | ----------: | ------: | -----------------: | -----------: | -------: |
| Wagtail #9208 | 150,706 → 3 |  48 → 1 | 11,823,405 → 3,714 |      67.096s | 179.984s |
| NetBox #17498 |  21,381 → 2 | 180 → 4 | 2,738,171 → 11,965 |     403.791s | 698.040s |

The NetBox schema search had 70 oracle infrastructure errors and does not
claim local irreducibility. The Wagtail run did. These are individual
end-to-end measurements, not repeated performance comparisons. The Wagtail
and NetBox cases describe the datasets and oracle limits.

The v0.4 data-only control on the same Wagtail source, with the clone backend,
`--confirm 2`, and no concurrent integration tests, took 55.229s. It retained
48 tables, reduced 150,706 rows to 3, and used 36 oracle executions. The v0.3
clone median below was 59.610s. One v0.4 run establishes functional parity;
it is not enough to claim a performance improvement.

## v0.3 Wagtail comparison

Repeated on 2026-09-28 with DBReduce source at commit `6c10073` (Python source
digest `d0a1c9438f3b63140879c69ecc784efa2caa62d7d977defc10e0cb961e53b2c6`). Each
variant ran three times, sequentially, against the same PostgreSQL 17.11 source,
Wagtail 8.0 oracle, and `--confirm 2`. The source had 150,706 rows, including
generated application noise; it was not a production dump. Hardware was a MacBook
Air M1 (8 cores, 8 GB RAM, internal SSD), macOS 15.7.9, Docker Engine 29.8.0.
Python was 3.14.7. The PostgreSQL 18.6 client wrappers started a fresh Docker
container for each `pg_dump`/`pg_restore`; timings compare those complete
workflows and include container startup.

| Backend            |    Run 1 |    Run 2 |    Run 3 |   Median |            Range |
| ------------------ | -------: | -------: | -------: | -------: | ---------------: |
| Snapshot, jobs=1   | 143.539s | 115.981s | 114.422s | 115.981s | 114.422–143.539s |
| Snapshot, jobs=4   | 105.025s | 108.337s | 104.086s | 105.025s | 104.086–108.337s |
| Clone, `WAL_LOG`   |  58.990s |  60.407s |  59.610s |  59.610s |   58.990–60.407s |
| Clone, `FILE_COPY` |  58.067s |  58.716s |  57.790s |  58.067s |   57.790–58.716s |

All 12 runs reduced the database to 3 rows in `wagtailcore_collection`, retained
the same failure identity, and passed final logical restore confirmation. Snapshot
had 21 candidate dumps and 21 fingerprint dumps per run; clone had 54 database
clones, 21 candidates, and 36 oracle executions. All clone-created databases
were dropped; no clone failures, fallbacks, or cleanup failures were reported.

Each cell gives the median total seconds; counts in the header follow the same
backend order as the columns:

| Operation                              | Snapshot jobs=1 | Snapshot jobs=4 | Clone `WAL_LOG` | Clone `FILE_COPY` |
| -------------------------------------- | --------------: | --------------: | --------------: | ----------------: |
| Restore (78 / 78 / 9 / 9)              |          51.428 |          43.063 |           7.459 |             7.437 |
| Database create (78 / 78 / 9 / 9)      |           2.266 |           2.252 |           0.241 |             0.251 |
| Database clone (0 / 0 / 54 / 54)       |               0 |               0 |           4.639 |             4.998 |
| Candidate deletion (21 / 21 / 21 / 21) |           9.392 |           9.377 |           9.061 |             9.383 |
| Candidate dump (21 / 21 / 0 / 0)       |           8.878 |           8.470 |               0 |                 0 |
| Fingerprint dump (21 / 21 / 0 / 0)     |           8.275 |           8.412 |               0 |                 0 |
| Oracle preparation (33 / 33 / 36 / 36) |          24.500 |          20.300 |           7.077 |             7.486 |
| Oracle process (33 / 33 / 36 / 36)     |          26.035 |          25.063 |          26.847 |            26.232 |
| Database cleanup (78 / 78 / 63 / 63)   |           1.448 |           1.632 |           3.002 |             1.053 |

The phase totals overlap: for example, restore is included in oracle preparation.
Do not sum the rows. Reports include execution averages and the remaining
instrumented phases. On this workload, parallel restore improved snapshot median
time by about 9%; clone reduced the median by 43–45% compared with snapshot at
jobs=4. `FILE_COPY` was 1.5s faster at the median than `WAL_LOG`, but three runs
do not establish a reliable winner between clone strategies. Clone remains
opt-in; these results cover one application dataset and environment.

PostgreSQL 18 `FILE_COPY` with `file_copy_method=CLONE` passed the CREATE DATABASE
API probe, but reflink was unsupported on the container's PostgreSQL data
filesystem (`cp --reflink=always` returned “Operation not supported”). No
filesystem-clone timing is claimed. A full PostgreSQL 18 Wagtail reduction was
also completed in an earlier development run; see [validation history](real-world-validation.md).

The NetBox v4.1.1 case using the official NetBox 4.1 demo SQL dump completed one
full reduction on PostgreSQL 17: 21,381 to 2 rows in 262.958s, with 113 oracle
executions and a successful fresh SQL restore. The issue reporter's exact
database is unavailable. NetBox setup and negative control partly used raw SQL
because delete signals depend on Redis; the positive oracle exercised the real
form path, not a full HTTP flow. See the [NetBox case](cases/netbox-17498.md).

## Reproducible demo benchmark

The demo benchmark uses 120,005 relational rows and the structured oracle in
`examples/oracle.py`. Run it only against a newly created disposable database:

```bash
createdb dbreduce_benchmark
psql -X -v ON_ERROR_STOP=1 -d dbreduce_benchmark -f examples/benchmark.sql
uv run dbreduce reduce \
  --database postgresql://localhost/dbreduce_benchmark \
  --oracle 'uv run python examples/oracle.py' --oracle-json --confirm 2 \
  --output benchmark.min.sql --report benchmark.report.json
```

Record the hardware, PostgreSQL server and client versions, oracle, confirmation
count, and revision. Repeat each variant three times. Confirm the exported SQL
with a fresh restore and the same oracle. The report contains phase counts,
totals, and per-execution averages under `performance.phases`.
