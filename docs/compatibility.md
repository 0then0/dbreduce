# Compatibility

This page describes supported versions, the reasons for their requirements, and
the checks performed on 2026-09-30 for DBReduce v0.5.0. This release retains the
Python and PostgreSQL compatibility established in v0.4.1 and adds explicit
candidate validity without new reducer primitives.

## Range in v0.5.1

DBReduce v0.5.1 supports **Python 3.10–3.15** and **PostgreSQL 13–18**. The
[v0.5.1 verification](#v051-verification) below extends the v0.5.0 results; the tables
that follow remain the record of the v0.5.0 validation.

## Python

DBReduce v0.5.0 requires **Python 3.10 or newer**. Each Python version below passed
the 104 non-PostgreSQL tests, mypy, Ruff, wheel/sdist builds, standalone wheel
installation and `dbreduce --help` in the
[v0.5 CI matrix](https://github.com/0then0/dbreduce/actions/runs/36694211296):

| Python | Unit/type/lint/CLI | Wheel build/tool install/uvx |
| ------ | ----------------- | --------------------------- |
| 3.10   | PASS              | PASS                        |
| 3.11   | PASS              | PASS                        |
| 3.12   | PASS              | PASS                        |
| 3.13   | PASS              | PASS                        |
| 3.14   | PASS              | PASS                        |

Local source checks used macOS arm64; PostgreSQL tests additionally used Linux
arm64. GitHub Actions passed Python 3.10–3.14 and PostgreSQL 15–18 on Ubuntu.
The wheel declares `Requires-Python: >=3.10`. The v0.4.1 compatibility validation
also checked `python -m pip install <wheel>` and its installed CLI in a separate
Python 3.10 environment; the installation requirements remain unchanged.

### Why Python 3.10 is the minimum

- **Trivial syntax:** three PEP 695 `type` statements and `chunks[T]` required
  Python 3.12. Ordinary type aliases and `TypeVar` remove this restriction without
  changing the reduction algorithm or introducing polyfills.
- **Hard requirement:** `zip(..., strict=True)` and runtime unions `X | None`
  require Python 3.10. No production stdlib API requiring 3.11+ was found.
- **Dependency limitation:** the locked psycopg/psycopg-binary 3.3.6 and Typer
  0.27.2 both officially declare >=3.10. No runtime dependency was downgraded.
- **Intentional policy:** the old >=3.13 floor had no remaining technical basis.
  Python 3.10 needs the same small syntax change as 3.11, so choosing 3.11 would
  retain an unnecessary installation restriction.

Dependency metadata checked against the exact locked versions and PyPI metadata:
psycopg 3.3.6 >=3.10; Typer 0.27.2 >=3.10; pytest 9.1.1 >=3.10;
mypy 1.20.2 >=3.10; Ruff 0.16.8 >=3.7. Hatchling 1.32.4 declares >=3.10,
and wheel/sdist builds passed on every tested interpreter. uv 0.12.20 declares
Python >=3.8 for its Python distribution; its native CLI installed and ran DBReduce on
every tested interpreter. Python 3.10 adds only pytest's transitive `tomli` and
`exceptiongroup` to the lockfile, not production compatibility code.

[psycopg metadata](https://pypi.org/pypi/psycopg/3.3.6/json),
[Typer metadata](https://pypi.org/pypi/typer/0.27.2/json),
[pytest metadata](https://pypi.org/pypi/pytest/9.1.1/json),
[mypy metadata](https://pypi.org/pypi/mypy/1.20.2/json),
[Ruff metadata](https://pypi.org/pypi/ruff/0.16.8/json),
[Hatchling metadata](https://pypi.org/pypi/hatchling/1.32.4/json),
[uv metadata](https://pypi.org/pypi/uv/0.12.20/json).

## PostgreSQL envelope

Use a current patched server and native, matching-major `pg_dump`, `pg_restore`
and `psql`. Snapshot is the default; clone remains experimental and opt-in.
Whole-table schema reduction always uses logical archives, independently of the
row backend. Final logical restore is mandatory before SQL publication.

| Server and client major | Snapshot | Clone WAL_LOG | Clone FILE_COPY | Schema + final logical restore |
| ----------------------- | -------- | ------------- | --------------- | ------------------------------ |
| 15                      | PASS     | PASS          | PASS            | PASS                           |
| 16                      | PASS     | PASS          | PASS            | PASS                           |
| 17                      | PASS     | PASS          | PASS            | PASS                           |
| 18                      | PASS     | PASS          | PASS            | PASS                           |

The real servers/clients were 15.19, 16.15, 17.11 and 18.6. Native binaries were
selected from `/usr/lib/postgresql/<major>/bin`; no dump/restore wrapper changed
file behavior. Both clone strategies exercise failure identity, oracle-write
isolation, ownership verification, cleanup and final restore in the integration
suite. All 49 PostgreSQL tests passed on each of the four server/client majors,
with no skips. Source-preservation and schema-candidate tests also pass.

DBReduce v0.4.0 restricted clone mode to PostgreSQL 17 or newer. Version 0.4.1
supports clone mode from PostgreSQL 15, where explicit copy strategies
are available. Both strategies passed lifecycle tests on PG15 and PG16.
Servers older than PG15 receive a version error before clone creation.
`--file-copy-method` still requires PostgreSQL 18; explicit `copy` requests were
rejected clearly on real PG15, PG16 and PG17 servers.

Servers older than PostgreSQL 15 cannot use the clone backend: PostgreSQL 14.24
rejects `CREATE DATABASE ... STRATEGY WAL_LOG` with `option "strategy" not
recognized`. Since v0.5.1 DBReduce stops with a version error before any dump or
restore when `--candidate-backend clone` meets such a server. Snapshot and schema
reduction need no PostgreSQL 15 feature and are verified on 13 and 14 (see
[v0.5.1 verification](#v051-verification)). PostgreSQL
[13 is past end of support and 14 reaches it on 2026-11-12](https://www.postgresql.org/support/versioning/);
use a patched release.

## v0.5.1 verification

Checked on 2026-10-31 on macOS arm64 (Python source checks) and Linux arm64 containers
(PostgreSQL), with the committed `uv.lock`. GitHub Actions was not run for these
results; the CI matrix was extended to the same combinations.

Python 3.10, 3.11, 3.12, 3.13, 3.14 and 3.15 (3.15.2) each passed Ruff, mypy and the
122 non-PostgreSQL tests (also with `DeprecationWarning` as an error), built the wheel
and sdist once, and ran `dbreduce --help` from that wheel through
`uvx --isolated --python <version>`. No source change was needed for 3.15; the locked
psycopg-binary and Typer install and run on it. The wheel declares
`Requires-Python: >=3.10` with no upper bound.

PostgreSQL integration suite (`pytest -m postgres`) with native, matching-major clients:

| Server and client | Result                      | Notes                                       |
| ----------------- | --------------------------- | ------------------------------------------- |
| 13.23             | 45 passed, 21 skipped       | snapshot, schema, final restore             |
| 14.24             | 45 passed, 21 skipped       | snapshot, schema, final restore             |
| 15.19             | 65 passed, 1 skipped        | adds clone WAL_LOG and FILE_COPY            |
| 16.15             | 65 passed, 1 skipped        |                                             |
| 17.11             | 65 passed, 1 skipped        |                                             |
| 18.6              | 65 passed, 1 skipped        | `file_copy_method` is PostgreSQL 18 only    |

The skips are deliberate: clone tests and the per-database ICU provider test need
PostgreSQL 15, and the test that checks the clone version error needs a server older
than 15. Everything else, including cancellation and cleanup, ran on all six majors,
and no run left a `dbreduce_` database behind. The whole suite (167 passed, 21
skipped) also passed on Python 3.15.2 with PostgreSQL 14.24. Only that pairing and the
Python 3.11 container used for the other majors were executed here; other
Python/PostgreSQL combinations rely on the CI matrix.

PostgreSQL 13 and 14 have no `CREATE DATABASE ... STRATEGY`, per-database ICU locale
providers or `file_copy_method`. Restoring an archive whose database uses the ICU provider
into such a server fails with the server's own error. `pg_dump --restrict-key`, which
snapshot fingerprinting needs, is present in the 13.23 and 14.24 clients used here; for 13 the
first patched client version was not checked.

## PostgreSQL feature audit

- **`CREATE DATABASE ... TEMPLATE`: PostgreSQL 7.1.** The
  [7.0 grammar](https://www.postgresql.org/docs/7.0/sql-createdatabase.htm) lacks
  it; [7.1 documents it](https://www.postgresql.org/docs/7.1/sql-createdatabase.html).
  Copying requires a quiescent template. DBReduce restores into owned databases,
  disables connections, checks sessions, and never templates the user source.
- **`STRATEGY WAL_LOG/FILE_COPY`: PostgreSQL 15.** Explicit strategy selection and
  WAL_LOG are documented in [PG15 CREATE DATABASE](https://www.postgresql.org/docs/15/sql-createdatabase.html).
  FILE_COPY was the earlier copying behavior, but its explicit STRATEGY syntax
  is also new in 15. WAL_LOG writes blocks to WAL; FILE_COPY forces checkpoints.
- **`file_copy_method`: PostgreSQL 18.** See
  [PG18 resource settings](https://www.postgresql.org/docs/18/runtime-config-resource.html).
  COPY/CLONE affects FILE_COPY only. The capability probe does not prove reflink
  support or shared physical blocks; no filesystem-clone guarantee is made.
- **`DROP DATABASE ... WITH (FORCE)`: PostgreSQL 13.** See
  [PG13 release notes](https://www.postgresql.org/docs/release/13.0/).
  Snapshot cleanup uses it only after exact ownership checks. Clone cleanup does
  not terminate sessions. Prepared transactions, slots/subscriptions and
  insufficient termination privileges can still prevent deletion.
- **`pg_restore --use-list`: PostgreSQL 7.1 archive/restore tooling.** The
  [original reference](https://www.postgresql.org/files/documentation/pdf/7.1/reference.pdf)
  contains `-L`; [7.2 documents the long form](https://www.postgresql.org/docs/7.2/app-pgrestore.html).
  DBReduce uses native TOC lists; it does not infer SQL dependencies from an AST.
  Unsupported/newer archive versions require a compatible `pg_restore`.
- **`pg_dump --restrict-key`: clients 14.19/15.14/16.10/17.6/18.0.** This was
  backported with the August 2025 security fixes, not limited to major 18.
  [PG15.14 notes](https://www.postgresql.org/docs/release/15.14/) explain restricted
  psql mode; the option is also present in PostgreSQL source tags
  [REL_15_14](https://github.com/postgres/postgres/blob/REL_15_14/src/bin/pg_dump/pg_dump.c),
  REL_14_19, REL_16_10, REL_17_6 and REL_18_0. Snapshot fingerprinting actually
  requires this client option. It is not merely a test convenience. Use latest
  patched clients and retain native `\restrict` behavior.
- **Catalog queries:** `pg_proc.prokind` requires PG11 procedures; database ICU
  providers require PG15, ICU rules PG16, builtin locale provider PG17.
  `read_settings` uses JSON field lookup for version-dependent `pg_database`
  columns. The PG15–18 suite verifies libc/ICU settings and archive locale restore.
  Newer locale providers must not be claimed portable to older servers.
- **`pg_depend` types:** auto/internal `a`/`i` belong to dependency tracking
  introduced in PG7.3; extension `e` to PG9.1; partition `P`/`S` to PG12.
  PG11 used the earlier internal-auto type `I`; PG12 replaced it with `P`/`S`.
  See [PG12 catalog documentation](https://www.postgresql.org/docs/12/catalog-pg-depend.html)
  and [PG15 catalog documentation](https://www.postgresql.org/docs/15/catalog-pg-depend.html).
  Schema reduction closes `a/i/P/S` and incoming foreign keys; extension-owned
  tables are rejected. Normal dependencies such as views may reject removal at
  restore time. They are not new independent reduction targets.

## Client/server policy

Matching-major current native clients and servers are the tested baseline.
The [pg_dump manual](https://www.postgresql.org/docs/18/app-pgdump.html) allows
reading older servers with newer clients and rejects newer servers with older
clients. It explicitly does not guarantee restoring that newer client's output
to the older server, even when the dump originated there. Therefore “matching or
newer clients” is insufficient as a complete round-trip compatibility claim.
Cross-major combinations were not added to the support matrix.

## CI and installation

The workflow separates six Python unit/type/lint/install jobs from six
PostgreSQL integration jobs (PG13/Python 3.11, PG14/3.15, PG15/3.10, PG16/3.12,
PG17/3.13, PG18/3.14), not a cross product. Each PostgreSQL job checks all three native client major
versions, server major, privileged test role and `--restrict-key` availability.
The release job depends on both matrices. The tables above summarize v0.5.0
validation; the candidate-validity, oracle-write isolation and final confirmation
regressions are included. GitHub Actions results are available on the repository's
[Actions page](https://github.com/0then0/dbreduce/actions/workflows/tests.yml).

`uv tool install dbreduce`, the installed `dbreduce --help`, and
`uvx dbreduce --help` passed against published v0.4.0 with Python 3.13.
Local wheels passed standalone tool installation and ephemeral execution on
3.10–3.14. See [uv's tool guide](https://docs.astral.sh/uv/guides/tools/).
DBReduce's Python minimum controls CLI installation only. The external oracle's
language/runtime is independent, as demonstrated by the
[Mastodon Rails migration](cases/mastodon-37059.md).
