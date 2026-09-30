# Changelog

Notable user-facing changes.

## [0.5.0] - Unreleased

- Extend strict/framed structured verdicts with explicit `candidate_invalid`,
  rejecting contradictory/unknown outcomes without inferring validity from output.
- Keep invalid application candidates separate from target-negative verdicts,
  different failures, infrastructure failures and invalid schema reconstruction.
- Count validity barriers for row/schema reduction and qualify local minimality
  claims; preserve confirmation, exact-state cache and fresh final restore policies.
- Add case-specific pre-boot Mastodon/NetBox wrappers and protocol/integration
  regressions. No framework integrations or additional schema primitives.

## [0.4.1] - 2026-09-29

- Lower the package's Python minimum to 3.10 after interpreter, type, lint,
  wheel and standalone installation checks on Python 3.10–3.14.
- Test PostgreSQL 15–18 with matching native clients; allow both clone strategies
  from PostgreSQL 15 while keeping `file_copy_method` exclusive to PostgreSQL 18.
- Separate Python checks from PostgreSQL integration CI and gate releases on both.
- Document standalone uv/pip installation, the client/server round-trip baseline,
  compatibility decisions and external adoption reporting.
- Validate Mastodon #37059 through a framed Ruby/Rails migration oracle, with
  data/schema reduction, independent fresh restore and fixed-version control.

## [0.4.0] - 2026-09-28

- Add opt-in `--reduce-schema` after row reduction. Whole application tables and
  their automatic restore dependencies are removed only after a clean candidate
  restore and matching oracle verdict.
- Include schema counts, candidate outcomes, plain SQL sizes, and schema-phase
  timings in reports; show schema search-space counts in `inspect`.

## [0.3.1] - 2026-09-28

- Add a project icon, centered README branding, and this changelog. No runtime
  behavior changes.

## [0.3.0] - 2026-09-28

- Add an opt-in PostgreSQL clone backend with independent candidate and oracle
  databases, safe ownership tracking, promotion of the pristine candidate, and
  final logical restore verification. Snapshot remains the default backend.
- Add `--oracle-framed-json` for one explicitly framed verdict among application
  logs, and `--restore-jobs` for parallel custom-archive restores.
- Add candidate lifecycle phase timings and backend, clone, fallback, and cleanup
  statistics to reports.
- Validate against PostgreSQL 17 and 18; add real-world Wagtail and NetBox
  validation and comparative Wagtail benchmarks. NetBox's issue-author database
  is unavailable; its later public-dataset validation uses the official NetBox
  4.1 demo dump.

## [0.2.0] - 2026-09-27

- Add failure identity checks using strict JSON verdicts, stdout/stderr matchers,
  and optional expected exit codes, with repeated confirmations.
- Add explicitly configured virtual relationships for application-level
  dependencies that are absent from database foreign keys.
- Bound oracle execution and output, clean up oracle process groups, and keep raw
  oracle output and signatures out of reports.

## [0.1.2] - 2026-09-24

- Add automated PyPI publication through GitHub Actions Trusted Publishing.
- Keep project and lockfile versions aligned with release tags.

## [0.1.0] - 2026-09-24

- Add the PostgreSQL CLI to inspect schemas and reduce relational datasets while
  an application oracle checks each candidate.
- Add foreign-key-aware reductions, PostgreSQL dump/restore isolation, and plain
  SQL plus JSON report exports.
- Handle constraint and trigger rejections, large archives, workspace cleanup
  failures, and concurrent workspace lifecycle operations.
- Add PostgreSQL-backed tests, Ruff and mypy checks, and tagged GitHub releases
  with installable wheel and source archive artifacts.
