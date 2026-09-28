# Changelog

Notable user-facing changes.

## [0.4.0] - Unreleased

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
