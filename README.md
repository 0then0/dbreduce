<p align="center">
  <img src="docs/dbreduce-icon.svg" width="80" height="80" alt="DBReduce logo">
</p>

<h1 align="center">DBReduce</h1>

<p align="center">
  <strong>Give DBReduce a PostgreSQL database and a failing test.<br>
  It finds a smaller relational dataset and can reduce its schema while preserving the bug.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/dbreduce/"><img src="https://img.shields.io/pypi/v/dbreduce" alt="PyPI"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.13%2B-3776AB" alt="Python 3.13+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/pypi/l/dbreduce" alt="License"></a>
  <a href="https://github.com/0then0/dbreduce/actions/workflows/tests.yml"><img src="https://github.com/0then0/dbreduce/actions/workflows/tests.yml/badge.svg?branch=main" alt="Tests"></a>
</p>

DBReduce reduces PostgreSQL data while checking that each candidate preserves the
original failure identity. It supports output matchers and structured JSON verdicts,
keeps oracle writes out of accepted data, and follows both PostgreSQL foreign keys
and explicitly configured application relationships.

## Quick start

DBReduce requires Python 3.13 or newer, PostgreSQL, and client tools (`pg_dump` and
`pg_restore`) compatible with your server. Install it with:

```bash
python -m pip install dbreduce
```

Then reduce a database with a test that identifies the bug:

```bash
dbreduce reduce \
  --database postgresql://localhost/app_bug \
  --oracle 'uv run pytest tests/test_checkout.py::test_negative_total' \
  --match-stdout 'AssertionError: negative total' \
  --confirm 3
```

The oracle must connect through `DATABASE_URL` or `DBREDUCE_DATABASE_URL`. DBReduce
sets both variables to its disposable workspace for every run. Use a database role
with `CREATEDB` permission. The source database is read only; reductions and oracle
executions run against a generated copy. Use trusted commands and dumps: SQL
functions, triggers, and external services can have effects outside that copy.

For an oracle that can report a stable signature directly, use `--oracle-json` and
have it write `{"reproduced": true, "signature": "checkout-negative-total"}` to
stdout. See the guide for the verdict protocol and safety details.

Version 0.3.0 adds `--oracle-framed-json` for applications that log to stdout,
`--restore-jobs N` for parallel custom-archive restores, and an experimental,
opt-in PostgreSQL 17/18 `--candidate-backend clone`. Snapshot isolation remains
the default. Clone mode verifies a final logical restore before publishing SQL.

Version 0.4.0 provides opt-in whole-table schema reduction with `--reduce-schema`.
It runs after row reduction, checks each proposed removal with
the same oracle, and exports only after a fresh logical restore. The default
data-only behavior is unchanged. Use `--oracle-json` or `--oracle-framed-json` so
an application startup error cannot count as the original bug.

## What it does

- Checks failure identity so an unrelated error does not count as reproducing the bug.
- Isolates each oracle run so its database writes do not affect later candidates.
- Reduces through real foreign keys and declared single or composite relationships.
- Reports the final identity, candidate outcomes, timings, and row counts without
  saving raw oracle output.
- With `--reduce-schema`, tries removal of application tables and their automatic
  dependencies; reports schema object counts and logical SQL size.

Without an identity matcher, legacy mode accepts any nonzero application exit status
and prints a warning. The result is locally irreducible under attempted
relationship-closed transformations; DBReduce does not promise a global minimum.
By default, DBReduce writes `dbreduce.min.sql` and `dbreduce-report.json` and never
overwrites existing files.

## Real-world validation

The opt-in v0.4 schema phase was validated with real Wagtail and NetBox
application oracles on PostgreSQL 17. Wagtail used generated application noise;
NetBox used the public 4.1 demo dataset with the issue condition added:

```text
Wagtail #9208
Rows:             150,706 -> 3
Tables:           48 -> 1
Failure identity: preserved
Fresh SQL restore: PASS
```

```text
NetBox #17498
Rows:             21,381 -> 2
Tables:           180 -> 4
Failure identity: preserved
Fresh SQL restore: PASS
```

Seventy NetBox schema candidates exited before the structured oracle verdict;
the NetBox report therefore does not claim local irreducibility. NetBox
validation exercised its form path, not a full HTTP flow. Earlier v0.2/v0.3
data-only results remain documented in the case reports.

See the [real-world validation report](docs/real-world-validation.md) and
[case studies](docs/cases/) for commands, environment, performance numbers, and
limitations.

See [CHANGELOG.md](CHANGELOG.md) for release history.

## Documentation

- [Usage, oracle identity, configuration, safety, and development](docs/guide.md)
- [Benchmark and demo](docs/benchmark.md)
- [Real-world validation report](docs/real-world-validation.md)
- [PostgreSQL isolation investigation](docs/isolation.md)

## Project links

- [Repository](https://github.com/0then0/dbreduce)
- [Issues](https://github.com/0then0/dbreduce/issues)
- [PyPI](https://pypi.org/project/dbreduce/)

Licensed under [Apache-2.0](LICENSE).
