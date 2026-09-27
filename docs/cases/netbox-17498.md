# NetBox issue #17498

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

The source database used NetBox's real v4.1.1 migrations on PostgreSQL 17. It had
1,335 rows across 179 tables, with 5 populated tables before reduction. This case
was intentionally smaller than the Wagtail benchmark; Wagtail was used for the
100k+ validation run.

## DBReduce command

```bash
TMPDIR=/private/tmp PATH=/private/tmp/dbreduce-pgtools:$PATH \
  .venv/bin/python -m dbreduce reduce \
  --database postgresql://postgres:postgres@localhost:55432/dbreduce_netbox_17498 \
  --oracle '/private/tmp/dbreduce-validation/netbox/netbox_oracle.sh oracle' \
  --oracle-json --confirm 2 --timeout 60 \
  --output /private/tmp/dbreduce-netbox-17498.min.sql \
  --report /private/tmp/dbreduce-netbox-17498.report.json
```

No virtual relationships were used.

## Result

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

## Notes

- The official NetBox Docker configuration prints configuration load messages to
  stdout during `django.setup()`. The oracle wrapper redirects that startup noise
  to stderr so DBReduce JSON mode receives a single JSON object.
- NetBox delete signals consult Redis-backed configuration cache. The setup and
  negative control used raw SQL deletion to avoid requiring Redis. The positive
  oracle still exercises the real NetBox form path that triggers the issue.
