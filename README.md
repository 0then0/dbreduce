# DBReduce

Give DBReduce a PostgreSQL database and a failing test. It reduces the relational
dataset while preserving **the same bug**, using an explicit failure matcher or
structured oracle verdict. Results are locally irreducible under the attempted
relationship-closed deletions, not a guaranteed global minimum.

```bash
python -m pip install dbreduce

dbreduce reduce \
  --database postgresql://localhost/app_bug \
  --oracle 'uv run pytest tests/test_checkout.py::test_negative_total' \
  --match-stdout 'AssertionError: negative total' \
  --confirm 3
```

Requires Python 3.13+, PostgreSQL, current `pg_dump`/`pg_restore` client tools and a
workspace role with `CREATEDB`. The oracle must use `DATABASE_URL` or
`DBREDUCE_DATABASE_URL`, which point to a disposable copy. Use trusted oracles and
dumps; external connections and side effects are not sandboxed.

- **Failure identity:** stdout/stderr regexes, exit status, or JSON verdict with a signature.
- **Isolation:** oracle writes never enter accepted snapshots or exported data.
- **Relationships:** real FKs and explicit single-column/composite virtual relationships.
- **Inspection:** row counts, keys, dependency graphs and strongly connected components.
- **Reports:** identity, outcomes, candidate/cache statistics and timings, without raw logs.

Without an identity matcher, the compatible legacy mode warns that unrelated
nonzero failures may count as reproduction. Timeouts and detectable infrastructure
errors abort. v0.2 retains the conservative snapshot backend; fast cloning is deferred.

Outputs: `dbreduce.min.sql` and `dbreduce-report.json`. Existing files are never
overwritten. DBReduce reads the source and reduces only its generated workspace.

## Documentation

- [Usage, identity protocol, config, safety and development](docs/guide.md)
- [Reproducible 120,005-row benchmark and demo](docs/benchmark.md)
- [PostgreSQL cloning investigation and v0.2 decision](docs/isolation.md)

Licensed under Apache-2.0.
