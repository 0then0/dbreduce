# DBReduce

**Give DBReduce a PostgreSQL database and a failing test. It finds a smaller
relational dataset that still reproduces the same bug.**

[![PyPI](https://img.shields.io/pypi/v/dbreduce)](https://pypi.org/project/dbreduce/)
[![Python](https://img.shields.io/badge/Python-3.13%2B-3776AB)](https://www.python.org/)
[![License](https://img.shields.io/pypi/l/dbreduce)](LICENSE)
[![Tests](https://github.com/0then0/dbreduce/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/0then0/dbreduce/actions/workflows/tests.yml)

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

## What it does

- Checks failure identity so an unrelated error does not count as reproducing the bug.
- Isolates each oracle run so its database writes do not affect later candidates.
- Reduces through real foreign keys and declared single or composite relationships.
- Reports the final identity, candidate outcomes, timings, and row counts without
  saving raw oracle output.

Without an identity matcher, legacy mode accepts any nonzero application exit status
and prints a warning. The result is locally irreducible under attempted
relationship-closed transformations; DBReduce does not promise a global minimum.
By default, DBReduce writes `dbreduce.min.sql` and `dbreduce-report.json` and never
overwrites existing files.

## Real-world validation

DBReduce v0.2.0 and v0.3.0 were validated with real Wagtail and NetBox schemas and
application oracles. Wagtail's surrounding data was generated application noise,
not a production dump. The 1,335-row NetBox database was reconstructed; the issue
author's exact dump is unavailable. A separate v0.3.0 run used the official NetBox
4.1 demo dump as a public alternative and passed a fresh restore check. NetBox
validation exercised its form path, not a full HTTP flow:

```text
Wagtail #9208
150,706 rows
    v
DBReduce
    v
3 rows

same failure preserved
fresh restore verified
```

```text
NetBox #17498
1,335 rows
    v
DBReduce
    v
2 rows

same failure preserved
fresh restore verified
```

See the [real-world validation report](docs/real-world-validation.md) and
[case studies](docs/cases/) for commands, environment, performance numbers, and
limitations.

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
