# Candidate isolation

DBReduce provides two PostgreSQL candidate backends:

- `snapshot` recreates candidates through custom-format dump and restore. It is
  the default and compatibility fallback.
- `clone` uses PostgreSQL 17 or later database copies. It is experimental and
  opt-in with `--candidate-backend clone`.

## Clone safety model

The user source is read-only and is never used as a PostgreSQL template. Clone
mode first dumps it, then restores that dump into a DBReduce-owned database.
Only DBReduce-owned databases can become clone sources.

An accepted database is frozen before it is used as a template. DBReduce checks
that it has no unexpected sessions and does not terminate other connections.
Each candidate is cloned from the accepted state and mutated once. The candidate
is frozen before oracle checks. Each confirmation runs against a separate clone
of that candidate, which is dropped afterward. If accepted, the pristine
candidate itself is promoted; DBReduce does not replay the deletion.

Before dropping a database, DBReduce verifies its generated name, OID, owner,
and per-run marker. It does not use a name prefix as ownership proof or use
`DROP DATABASE ... WITH (FORCE)` in clone mode. If safe cleanup fails, DBReduce
reports the exact database for inspection. A hard process kill can leave owned
databases behind; inspect ownership before manual cleanup.

Clone preflight checks the live source before dumping and the owned initial
database after restore. It rejects publications, subscriptions, logical
replication slots, and prepared transactions when detected. This source check
matters because `pg_dump` can omit subscriptions depending on privileges. A
custom archive cannot prove whether the original source had logical slots or
prepared transactions. Use snapshot mode if the dataset depends on replication
state. See the PostgreSQL
[subscription documentation](https://www.postgresql.org/docs/18/logical-replication-subscription.html).

Before publication, clone mode dumps the final accepted database, restores the
archive into a fresh database from `template0`, confirms the same failure
identity, and exports plain SQL. It publishes only after these checks and
cleanup succeed.

## PostgreSQL strategies

The clone default is `WAL_LOG`; `FILE_COPY` is selected with
`--clone-strategy file_copy`. Relative speed depends on workload and storage.
PostgreSQL 18 `--file-copy-method clone` is available only with `FILE_COPY`.
The capability probe checks the PostgreSQL API. It does not prove that the
filesystem supports reflink or that blocks were shared.

`--restore-jobs N` controls custom-archive `pg_restore` workers for both
backends. The default is 1. No parallel dump mode is used.

See the [benchmark](benchmark.md) and PostgreSQL documentation for
[CREATE DATABASE](https://www.postgresql.org/docs/18/sql-createdatabase.html),
[file_copy_method](https://www.postgresql.org/docs/18/runtime-config-resource.html),
and [DROP DATABASE](https://www.postgresql.org/docs/18/sql-dropdatabase.html).
