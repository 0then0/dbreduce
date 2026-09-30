# Trying DBReduce and reporting failures

Install DBReduce as a separate CLI with `uv tool install dbreduce` or
`python -m pip install dbreduce`. It does not belong in your application's
dependencies. See [compatibility](docs/compatibility.md) for the tested envelope
and [the guide](docs/guide.md) for isolation and supported schema shapes.

Start with a trusted local copy of the source database. The source is read-only;
DBReduce creates and mutates owned temporary databases. Give the workspace role
`CREATEDB`, and make the oracle use the supplied `DATABASE_URL`. Database triggers,
functions and application commands can still contact external systems. Use a
disposable application environment without production credentials.

Before reducing, verify both controls: the oracle identifies the target bug on
its triggering state, and returns `reproduced: false` when that state is removed
or the application is fixed. Prefer `--oracle-json` or `--oracle-framed-json` with
a stable signature. An unrelated startup failure is not the target bug.
With v0.5.0, a structured oracle may explicitly return
`{"reproduced":false,"outcome":"candidate_invalid"}` for a known violated
application precondition. Unknown failures must remain errors. See the
[candidate-validity contract](docs/candidate-invalid.md).

For an independent usage report or blocker, [open an issue](https://github.com/0then0/dbreduce/issues/new)
with:

- DBReduce, Python, application and framework versions; OS and installation command.
- PostgreSQL server and native `pg_dump`, `pg_restore`, `psql` versions.
- Backend, oracle mode, relevant CLI flags and the exact observable failure.
- Candidate-invalid counts and the declared preconditions used to classify them,
  separately from negative verdicts and infrastructure errors.
- A small shareable reproducer, or the report's counts and sanitized error message.
- Whether the exported SQL reproduces the same identity after a fresh restore.

Do not include credentials, private data, raw production dumps or sensitive oracle
output. A schema candidate that prevents the application from starting is useful
feedback even when the accepted result still restores successfully.

Reports are classified as installation, compatibility, oracle ergonomics, schema
reduction, performance, documentation or unsupported PostgreSQL features in
[usage reports](docs/adoption.md). When contributing a change, describe the
application behavior it addresses and run the relevant checks in
[the development guide](docs/guide.md#development).
