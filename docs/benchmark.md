# Benchmark and validation results

## v0.3 development evidence (unreleased)

This series used the v0.3 working tree based on commit `55ac5f6`, with the same
source code throughout the 12 runs. It is not a measurement of a tagged v0.3.0
release. The benchmark source-tree digest was
`2a3fcceeb4512d7e9df22a9e7c1419058cbc8b6475bcf48e6b0ba76c76b79f20`
(`find src -name '*.py' -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256`).
The Wagtail #9208 source was rebuilt from the validation scripts found
under `/private/tmp/dbreduce-validation/wagtail`: real Wagtail 8.0 schema and bug,
150,706 rows including generated application noise, not a production dump. The
original v0.2 database was no longer available, so the old v0.2 times below are
historical context rather than a controlled before/after comparison.

Every run used the same rebuilt source database, Wagtail oracle, `--confirm 2`,
MacBook Air M1 (8 cores, 8 GB RAM, internal SSD), macOS 15.7.9, Docker Desktop,
PostgreSQL 17.11 server, PostgreSQL 18.6 client tools, and Python 3.14.7.
The PostgreSQL client tools were host wrappers that started a fresh
`postgres:18-trixie` Docker container for each `pg_dump` or `pg_restore`
invocation. Therefore dump and restore phase times include Docker process and
container startup. Both backends used the same wrappers, but snapshot performs
many more client invocations; these results compare the complete configured
workflows and do not isolate PostgreSQL restore throughput from wrapper cost.
The four variants were run in interleaved order (serial, parallel, WAL_LOG,
FILE_COPY), three repetitions each. All 12 runs reduced 150,706 rows to the
same 3 collection rows (including IDs and values), retained the required
failure identity, and passed final fresh logical restore confirmation.

| Backend / strategy | Restore jobs | Run 1 | Run 2 | Run 3 | Median | Range |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Snapshot | 1 | 113.510s | 116.719s | 105.557s | 113.510s | 105.557–116.719s |
| Snapshot | 4 | 104.880s | 101.953s | 100.422s | 101.953s | 100.422–104.880s |
| Clone / WAL_LOG | 1 | 57.534s | 55.721s | 56.937s | 56.937s | 55.721–57.534s |
| Clone / FILE_COPY | 1 | 57.156s | 53.951s | 53.649s | 53.951s | 53.649–57.156s |

Median phase totals below come from `performance.phases` in the reports. The
counts are the same in each run of a variant. `restore` includes accepted,
candidate, and oracle resets; `oracle_state_preparation` includes its restore or
clone; `database_clone` includes candidate and oracle clones. **These phase
figures overlap and must not be summed.** Each report also gives the average
per execution of every measured operation.

| Operation | Snapshot jobs=1 (count / median total) | Snapshot jobs=4 | Clone WAL_LOG | Clone FILE_COPY |
| --- | ---: | ---: | ---: | ---: |
| Source dump | 1 / 0.594s | 1 / 0.531s | 1 / 0.529s | 1 / 0.528s |
| Database create | 78 / 2.111s | 78 / 1.955s | 9 / 0.214s | 9 / 0.221s |
| Restore | 78 / 51.301s | 78 / 41.449s | 9 / 7.306s | 9 / 7.184s |
| Database clone | 0 | 0 | 52 / 3.719s | 52 / 4.618s |
| Candidate deletion | 21 / 9.556s | 21 / 9.758s | 21 / 9.388s | 21 / 9.430s |
| Candidate dump | 21 / 8.390s | 21 / 8.293s | 0 | 0 |
| Candidate state read | 22 / 2.949s | 22 / 2.985s | 23 / 2.705s | 23 / 2.658s |
| Fingerprint dump | 21 / 8.094s | 21 / 8.082s | 0 | 0 |
| Fingerprint hash | 21 / 0.170s | 21 / 0.166s | 0 | 0 |
| Oracle preparation | 33 / 23.622s | 33 / 19.862s | 34 / 6.468s | 34 / 6.766s |
| Oracle process | 33 / 25.893s | 33 / 24.155s | 34 / 24.884s | 34 / 23.968s |
| Database cleanup | 78 / 1.406s | 78 / 1.377s | 61 / 3.023s | 61 / 0.970s |
| Final normalization | 1 / 0.582s | 1 / 0.519s | 2 / 1.139s | 2 / 1.230s |
| Final export | 1 / 0.360s | 1 / 0.374s | 1 / 0.343s | 1 / 0.341s |

The repeated snapshot profile identifies reconstruction, candidate deletion,
candidate/fingerprint dumps, and oracle process execution as separate costs.
It does not infer dump/restore time from `total - oracle time`. Snapshot had one
exact-cache hit per run (1/21 lookups, 4.8%); the report estimates roughly
1.3–1.6 seconds saved per run. Exact fingerprint dumps cost about 8 seconds.
Clone disables this cache because its exact key costs more than this observed
benefit, without substituting requested deletions for database-state identity.

Parallel `pg_restore` improved this Wagtail workload but remains opt-in because
the default 1 worker preserves the established behavior. Clone was clearly
faster than either snapshot variant here; `WAL_LOG` and `FILE_COPY` ranges
overlap, so this data does not establish a reliable winner between them. Clone
stays opt-in while compatibility and cleanup behavior are assessed beyond these
cases. No parallel `pg_dump` or archive-format change was introduced.

PostgreSQL 18 `FILE_COPY` with `file_copy_method=CLONE` passed a PostgreSQL
CREATE DATABASE API probe, but `cp --reflink=always` failed with “Operation not
supported” on the container's PostgreSQL data filesystem. Actual block sharing
was not verified, so **filesystem CLONE is unsupported as a measured copy-on-write
contender in this environment**. It is not represented by an ordinary
`FILE_COPY` timing above.

Separately, a full PostgreSQL 18 Wagtail reduction completed in clone WAL_LOG
mode (150,706 to 3 rows, 61.395s, same identity and fresh logical restore).
The NetBox v4.1.1 form-path case completed in clone WAL_LOG mode on PostgreSQL
17 (1,323 to 2 rows, 47.511s, same identity and fresh logical restore). This
earlier database was reconstructed from available scripts, not obtained from
the issue reporter; its row count differed from another reconstructed 1,335-row
dataset. The exact database used by the issue author is not publicly available.
Separately, the official NetBox 4.1 demo SQL dump was restored on PostgreSQL 17
and the NetBox v4.1.1 form oracle reproduced issue #17498 after adding its two
duplicate manufacturers. This is a public alternative dataset, not the issue
author's database. A full clone / WAL_LOG reduction on this dataset took
262.958s (21,381 to 2 rows, 113 oracle executions), preserved the signature,
and passed a fresh PostgreSQL 17 plain-SQL restore and oracle confirmation. This
was one run, not a repeated performance comparison. Setup and negative control
use some raw SQL because of Redis-dependent delete signals; this is not an HTTP
end-to-end test. After the ownership and Accepted-state fixes, the complete
integration suite passed 112 tests with native PostgreSQL 17 clients and 112
with native PostgreSQL 18 clients. Commit `a248633` also passed both hosted
PostgreSQL matrix jobs, each with 112 tests
([CI run](https://github.com/0then0/dbreduce/actions/runs/36388139952)).
As a separate export check, plain SQL from the PG17 Wagtail, PG18 Wagtail and
PG17 NetBox clone runs was loaded with native `psql` into new databases. Each
application oracle reproduced its original signature there.

## Real-world v0.2 results

Validated DBReduce version: 0.2.0 at repository commit
`a6e0f6c4c88bd9221d0cb81540b94ab617229b0b`.

Environment:

- OS: macOS 15.6 Darwin 24.6.0, arm64.
- Hardware: MacBook Air, Apple M1, 8 cores, 8 GB RAM.
- Storage: internal MacBook Air SSD.
- Python running DBReduce: 3.14.7 from `.venv`.
- PostgreSQL 17 server: `17.11 (Debian 17.11-1.pgdg13+2)`.
- PostgreSQL 18 integration check server: `18.6 (Debian 18.6-1.pgdg13+2)`.
- PostgreSQL client tools: `postgres:18-trixie` container wrappers for
  `pg_dump`, `pg_restore`, and `psql`.
- Docker: Docker Desktop 4.92.0, Engine 29.8.0.

| Case          | Initial rows | Final rows | Oracle runs | Candidate probes | Accepted | Rejected |  Elapsed | Oracle time | Fresh restore |
| ------------- | -----------: | ---------: | ----------: | ---------------: | -------: | -------: | -------: | ----------: | ------------- |
| Wagtail #9208 |      150,706 |          3 |          33 |               21 |        9 |       12 | 103.316s |     23.495s | PASS          |
| NetBox #17498 |        1,335 |          2 |          16 |                9 |        3 |        6 |  91.179s |     27.983s | PASS          |

The Wagtail case was repeated three times with identical hardware, PostgreSQL
configuration, source dataset, oracle, DBReduce version, and confirmation count:

| Run |  Elapsed | Oracle time | Oracle executions | Candidate probes | Accepted | Rejected | Cache hits |
| --- | -------: | ----------: | ----------------: | ---------------: | -------: | -------: | ---------: |
| 1   | 103.316s |     23.495s |                33 |               21 |        9 |       12 |          1 |
| 2   | 103.419s |     23.606s |                33 |               21 |        9 |       12 |          1 |
| 3   | 104.722s |     23.599s |                33 |               21 |        9 |       12 |          1 |

Median elapsed time: 103.419s. Range: 103.316s to 104.722s.

PostgreSQL 18 compatibility was checked by restoring the minimized Wagtail SQL
into PostgreSQL 18 and rerunning the same structured oracle. It reproduced the
same signature.

For details, see [the real-world validation report](real-world-validation.md),
[Wagtail #9208](cases/wagtail-9208.md), and
[NetBox #17498](cases/netbox-17498.md).

## Reproducible manual benchmark

Run from the repository root with its existing uv environment and PostgreSQL client
tools. This uses the real reducer, FK closure, oracle subprocesses, snapshots and
SQL export. The workload is the demo's relational checkout data at 120,005 rows:
30,001 users, orders, items and payments, plus one coupon. Only a paid checkout
with a coupon exceeding the total should reproduce `checkout-negative-total`.

Create an explicitly disposable source database yourself, then load the benchmark:

```bash
createdb dbreduce_benchmark
psql -X -v ON_ERROR_STOP=1 -d dbreduce_benchmark -f examples/benchmark.sql
uv run dbreduce reduce \
  --database postgresql://localhost/dbreduce_benchmark \
  --oracle 'uv run python examples/oracle.py' --oracle-json --confirm 2 \
  --output benchmark.min.sql --report benchmark.report.json
```

Full reduction is intentionally manual: snapshot restoration can be slow and
requires disk proportional to the fixture. Never load this fixture into an existing
application database. The loader deliberately fails on existing table names.

Record server/client/Python versions, OS, CPU, RAM, storage, whether PostgreSQL is
local, and the repository revision. Keep the entire report. Compare identical
hardware, fixture, oracle, confirmation count and PostgreSQL settings. Repeat each
run at least three times and report all times or a median and range; use distinct
output filenames for each run. Cache is local to a run.

Metrics in `benchmark.report.json`:

- `initial_rows`, `final_rows`.
- `oracle_executions`, `oracle_stats.outcomes`.
- `candidate_stats.created`, `accepted`, `rejected`, and `cache_hits`.
- `performance.elapsed_seconds`, `performance.oracle_seconds`.
- `failure_identity.expected_signature`, `final_signature`, `preserved`.

Restore the exported SQL with the guide's instructions, then run the same structured
oracle using the restored database URL. It must return the same explicit signature.
The expected relational reproducer is five rows; record the actual result rather
than assuming this count. Inspect remaining rows and check that further single-row
relationship-closed deletions cannot preserve the same failure.

## Comparison policy

No clone speedup was measured for v0.2. Historical demo numbers are not included
as v0.2 measurements.

To measure v0.1 against v0.2 in separate installations, use the same exit-based
oracle with an explicit message and legacy CLI options for both, and independently
verify the output identity. The v0.1 CLI cannot consume `--oracle-json`; directly
comparing it with the updated demo would count a zero-exit structured verdict as
success and invalidate the benchmark.
