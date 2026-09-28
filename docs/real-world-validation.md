# DBReduce real-world validation report

This page records the v0.2 validation. Rebuilt v0.3 Wagtail and NetBox runs,
phase timings, and the full PostgreSQL 18 reduction are recorded in the
[v0.3 benchmark section](benchmark.md#v03-development-evidence-unreleased).

Validated DBReduce version: 0.2.0 at repository commit
`a6e0f6c4c88bd9221d0cb81540b94ab617229b0b`.

Environment:

- OS: macOS 15.6 Darwin 24.6.0, arm64.
- Hardware: MacBook Air, Apple M1, 8 cores, 8 GB RAM.
- Storage: internal MacBook Air SSD.
- Python running DBReduce: 3.14.7 from `.venv`.
- PostgreSQL 17 server: `17.11 (Debian 17.11-1.pgdg13+2)`.
- PostgreSQL 18 server for integration check: `18.6 (Debian 18.6-1.pgdg13+2)`.
- PostgreSQL client tools: `postgres:18-trixie` container wrappers for
  `pg_dump`, `pg_restore`, and `psql`.
- Docker: Docker Desktop 4.92.0, Engine 29.8.0.

## Validation matrix

| Case          | Initial rows | Final rows | Failure identity | Fresh restore | Oracle runs |  Elapsed | Virtual relationships | Result |
| ------------- | -----------: | ---------: | ---------------- | ------------- | ----------: | -------: | --------------------- | ------ |
| Wagtail #9208 |      150,706 |          3 | preserved        | PASS          |          33 | 103.316s | no                    | PASS   |
| NetBox #17498 |        1,335 |          2 | preserved        | PASS          |          16 |  91.179s | no                    | PASS   |

## Case summaries

Wagtail #9208 uses the real Wagtail schema and documented bug. Its oracle checks a
collection tree state that triggers a duplicate key error
on `wagtailcore_collection.path` when adding a new collection. DBReduce reduced a
150,706-row PostgreSQL database to 3 rows while preserving the structured
signature `wagtail-9208-collection-path-integrity`. Fresh restore reproduced the
same signature on PostgreSQL 17, and the minimized SQL also restored and
reproduced on PostgreSQL 18.
The large surrounding dataset was generated application noise, not a production dump.

NetBox #17498 uses the real NetBox v4.1.1 application/form path for a bulk import
lookup where duplicate manufacturer
descriptions trigger `Manufacturer.MultipleObjectsReturned`. DBReduce reduced a
1,335-row database reconstructed from available scripts to 2 rows while preserving
`netbox-17498-manufacturer-description-multiple-objects`. Fresh restore
reproduced the same signature on PostgreSQL 17.
The exact database used by the issue author is not publicly available; issue
#17498 provides reproduction steps but no dump. A separate reduction using the
official NetBox 4.1 demo dump is recorded in the [v0.3 benchmark evidence](benchmark.md#v03-development-evidence-unreleased)
and [NetBox case report](cases/netbox-17498.md).

Detailed notes:

- [Wagtail #9208](cases/wagtail-9208.md)
- [NetBox #17498](cases/netbox-17498.md)

## Repeated benchmark

The Wagtail case was repeated three times on identical hardware, PostgreSQL 17,
source dataset, oracle, DBReduce version, and confirmation count.

| Run |  Elapsed | Oracle time | Oracle executions | Candidate probes | Accepted | Rejected | Cache hits |
| --- | -------: | ----------: | ----------------: | ---------------: | -------: | -------: | ---------: |
| 1   | 103.316s |     23.495s |                33 |               21 |        9 |       12 |          1 |
| 2   | 103.419s |     23.606s |                33 |               21 |        9 |       12 |          1 |
| 3   | 104.722s |     23.599s |                33 |               21 |        9 |       12 |          1 |

Median elapsed time: 103.419s. Range: 103.316s to 104.722s.

The run-to-run spread was small for this environment.

## Validation limits and integration findings

- Both real-app oracles use structured signatures and passed positive and
  negative controls. Wagtail's negative control required `fixtree --full` to
  clear the faulty tree state.
- NetBox delete signals consult Redis-backed configuration cache. The setup and
  negative control used raw SQL to remove a duplicate manufacturer; the positive
  oracle exercised the real import form.
- A non-target application exception can occur after candidate data is removed.
  The Wagtail oracle classifies those exceptions as `reproduced: false`, while
  preserving the specific signature for the target failure.
- The JSON oracle test for distinguishing `BUG_A` from `BUG_B` passed.
- The virtual relationship closure test passed. Neither real-app case required
  virtual relationships.
- The PostgreSQL-backed `BUG_A`/`BUG_B` rejection test did not complete because
  the Docker client wrapper left `candidate.dump` unavailable. Database-level
  rejection of a different failure identity remains unverified in this run.

## Bottleneck

For Wagtail run 1:

- Total elapsed: 103.316s.
- Oracle time: 23.495s.
- Non-oracle time: 79.821s.

For NetBox:

- Total elapsed: 91.179s.
- Oracle time: 27.983s.
- Non-oracle time: 63.196s.

These differences include all work outside the timed oracle process, including
database reconstruction, candidate deletion, state reading, fingerprinting and
oracle preparation. v0.2 did not time those phases separately, so these numbers
do not identify a specific bottleneck.

## Integration pain points

Shared integration findings:

- The oracle must produce exactly one structured JSON object on stdout.
- Application startup output must be redirected away from stdout.
- Oracle startup time matters when every candidate runs the application.
- PostgreSQL client paths and temp directories matter when DBReduce runs outside
  the PostgreSQL container.

Project-specific findings:

- Wagtail's `fixtree --full` was required for the negative control; plain
  `fixtree` did not clear the condition.
- NetBox Docker configuration prints startup messages and its delete signals
  expect Redis-backed configuration cache.

## Conclusions

1. DBReduce works on real PostgreSQL applications in this validation: Wagtail and
   NetBox both reduced successfully and restored reproducers preserved the same
   bug identity.
2. Reduction was strong for these cases: 150,706 to 3 rows for Wagtail and 1,335
   to 2 rows for NetBox.
3. The same bug was preserved in both successful cases using structured JSON
   signatures and fresh restore checks.
4. Candidate isolation dominated runtime in both cases.
5. Candidate isolation and oracle startup were the main measured runtime costs.
