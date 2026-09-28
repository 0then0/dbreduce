# Candidate isolation

## v0.3 experimental clone backend

`--candidate-backend snapshot` remains the default. `--candidate-backend clone`
uses PostgreSQL 17 or 18 and starts only after a logical source dump has been
restored into a DBReduce-owned initial database. The user source is never a
template. Clone mode checks a live source before dumping and the owned initial
database after restoring, rejecting publications, subscriptions, logical
replication slots and prepared transactions. A custom input archive cannot
prove whether its original database had slots or prepared transactions; the
preflight covers the restored database represented by that archive. Use snapshot
mode for datasets outside this boundary.
PostgreSQL can omit subscriptions from a non-superuser `pg_dump`, so checking only
the restored archive would miss them; see the
[subscription documentation](https://www.postgresql.org/docs/18/logical-replication-subscription.html).

The accepted database is frozen with `ALLOW_CONNECTIONS false` after setup. Each
candidate is cloned from it, mutated once, read, then frozen. Each confirmation
gets a separate clone of the pristine candidate. Oracle database writes cannot
reach candidate or accepted state. An accepted candidate is promoted as it stands;
the deletion is not replayed. DBReduce does not mark these databases as public
templates or terminate unexpected sessions. A connected template causes a clear
failure. PostgreSQL's own autovacuum worker can briefly enter an owned database;
DBReduce waits up to 10 seconds for that internal worker to leave. It does not
terminate it. Other sessions still fail immediately.

Every clone database is tracked by its exact generated name, database OID, owner
and a per-run database comment. Cleanup checks all of these before `DROP DATABASE`.
The clone backend does not use `FORCE`. If an unexpected session, prepared
transaction, slot or subscription prevents cleanup, the command fails and names
the leftover database for manual inspection. A hard kill can also leave a
database. Its name prefix alone is not proof that it is safe to remove.

Before export, DBReduce clones the final accepted state solely for `pg_dump`,
restores that custom archive into a fresh database created from `template0`,
compares the row state and confirms the same failure identity again. It publishes
SQL and report only after this logical round trip and cleanup succeed.

`--clone-strategy wal_log` is the clone default. `file_copy` is available explicitly.
PostgreSQL documents `FILE_COPY` as requiring checkpoints before and after copy;
it is not inherently faster than `WAL_LOG` on a given workload. On PostgreSQL 18,
`--clone-strategy file_copy --file-copy-method clone` probes whether PostgreSQL
accepts `file_copy_method=CLONE` for an actual database copy. A passed probe
verifies the server API, not block sharing on the underlying filesystem; the
report sets `filesystem_reflink_verified` to false. Do not interpret this mode
as proven copy-on-write performance without separate filesystem evidence.

`--restore-jobs N` applies to custom-archive restores, including snapshot
candidate and oracle resets. It remains 1 by default; the measured Wagtail
environment benefited from 4 workers. No parallel dump format was introduced.

See [CREATE DATABASE](https://www.postgresql.org/docs/18/sql-createdatabase.html),
[file_copy_method](https://www.postgresql.org/docs/18/runtime-config-resource.html),
and [DROP DATABASE](https://www.postgresql.org/docs/18/sql-dropdatabase.html).

## v0.2 decision (historical)

v0.2 keeps the snapshot backend. No speedup or clone/snapshot parity is claimed.

PostgreSQL supports `CREATE DATABASE candidate TEMPLATE accepted`, but requires
no other connections to the template when copying begins. The caller must own the
template or be superuser unless it is marked as a public template. DBReduce should
never mark user databases as templates or terminate connections to the source.
See [CREATE DATABASE](https://www.postgresql.org/docs/17/sql-createdatabase.html)
and [template databases](https://www.postgresql.org/docs/17/manage-ag-templatedbs.html).

A viable future design needs three distinct roles: pristine accepted database,
pristine deletion candidate, and an oracle clone recreated for each confirmation.
Promoting the oracle clone would leak INSERT/UPDATE/DELETE and sequence changes.
Replaying a deletion on accepted state after probing is also unsafe: triggers can
be nondeterministic. Only the pristine candidate can be promoted.

Cloning preserves database contents, sequences, extensions, triggers and database
encoding/locale. This differs from the present normalization through dump/restore.
Existing tests reject candidates whose removed rows reappear during restoration.
An in-server clone alone cannot establish that the final SQL reproducer behaves
the same after restore. A correct fast backend must keep export/restore verification
and decide when normalization is necessary, including restoration-created data.

Before shipping it, test:

- Quiescent templates and active-connection failures without touching source sessions.
- Distinct ownership proof for every generated database, collisions, and uncertain
  CREATE replies. A name prefix alone never authorizes DROP DATABASE.
- Sequence state, extensions, trigger effects, locale and exported SQL equivalence.
- Clean clones for each N/N confirmation, final confirmation and next candidate.
- Process-group cleanup before dropping oracle clones; Ctrl-C at each lifecycle step.
- CREATEDB/ownership failures, database connection settings and cleanup exceptions.
- Kill/crash leftovers: no automatic wildcard cleanup. Manual removal requires
  operator verification of ownership; a crash can lose in-memory creation evidence.
- Snapshot/clone regression parity using an oracle that writes and sometimes returns
  a different failure identity; both outputs must survive fresh SQL restoration.

CREATE DATABASE copies physical data; it is not guaranteed to be a cheap
copy-on-write operation. Benchmark on the intended PostgreSQL version and storage.
Until the lifecycle and export semantics are verified, retaining snapshots follows
the v0.2 priority of correctness over speed. The existing reducer Backend protocol
remains the boundary for a future clone implementation; no plugin framework is needed.
