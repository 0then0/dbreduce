# NetBox issue #17498

## Opt-in schema validation (v0.4.0)

On 2026-09-28, the public NetBox 4.1 demo SQL was restored again, and the
duplicate-manufacturer issue condition was added with NetBox v4.1.1. The real
form oracle reproduced the issue on 21,381 rows in 180 tables. A full
`--candidate-backend clone --reduce-schema --oracle-json --confirm 2` run on
PostgreSQL 17.11 produced:

| Measure                      |          Before |        After |
| ---------------------------- | --------------: | -----------: |
| Rows                         |          21,381 |            2 |
| Tables                       |             180 |            4 |
| Indexes                      |             875 |           16 |
| Constraints                  |             869 |           14 |
| Sequences                    |             178 |            4 |
| Non-data archive TOC entries |           1,577 |           27 |
| Plain logical SQL            | 2,738,171 bytes | 11,965 bytes |

The four retained tables are `dcim_manufacturer`, `django_content_type`,
`extras_customfield`, and `extras_customfield_object_types`. The schema phase
attempted 83 candidates and accepted 13; it took 403.791 seconds of the
698.040-second run. On 70 candidates, the application oracle exited before
providing a structured verdict. DBReduce rejected all of them as oracle
infrastructure errors. Its report does not claim local irreducibility for this
NetBox result. The final identity matched after DBReduce's fresh logical restore.
The published SQL was also loaded with `psql` into a separate PostgreSQL 17
database; the same NetBox form oracle returned the original signature. This
uses the public demo dataset plus the issue condition, not the unavailable
reporter's database.

## Project

- Project: [netbox-community/netbox](https://github.com/netbox-community/netbox)
- Issue: [#17498](https://github.com/netbox-community/netbox/issues/17498)
- Affected version in issue: NetBox v4.1.1, Python 3.10
- Tested version: official Docker image `netboxcommunity/netbox:v4.1.1`, Python 3.12.3
- Fix PR: [#17593](https://github.com/netbox-community/netbox/pull/17593)
- Fix commit: `ee3efa8c2c2e18319a3f9e2b01e8157aebbfb0dd`
- Merge commit: `cc6f21ded21533686e34f23322d2b307b1a9e6ec`

## Bug

NetBox bulk import allowed `manufacturer.description` as a CSV lookup header.
When two manufacturers had the same description, `DeviceTypeImportForm` used a
plain Django `ModelChoiceField` and raised
`dcim.models.devices.Manufacturer.MultipleObjectsReturned` instead of returning a
validation error.

The oracle exercises the real NetBox v4.1.1 form path:

```json
{
  "reproduced": true,
  "signature": "netbox-17498-manufacturer-description-multiple-objects"
}
```

The baseline control was:

- Create two `dcim_manufacturer` rows with `description = 'd'`.
- Instantiate `DeviceTypeImportForm` with `headers={"manufacturer": "description"}`.
- `form.is_valid()` raises `Manufacturer.MultipleObjectsReturned`.
- Delete one duplicate manufacturer: the oracle returns `{"reproduced": false}`.

## Dataset

The exact database used by the issue author is unavailable. [Issue #17498](https://github.com/netbox-community/netbox/issues/17498)
describes how to create two manufacturers with the same description and shows
the resulting exception; it does not include a database dump. The earlier
1,335-row validation database was reconstructed with NetBox v4.1.1 migrations
and setup scripts. It is not the issue author's database.

The official [NetBox 4.1 demo SQL dump](https://github.com/netbox-community/netbox-demo-data/blob/master/sql/netbox-demo-v4.1.sql)
is a public alternative source dataset for this application version. It was
restored into PostgreSQL 17 (the dump's original PostgreSQL version is 14.12),
after creating its referenced `netbox` role. The duplicate-manufacturer
condition from the issue was then added. The NetBox v4.1.1 form oracle reproduced
`netbox-17498-manufacturer-description-multiple-objects` on that restored
dataset. This validates the demo dataset and application path, not the original
user database.

### Public demo dataset run

On 2026-09-28, the official NetBox 4.1 demo dump was restored to PostgreSQL
17.11. The issue condition was added through the NetBox v4.1.1 ORM, then the
clone backend completed a full reduction:

- Initial: 21,381 rows across 180 tables.
- Final: 2 rows.
- Elapsed: 262.958s, including 113 oracle executions.
- Backend: clone / `WAL_LOG`; 177 created databases, all 177 dropped, no cleanup
  failures.
- Final signature matched the initial signature.
- The published plain SQL was loaded into a new database with PostgreSQL 17.11
  `psql`; the same NetBox oracle reproduced the signature after that fresh
  restore.

This is one end-to-end validation run, not a repeated performance benchmark.

### Earlier reconstructed-dataset configuration

The reconstructed-dataset run used snapshot mode, `--oracle-json`, `--confirm 2`
and `--timeout 60`, without virtual relationships. Its external wrapper invoked
the form oracle in the official NetBox v4.1.1 container and read the workspace
connection from `DATABASE_URL`. The wrapper was specific to the validation
environment; the results below describe that run rather than provide a standalone
setup script.

### Earlier reconstructed-dataset result

- Initial: 1,335 rows, 179 tables, 5 populated tables.
- Final: 2 rows, 179 tables, 1 populated table.
- Reduction: 99.8502%.
- Final populated table: `dcim_manufacturer`.
- Failure identity: preserved.
- Fresh restore: PASS on PostgreSQL 17.
- Elapsed time: 91.179s.
- Oracle time: 27.983s.
- Oracle executions: 16.
- Candidate probes: 9.
- Accepted candidates: 3.
- Rejected candidates: 6.
- Cache hits: 0.

## Oracle requirements and limitations

- The official NetBox Docker configuration prints configuration load messages to
  stdout during `django.setup()`. The oracle wrapper redirects that startup noise
  to stderr so DBReduce JSON mode receives a single JSON object.
- NetBox delete signals consult Redis-backed configuration cache. The setup and
  negative control used raw SQL deletion to avoid requiring Redis. The positive
  oracle still exercises the real NetBox form path that triggers the issue.
- This validates the application/form path, not a complete HTTP end-to-end flow.
