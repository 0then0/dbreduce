# Reproducible manual benchmark

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

## Results and comparison policy

No v0.2 benchmark results are recorded yet. The implementation environment did not
have PostgreSQL client tools or a configured test server. There is no clone backend
in v0.2, so no clone speedup can be measured. Do not reuse historical demo numbers
as v0.2 measurements.

To measure v0.1 against v0.2 in separate installations, use the same exit-based
oracle with an explicit message and legacy CLI options for both, and independently
verify the output identity. The v0.1 CLI cannot consume `--oracle-json`; directly
comparing it with the updated demo would count a zero-exit structured verdict as
success and invalidate the benchmark. Performance optimization remains future work.
