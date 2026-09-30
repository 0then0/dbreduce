# Mastodon issue #37059

## Candidate validity validation (v0.5.0 source, 2026-09-30)

The preserved data-only export was restored unchanged on PostgreSQL 17.11:
two settings rows with the full 109-table schema. The same schema phase ran
with snapshot isolation, `--reduce-schema --oracle-framed-json --confirm 2
--timeout 60`, using the real v4.5.2 image and the
[pre-boot wrapper](../../examples/mastodon-37059/wrapper.rb). DBReduce used Python
3.13 with native PG17 clients; Redis 7 and the normal generated application
settings were available on the disposable network.

Before/after schema outcomes:

- v0.4.1: 601 attempted, 27 accepted, 26 negative verdicts, 524 invalid-schema
  reconstructions, 24 oracle infrastructure errors.
- v0.5: 601 attempted, 27 accepted, 26 candidate-invalid, zero negative schema
  verdicts, 524 invalid-schema reconstructions, 24 oracle infrastructure errors.
- Tables: 109 → 12; schema objects: 868 → 116; rows: two → two.
- Plain SQL: 262,004 → 44,111 bytes. Final failure identity is unchanged.
- v0.5 elapsed: 618.422s (584.482s schema); 111 oracle executions, comprising
  58 same-failure confirmations, three data-negative verdicts, 26 candidate-invalid
  and 24 infrastructure errors. Schema used 104 executions: 54 same-failure,
  26 candidate-invalid and 24 infrastructure errors.
- The historical schema-input run took 531.926s (474.437s schema), also 111
  executions. Concurrent PostgreSQL matrix tests and NetBox reduction ran during
  this validation, so these timings are not an acceleration benchmark.
- DBReduce created/dropped 829 databases, with zero cleanup failures. Schema
  `locally_irreducible` is false, `blocked_by_candidate_invalid` is true, and
  `infrastructure_errors` is 24.

The wrapper declares `accounts` required for boot and `settings` required for the
target migration. Only successful metadata queries proving one absent emit
candidate-invalid. The 26 invalid verdicts correspond to candidates that the old
migration oracle classified as negative after a missing-table exception; they
are not evidence of target-bug disappearance. The 24 infrastructure outcomes
remain unresolved under this narrow contract. They were not relabeled from
stderr or from the fact that schema changed. The standalone missing-`accounts`
control is explicitly recognized, but does not imply that these 24 restore-valid
schema probes removed `accounts` or that every Rails boot failure is understood.

A separate native `psql` restore of the unmodified final SQL reproduced the exact
v4.5.2 signature. On a disposable copy, removing `accounts` emitted
candidate-invalid before Rails boot. Removing `landing_page` produced an ordinary
negative verdict. A connection to an unreachable PostgreSQL port failed nonzero
without a verdict. The same final SQL and wrapper in fixed v4.5.3 returned
`reproduced: false` through the unmodified migration. These controls validate
explicit known invalidity and preserve unknown failures as infrastructure errors.


## Project and issue

- Project: [mastodon/mastodon](https://github.com/mastodon/mastodon).
- Issue: [#37059](https://github.com/mastodon/mastodon/issues/37059), closed.
- Language/framework: Ruby 3.4.7, Rails/ActiveRecord 8.0.3.
- Affected application: official Mastodon v4.5.2 image,
  `ghcr.io/mastodon/mastodon@sha256:cadce590949fbd3ecc38df221d0fc406cce8533fa68860fc3087a9cc21fa5225`.
- Fixed application: official v4.5.3 image,
  `ghcr.io/mastodon/mastodon@sha256:b862a0f0f20dafb26597e6d59768ee73cdeab08fa8ecaa3ecf95ccf38c443657`.
- Fix: [PR #37079](https://github.com/mastodon/mastodon/pull/37079), merge commit
  `e5651e7e048f644cc521b067186e738a6cfade16`.
- PostgreSQL server and native clients: 17.11; Redis 7 for normal Rails boot.
- DBReduce: source version prepared for v0.4.1, Python 3.10.21,
  Linux arm64.
- Validation date: 2026-09-29.

## Bug

Migration `20251023210145_migrate_landing_page_setting.rb` reads
`trends_as_landing_page` and upserts `landing_page`. In v4.5.2 its `upsert` does
not specify `unique_by`. ActiveRecord chooses the primary key conflict target,
so an already-present `settings.var = 'landing_page'` violates the separate unique
index `index_settings_on_var` and raises `ActiveRecord::RecordNotUnique` caused by
`PG::UniqueViolation`.

The fix resets column information and selects `unique_by` according to the unique
index available on the upgrade path. The v4.5.2 schema has the single-column
unique index from the issue. No proprietary
service, credentials or production infrastructure is needed; normal boot needs
PostgreSQL, Redis and freshly generated Rails encryption settings.

## Oracle

[The Ruby oracle](../../examples/mastodon-37059/oracle.rb) runs inside the real
Mastodon application with `bundle exec rails runner`, invoked by the wrapper. It loads the application's
unaltered migration file and calls `MigrateLandingPageSetting.new.migrate(:up)`.
It does not reimplement the failing operation as SQL. Only the specific exception,
constraint and `landing_page` detail produce:

```text
DBREDUCE_VERDICT {"reproduced":true,"signature":"mastodon-37059-landing-page-unique-violation"}
```

Successful migration returns `reproduced: false`. The v0.4.1 script returned a negative verdict for a missing migration table;
other exceptions propagated, and boot-time errors could prevent any verdict.
In v0.5 the [wrapper](../../examples/mastodon-37059/wrapper.rb) runs before Rails
boot and explicitly checks the case's required `accounts` and `settings` tables.
Missing declared tables emit candidate-invalid. The migration oracle now lets
unknown database/application exceptions propagate.
Framed mode accepts Rails migration logging without redirecting or filtering it.
The migration is enclosed in a rollback transaction; PostgreSQL sequences are
not transactional, so controls should use disposable database copies.

The application oracle was checked on the bug
state, a separate restored database with `landing_page` removed, and fixed
Mastodon v4.5.3. The affected version returned the signature; both negative
controls returned `reproduced: false`.

## Dataset and setup

The dataset is reconstructed application state, not the issue reporter's dump.
Mastodon's real v4.5.2 `rails db:schema:load` creates 109 ordinary tables plus its
real views, materialized views, indexes, functions, constraints and sequences.
The migration's marker is removed from `schema_migrations` to model an upgrade
with that migration pending. The oracle invokes only that migration, not the full
historical migration chain.

Run the following setup from the DBReduce repository root. Docker must be
running; the tool image supplies matching native PostgreSQL clients and installs
DBReduce from the mounted source. The dataset and generated application secrets
belong to this disposable environment.

On a dedicated Docker network, the service names are
`dbreduce-adoption-pg17`, `dbreduce-adoption-redis` and
`dbreduce-adoption-mastodon`. The latter uses the v4.5.2 image, a read-only mount
of `examples/mastodon-37059` at `/case`, and these application settings:

```text
RAILS_ENV=production
LOCAL_DOMAIN=case.invalid
DB_HOST=dbreduce-adoption-pg17
DB_USER=postgres
DB_NAME=mastodon_case
DB_PORT=5432
REDIS_HOST=dbreduce-adoption-redis
ES_ENABLED=false
```

Generate fresh `SECRET_KEY_BASE`, `OTP_SECRET`,
`ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY`,
`ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT` and
`ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY` for this disposable environment. For
example, the image's Ruby can print a new hex value with
`ruby -rsecurerandom -e 'puts SecureRandom.hex(64)'`. Keep these environment values
outside the repository. Do not reuse application production secrets.

The following commands create the services, environment file and tool image.
The PostgreSQL server uses trust authentication only on the dedicated Docker
network and publishes no host port. The tool container needs the Docker socket
to execute the application oracle; use this setup with trusted code and dumps.

```bash
case_dir=$(mktemp -d)
umask 077
docker network create dbreduce-adoption
docker run -d --name dbreduce-adoption-pg17 --network dbreduce-adoption \
  -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17-trixie
docker run -d --name dbreduce-adoption-redis --network dbreduce-adoption redis:7-alpine

printf '%s\n' \
  RAILS_ENV=production LOCAL_DOMAIN=case.invalid \
  DB_HOST=dbreduce-adoption-pg17 DB_USER=postgres DB_NAME=mastodon_case DB_PORT=5432 \
  REDIS_HOST=dbreduce-adoption-redis ES_ENABLED=false > "$case_dir/mastodon.env"
docker run --rm --entrypoint ruby ghcr.io/mastodon/mastodon:v4.5.2 \
  -rsecurerandom -e 'ARGV.each { |name| puts "#{name}=#{SecureRandom.hex(64)}" }' \
  SECRET_KEY_BASE OTP_SECRET ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY \
  ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY \
  >> "$case_dir/mastodon.env"
docker run -d --name dbreduce-adoption-mastodon --network dbreduce-adoption \
  --env-file "$case_dir/mastodon.env" \
  -v "$PWD/examples/mastodon-37059:/case:ro" \
  --entrypoint sleep ghcr.io/mastodon/mastodon:v4.5.2 infinity
docker run -d --name dbreduce-adoption-mastodon-fixed --network dbreduce-adoption \
  --env-file "$case_dir/mastodon.env" \
  -v "$PWD/examples/mastodon-37059:/case:ro" \
  --entrypoint sleep ghcr.io/mastodon/mastodon:v4.5.3 infinity
docker build -t dbreduce-mastodon-tool examples/mastodon-37059

until docker exec dbreduce-adoption-pg17 pg_isready -U postgres -d postgres; do
  sleep 1
done
```

Load the application's real schema:

```bash
docker exec dbreduce-adoption-pg17 createdb -U postgres mastodon_case
docker exec dbreduce-adoption-mastodon bundle exec rails db:schema:load
```

Seed the issue and generated noise through `psql` in that newly created database:

```bash
docker exec -i dbreduce-adoption-pg17 psql -X -v ON_ERROR_STOP=1 \
  -U postgres -d mastodon_case <<'SQL'
INSERT INTO settings (var, value)
VALUES ('trends_as_landing_page', '--- true'), ('landing_page', '--- about');
INSERT INTO settings (var, value)
SELECT 'dbreduce_noise_' || i, '--- noise' FROM generate_series(1, 1000) i;
INSERT INTO domain_allows (domain, created_at, updated_at)
SELECT 'noise-' || i || '.invalid', now(), now() FROM generate_series(1, 100) i;
INSERT INTO tags (name, created_at, updated_at)
SELECT 'noise' || i, now(), now() FROM generate_series(1, 100) i;
DELETE FROM schema_migrations WHERE version = '20251023210145';
SQL
```

Initial state: 1,757 rows, 109 tables, five populated tables. The migration marker
and other metadata contribute 555 rows; generated noise contributes 1,200 rows;
two settings rows trigger the issue. Setup/control SQL is explicit; the positive
oracle always passes through the real Rails migration.

Check the baseline and negative controls on disposable copies. The first
command returns the target signature; the second and third return
`reproduced: false`.

```bash
docker exec dbreduce-adoption-pg17 psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
  -c 'CREATE DATABASE mastodon_bug_control TEMPLATE mastodon_case' \
  -c 'CREATE DATABASE mastodon_negative_control TEMPLATE mastodon_case'
docker exec dbreduce-adoption-pg17 psql -X -v ON_ERROR_STOP=1 \
  -U postgres -d mastodon_negative_control \
  -c "DELETE FROM settings WHERE var = 'landing_page'"
docker exec -e DATABASE_URL=postgresql://postgres@dbreduce-adoption-pg17/mastodon_bug_control \
  dbreduce-adoption-mastodon bundle exec ruby /case/wrapper.rb
docker exec -e DATABASE_URL=postgresql://postgres@dbreduce-adoption-pg17/mastodon_negative_control \
  dbreduce-adoption-mastodon bundle exec ruby /case/wrapper.rb
docker exec -e DATABASE_URL=postgresql://postgres@dbreduce-adoption-pg17/mastodon_bug_control \
  dbreduce-adoption-mastodon-fixed bundle exec ruby /case/wrapper.rb
```

## DBReduce command

Run DBReduce in the tool container on the same network. `/evidence` stores its
outputs and isolated Python environment in `$case_dir`; the application and
repository mounts remain separate.

```bash
docker run --rm --network dbreduce-adoption \
  -v "$PWD:/repo:ro" -v "$case_dir:/evidence" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  dbreduce-mastodon-tool --help
docker run --rm --network dbreduce-adoption \
  -v "$PWD:/repo:ro" -v "$case_dir:/evidence" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  dbreduce-mastodon-tool reduce \
  --database postgresql://postgres@dbreduce-adoption-pg17/mastodon_case \
  --oracle 'docker exec -e DATABASE_URL="$DATABASE_URL" dbreduce-adoption-mastodon bundle exec ruby /case/wrapper.rb' \
  --oracle-framed-json --confirm 2 --timeout 60 \
  --output /evidence/mastodon-data.min.sql \
  --report /evidence/mastodon-data.report.json
```

The equivalent command inside that tool environment is:

```bash
dbreduce reduce \
  --database postgresql://postgres@dbreduce-adoption-pg17/mastodon_case \
  --oracle 'docker exec -e DATABASE_URL="$DATABASE_URL" dbreduce-adoption-mastodon bundle exec ruby /case/wrapper.rb' \
  --oracle-framed-json --confirm 2 --timeout 60 \
  --output /evidence/mastodon-data.min.sql \
  --report /evidence/mastodon-data.report.json
```

The oracle uses DBReduce's standard executable contract without virtual
relationships or framework adapters. **DBReduce core required no language-specific integration.**

## Before/after

Data reduction completed before schema reduction:

- Rows: 1,757 → 2; populated tables: 5 → 1.
- Tables: 109 → 109. Only `settings` remains populated.
- Oracle executions: 63; elapsed: 527.215 seconds.
- Candidate probes: 40; accepted: 19; rejected: 21.
- Oracle outcomes: 42 same-failure confirmations, 21 negative verdicts.
- Plain SQL: 262,004 bytes after row reduction.
- Failure identity: `mastodon-37059-landing-page-unique-violation`, preserved.
- DBReduce-created databases: 146 created, 146 dropped; no cleanup failures.

The schema phase then used the independently restored data-only SQL as its input,
with the same command and `--reduce-schema`, outputting `mastodon-schema.min.sql`
and `mastodon-schema.report.json`. This avoids repeating the 1,755-row removal
while preserving the full 109-table schema as the schema-phase starting state.

- Tables: 109 → 12; schema objects: 868 → 116.
- Rows: 2 → 2; populated tables: 1 → 1.
- Plain SQL: 262,004 → 44,111 bytes.
- Oracle executions: 111; elapsed: 531.926 seconds, including 474.437 seconds
  in the schema phase. Combined data and schema runs took 1,059.141 seconds.
- Schema candidates: 601 attempted; 27 accepted; 574 rejected, comprising
  524 invalid-schema reconstructions, 26 negative verdicts and 24 oracle
  infrastructure errors. Infrastructure errors do not claim bug disappearance.
- Failure identity: unchanged. Data is locally irreducible under attempted
  transformations; schema local irreducibility is **not claimed**.
- DBReduce-created databases: 829 created, 829 dropped; no cleanup failures.

Native plain dumps of the schema-phase input were byte-identical between a
snapshot taken approximately 221 seconds into the run and a snapshot taken after
completion, using the same `--restrict-key` value. This comparison covers that
interval, not the entire run. Full-lifecycle source-preservation, ownership and
oracle-write isolation integration tests passed on PG15–18.

## Fresh restore and fixed-version control

For the data-only output, restore the SQL and check both application versions:

```bash
docker exec -i dbreduce-adoption-pg17 psql -X -v ON_ERROR_STOP=1 \
  -U postgres -d postgres < "$case_dir/mastodon-data.min.sql"
restore_database=$(docker run --rm --user 0 -v "$case_dir:/evidence:ro" \
  --entrypoint ruby ghcr.io/mastodon/mastodon:v4.5.2 -rjson \
  -e 'puts JSON.parse(File.read(ARGV.fetch(0))).fetch("restore_database")' \
  /evidence/mastodon-data.report.json)
docker exec -e DATABASE_URL="postgresql://postgres@dbreduce-adoption-pg17/$restore_database" \
  dbreduce-adoption-mastodon bundle exec ruby /case/wrapper.rb
docker exec -e DATABASE_URL="postgresql://postgres@dbreduce-adoption-pg17/$restore_database" \
  dbreduce-adoption-mastodon-fixed bundle exec ruby /case/wrapper.rb
```

To repeat the schema phase, run the tool-container reduction command against
`postgresql://postgres@dbreduce-adoption-pg17/$restore_database` with
`--reduce-schema`, changing the output and report names to `mastodon-schema.min.sql`
and `mastodon-schema.report.json`. Repeat the restore and oracle commands above
with those filenames to verify the schema-reduced export.

DBReduce's own final uncached logical-restore confirmation is followed by a
separate native `psql` restore into a newly created database. Exported SQL contains
`CREATE DATABASE` and `\connect`; execute it while connected to the administrative
`postgres` database, and run the oracle against the newly created database named
in that SQL. Do not precreate or rename its destination, strip restricted-mode
commands or run the oracle against the initial `psql --dbname` by mistake.

For the data-only export, the exact unmodified SQL restored successfully. The
v4.5.2 application returned the original signature. The same restored reproducer
and the same Ruby oracle in v4.5.3 returned `reproduced: false`; its migration
completed normally. No migration was patched in place for this negative control.

The final 44,111-byte schema-reduced export was also restored unmodified into a
completely new database. The same v4.5.2 Rails oracle returned
`mastodon-37059-landing-page-unique-violation`; the same v4.5.3 oracle completed the
migration and returned `reproduced: false`. Both checks used the real application
images listed above, without substituting or editing their migration files.

## Limitations

This validates a real Rails migration path and reconstructed data-dependent
upgrade failure. It is not a full web/streaming federation deployment, production
corpus or replay of every historical Mastodon migration. Redis is present for
normal application boot. Other database objects are retained when required by
logical-restore dependencies; views/functions are not independent minimization
targets. Author-driven validation is not independent adoption.

## Cleanup

When finished, remove only the services created above. Keep `$case_dir` for its
SQL and reports; it also contains the disposable environment's generated secrets.

```bash
docker rm -f dbreduce-adoption-mastodon dbreduce-adoption-mastodon-fixed \
  dbreduce-adoption-redis dbreduce-adoption-pg17
docker network rm dbreduce-adoption
```
