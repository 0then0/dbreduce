<p align="center">
  <img src="https://raw.githubusercontent.com/0then0/dbreduce/main/docs/dbreduce-icon.svg" width="80" height="80" alt="DBReduce logo">
</p>

<h1 align="center">DBReduce</h1>

<p align="center">
  <strong>Give DBReduce a PostgreSQL database and a failing test.<br>
  It finds a smaller relational dataset and can reduce its schema while preserving the bug.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/dbreduce/"><img src="https://img.shields.io/pypi/v/dbreduce" alt="PyPI"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/pypi/l/dbreduce" alt="License"></a>
  <a href="https://github.com/0then0/dbreduce/actions/workflows/tests.yml"><img src="https://github.com/0then0/dbreduce/actions/workflows/tests.yml/badge.svg?branch=main" alt="Tests"></a>
</p>

DBReduce reduces PostgreSQL data while checking that each candidate preserves the
original failure identity. It supports output matchers and structured JSON verdicts,
keeps oracle writes out of accepted data, and follows both PostgreSQL foreign keys
and explicitly configured application relationships.

## Quick start

DBReduce v0.4.1 supports Python 3.10–3.14 and PostgreSQL 15–18. Use current patched,
matching-major `pg_dump`, `pg_restore` and `psql` tools; see the
[compatibility report](docs/compatibility.md).

Install DBReduce as a standalone developer tool:

```bash
uv tool install dbreduce
dbreduce --help
```

`uvx dbreduce --help` also works without a persistent install. Standard installation
remains available with `python -m pip install dbreduce`. DBReduce itself is Python
software, but does not need to be installed in the application's environment or
added to its dependencies. The oracle can be Ruby, Java, Go, Node.js, Python,
shell, or any other executable.

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

Use `--oracle-framed-json` when the application logs to stdout. Snapshot isolation
is the default; `--candidate-backend clone` is an experimental alternative on
PostgreSQL 15–18. Both `WAL_LOG` and `FILE_COPY` are tested; `file_copy_method`
requires PostgreSQL 18. `--restore-jobs N` enables parallel archive restores.

Use `--reduce-schema` for opt-in whole-table schema reduction.
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

The opt-in v0.4.0 schema phase was validated with real Wagtail and NetBox
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

Ruby/Rails validation uses
[Mastodon #37059](docs/cases/mastodon-37059.md): 1,757 rows become two; optional
schema reduction leaves 12 of 109 tables. The exported SQL reproduces the same
migration exception in v4.5.2 after fresh restore; v4.5.3 returns a negative
verdict. With the documented application container and network already set up,
the validated oracle command is:

```bash
dbreduce reduce \
  --database postgresql://postgres@dbreduce-adoption-pg17/mastodon_case \
  --oracle 'docker exec -e DATABASE_URL="$DATABASE_URL" dbreduce-adoption-mastodon bundle exec rails runner /case/oracle.rb' \
  --oracle-framed-json --confirm 2 --timeout 60
```

DBReduce runs separately from Rails. The case records 24 schema-phase oracle
infrastructure errors and does not claim schema local irreducibility.

See [CHANGELOG.md](CHANGELOG.md) for release history.

## Documentation

- [Usage, oracle identity, configuration, safety, and development](docs/guide.md)
- [Benchmark and demo](docs/benchmark.md)
- [Real-world validation report](docs/real-world-validation.md)
- [Candidate isolation and clone safety](docs/isolation.md)
- [Tested compatibility and decisions](docs/compatibility.md)
- [Trying DBReduce and reporting adoption blockers](CONTRIBUTING.md)
- [Design discussion: application-invalid schema candidates](docs/candidate-invalid.md)

## Project links

- [Repository](https://github.com/0then0/dbreduce)
- [Issues](https://github.com/0then0/dbreduce/issues)
- [PyPI](https://pypi.org/project/dbreduce/)

Licensed under [Apache-2.0](LICENSE).
