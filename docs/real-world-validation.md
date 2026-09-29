# Real-world validation

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
