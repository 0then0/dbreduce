# Benchmark and validation results

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
