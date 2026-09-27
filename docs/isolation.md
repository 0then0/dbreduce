# Candidate isolation: v0.2 decision

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
