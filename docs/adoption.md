# Usage reports and known adoption barriers

The Wagtail, NetBox and Mastodon case studies demonstrate workflows tested by the
project maintainers. As of 2026-09-30, no independent usage report has been
verified. Maintainer case studies establish validation results, rather than
external adoption.

[Contribution guidance](../CONTRIBUTING.md) explains installation and how to
report problems. [Issue #1](https://github.com/0then0/dbreduce/issues/1) collects
standalone installation and non-Python oracle reports. Reports from users help
identify which limitations affect applications beyond the published case studies.

## Feedback classification

Usage reports are grouped by installation, compatibility, oracle ergonomics,
schema reduction, performance, documentation and unsupported PostgreSQL features.
A useful report includes the tested versions, a link to the issue or reproducer,
and whether the problem has been reproduced. See the contribution guidance for
the complete reporting checklist.

## Known barriers

- NetBox's v0.4 schema phase attempted 83 candidates. Seventy application
  executions failed before returning a structured verdict. The v0.5 rerun explicitly
  classified 70 as candidate-invalid using the form's declared table preconditions;
  no infrastructure candidates occurred in that new run. Local irreducibility is
  still not claimed.
  See [the case](cases/netbox-17498.md).
- Mastodon validates the same external process boundary using Ruby and Rails,
  without a language-specific DBReduce dependency. Its 601 schema candidates
  included 24 oracle infrastructure errors. A control that removed `accounts`
  failed during Rails boot with `PG::UndefinedTable` before the framed oracle
  could emit a verdict. The v0.5 rerun counted 26
  candidate-invalid (previously negative missing-table verdicts), while 24 unknown
  infrastructure failures remained unresolved. Both cases illustrate the
  distinction between application validity and oracle-environment failures.
- Matching native PostgreSQL clients are the round-trip baseline. A newer client
  may dump an older server but emit SQL that cannot restore there. Installation
  instructions describe this limitation in the [compatibility guide](compatibility.md).
- Full application setup requires application-specific services and settings.
  Mastodon needs Redis and Rails encryption settings for normal boot. These
  belong to the application environment, not to DBReduce core.

v0.5.0 supports the [candidate-validity contract](candidate-invalid.md) for both
strict and framed JSON oracles. Case-specific wrappers may explicitly mark known
invalid schema/data states; unknown application/environment failures remain
infrastructure errors. Current feedback requirements are Python >=3.10,
PostgreSQL 15–18 with matching clients, and DBReduce v0.5.0 after release (or the
current source revision before release). Reports should state how invalidity was
established, separately from negative target checks and unresolved errors.

Issue #1 should be updated to these requirements after v0.5.0 is released; the
local implementation does not itself publish a release or close the issue.
