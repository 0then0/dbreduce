# Real-world validation

## v0.5 candidate validity validation (2026-09-30)

Local v0.5.0 source checks and both application reruns completed. The
[GitHub CI run](https://github.com/0then0/dbreduce/actions/runs/36694211296)
also passed the complete Python/PostgreSQL matrix for commit `2e93f22`.
The application measurements below are local validations, separate from CI.
No new DBMS, schema primitive, framework package or runtime dependency was added.

The [structured protocol](candidate-invalid.md) adds only explicit
`{"reproduced":false,"outcome":"candidate_invalid"}`. Old positive/negative JSON
and framed verdicts remain supported. Contradictory/unknown outcomes fail closed;
nonzero exit status remains an infrastructure failure. The typed internal outcome
keeps target-negative, different-failure, application-invalid and infrastructure
results distinct. Invalid restore retains its existing separate schema counters.
Early rejection, exact-state cached rejection and cumulative minimality barriers
are documented in that contract. Fresh final confirmation is still mandatory.

### Application reruns

Both runs reused preserved, independently restored data-only SQL with the full
original schema, avoiding repetition of the already validated data reduction:

- [NetBox #17498](cases/netbox-17498.md#candidate-validity-validation-v050-source-2026-09-30):
  83 schema probes, 13 accepted, 70 explicit candidate-invalid, zero infrastructure
  errors. Historical v0.4 protocol reported 70 infrastructure errors. The same
  four tables/two rows remain; schema elapsed was 243.250s, total 284.590s,
  105 oracle executions. Each invalid verdict came from an explicit missing-table
  precondition query before Django setup, not analysis of an old exception.
- [Mastodon #37059](cases/mastodon-37059.md#candidate-validity-validation-v050-source-2026-09-30):
  601 schema probes, 27 accepted, 26 candidate-invalid, 524 invalid restores and
  24 unresolved infrastructure errors. Historical v0.4.1 reported 26 negative
  verdicts and 24 infrastructure errors. The new invalid verdicts replace those
  negative missing-`settings` verdicts; the 24 unknown errors remain errors.
  The same 12 tables/two rows and exact identity remain; schema elapsed was
  584.482s, total 618.422s, 111 oracle executions.

Both final SQL files passed separate unmodified native PG17 restores and their
real application oracles. Separate missing-required-table, target-negative and
unreachable-database controls passed for both wrappers. Fixed Mastodon v4.5.3
returned a negative verdict on the final reproducer. This validation does not
claim that all infrastructure errors were eliminated or that v0.5 is faster.
Wrappers only cover their declared case-specific table invariants.

### Checks actually run

Using the existing environments, with `PYTHONPATH` pointing at the edited source:

```bash
python -m pytest -q -m 'not postgres'
python -m mypy src
python -m ruff check .
```

All three commands passed on each of Python 3.10, 3.11, 3.12, 3.13 and 3.14;
100 non-PostgreSQL tests passed per interpreter. In disposable Docker services,
matching native PostgreSQL clients and the existing Linux environments ran:

```bash
DBREDUCE_TEST_ADMIN=postgresql://postgres@case-server/postgres \
  python -m pytest -q -m postgres
```

All 49 PostgreSQL tests passed in each existing CI pairing: PG15/Python 3.10,
PG16/Python 3.12, PG17/Python 3.13 and PG18/Python 3.14. This includes schema/data
candidate-invalid for snapshot/clone, fresh restore, source preservation,
`BUG_A`/`BUG_B`, oracle-write isolation, ownership and cleanup regressions.
Protocol tests cover strict/framed compatibility, invalid fields, multiple verdicts,
exit status, early rejection and final-invalid publication prevention.
The negative cache test verifies retained rejection/barrier for an identical exact
state, and a fresh lookup when sequence state changes.

`uv lock --check`, `uv build`, extracted-wheel `python -m dbreduce --help`,
wheel version/Python metadata checks, Ruby syntax checks for both Mastodon scripts,
and `git diff --check` passed. The existing Python/PG CI matrix was left unchanged;
the subsequent GitHub CI run passed all five Python and four PostgreSQL jobs,
including wheel build, standalone installation and ephemeral execution.
After review, four NetBox wrapper regressions brought the non-PostgreSQL suite
to 104 tests. The wrapper now forwards all five effective `DB_*` parameters,
including libpq defaults, rather than retaining inherited application settings.
Those targeted regressions passed; the full application measurements above
precede that correction and were not repeated afterward.

### Limits and release status

Candidate-invalid does not prove an object necessary for the target failure.
Minimality barriers are cumulative and conservative. Early rejection cannot
observe later contradictory verdicts; per-run cache correctness still assumes
oracle determinism. The core does not infer validity from exceptions, stderr or
legacy matchers. Unknown application defects, services, credentials, timeouts
and protocol errors remain unresolved infrastructure failures.

The [v0.5.0 release](https://github.com/0then0/dbreduce/releases/tag/v0.5.0)
is dated 2026-09-30 in the changelog. The tag-triggered workflow repeats both
test matrices before creating the GitHub release and publishing to PyPI.
[Issue #1](https://github.com/0then0/dbreduce/issues/1) remains open for adoption
feedback on the current version. After this milestone the project returns to
maintenance/external-feedback mode; no v0.6 features are started.


## Ruby/Rails validation and regression checks

On 2026-09-29, the source version prepared for v0.4.1 validated
[Mastodon #37059](cases/mastodon-37059.md) with the actual Ruby/Rails migration in
official v4.5.2 and v4.5.3 images, PostgreSQL/native clients 17.11 and framed JSON.
Data reduction changed 1,757 rows to two; subsequent schema reduction changed
109 tables to 12 and plain SQL from 262,004 to 44,111 bytes. Separate fresh
restores of both exports reproduced the same v4.5.2 failure; the fixed application
returned negative verdicts. DBReduce core needed no language-specific dependency.
Twenty-four schema candidates produced oracle infrastructure errors, so schema
local irreducibility is not claimed. This repeats the NetBox candidate/verdict
limitation; see [the adoption evidence](adoption.md).

Wagtail data-only reduction was rerun against the same 150,706-row source:
published v0.4.0 and the v0.4.1 source version each reached three rows with
36 oracle executions and preserved failure identity. A separate restore of the
v0.4.1 source version's exported SQL passed the Wagtail oracle. The v0.4 NetBox
four-table/two-row SQL was separately restored with native PG17 clients and
passed the actual NetBox v4.1.1 form oracle. NetBox's full reduction was not
repeated. The [performance comparison](benchmark.md#v040-and-v041-comparison)
records the sequential Wagtail comparison.

## Earlier validation

The sections below summarize validation from v0.2.0 through v0.4.0. v0.3 measurements are
in the [benchmark](benchmark.md); application details are in the
[Wagtail](cases/wagtail-9208.md) and [NetBox](cases/netbox-17498.md) case studies.

The v0.4.0 opt-in schema run on the recreated Wagtail 8.0 source
reduced 150,706 rows and 48 tables to 3 rows and 1 table. It preserved the
failure identity after a fresh logical restore and after a separate `psql`
restore of the published SQL. Counts and timings are in the
[Wagtail case](cases/wagtail-9208.md#opt-in-schema-validation-v040).
The full public NetBox demo case reduced 21,381 rows and 180 tables to 2 rows
and 4 tables. Its published SQL also passed separate restore and oracle checks.
Seventy NetBox schema candidates produced oracle infrastructure errors, so
its report does not claim local irreducibility. Counts and timings are in the
[NetBox case](cases/netbox-17498.md#opt-in-schema-validation-v040).

## v0.2.0 results

Validated commit: `a6e0f6c4c88bd9221d0cb81540b94ab617229b0b`. The runs used a
MacBook Air M1 (8 cores, 8 GB RAM, internal SSD), Python 3.14.7, PostgreSQL
17.11, and PostgreSQL 18.6 for the Wagtail SQL restore check. PostgreSQL client
tools ran in Docker containers.

| Case          | Initial → final rows | Oracle runs | Candidate probes |          Elapsed | Fresh restore |
| ------------- | -------------------: | ----------: | ---------------: | ---------------: | ------------- |
| Wagtail #9208 |          150,706 → 3 |          33 |               21 | 103.316–104.722s | PG17 and PG18 |
| NetBox #17498 |            1,335 → 2 |          16 |                9 |          91.179s | PG17          |

Wagtail used the real Wagtail schema and documented bug. The surrounding dataset
contained generated application noise and was not a production dump. The three
Wagtail runs had a median of 103.419s. NetBox used the real NetBox v4.1.1
application/form path. Its 1,335-row database was reconstructed from migrations
and setup scripts. The issue author's exact database is unavailable: issue
#17498 describes how to create duplicate manufacturer descriptions but includes
no database dump.

## Validation limits

- NetBox setup and negative control partly used raw SQL because its delete
  signals depend on Redis-backed configuration. The positive oracle exercised
  the real form path, not a complete HTTP flow.
- Wagtail's negative control required `fixtree --full`; plain `fixtree` did not
  clear the faulty tree state.
- The original v0.2 PostgreSQL `BUG_A`/`BUG_B` oracle-write isolation scenario
  did not complete because its Docker client wrapper left a candidate dump
  unavailable. This gap was fixed later: native PostgreSQL 17 and 18 regression
  tests passed, including different-failure rejection and oracle-write
  isolation. See the [CI run](https://github.com/0then0/dbreduce/actions/runs/36388139952).

The [v0.3 Wagtail benchmark](benchmark.md#v03-wagtail-comparison) compares
snapshot serial and parallel restores with clone strategies on the same
150,706-row generated dataset. It records the PostgreSQL versions, Docker
wrapper cost, all runs, phase totals, and fresh logical restore results.

A full Wagtail reduction also completed on PostgreSQL 18 in an earlier v0.3
validation run: clone / `WAL_LOG`, 150,706 to 3 rows, 61.395s, with the same
failure identity after fresh logical restore. That run predates the current
comparative series. The official NetBox 4.1 demo dataset has a separate full
PostgreSQL 17 reduction recorded in the [NetBox case report](cases/netbox-17498.md).
