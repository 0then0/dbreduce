# Wagtail issue #9208

## Project

- Project: [wagtail/wagtail](https://github.com/wagtail/wagtail)
- Issue: [#9208](https://github.com/wagtail/wagtail/issues/9208)
- Tested version: Wagtail 8.0 from PyPI, Django 6.1.1, Python 3.13
- Original report: Wagtail 2.15.5 with later comments confirming the same behavior on Wagtail 4.x and analysis from a Wagtail contributor in 2026.
- Fix: no fix PR was linked when validated; the issue was still open.

## Bug

Renaming a collection can leave `wagtailcore_collection.path` out of the
alphabetical order expected by django-treebeard. Adding another collection under
the same parent can then raise a duplicate key/unique constraint error on
`wagtailcore_collection.path`.

The oracle exercises the real `wagtail.models.Collection` tree operation and
returns this structured verdict:

```json
{ "reproduced": true, "signature": "wagtail-9208-collection-path-integrity" }
```

The baseline control was:

- Seed `Amphibians`, `Fish`, `Crocodiles`, then rename `Crocodiles` to `Reptiles`.
- Add `Mammals`: oracle returns the signature above.
- Run `fixtree --full`: the same oracle returns `{"reproduced": false}`.

## Dataset

The source database used PostgreSQL 17 and Wagtail's real migrations. It had
150,706 rows across 48 tables. Most noise was in real Django/Wagtail tables:
50,000 users, 250 groups, 50,000 user/group membership rows, and 50,000 sessions.

This was intentionally not a minimal fixture. The bug itself needs three
`wagtailcore_collection` rows, while the surrounding database was a larger
application schema with real foreign keys. The surrounding rows were generated
application noise; this was not a production database dump.

## DBReduce command

```bash
TMPDIR=/private/tmp PATH=/private/tmp/dbreduce-pgtools:$PATH \
  .venv/bin/python -m dbreduce reduce \
  --database postgresql://postgres:postgres@localhost:55432/dbreduce_wagtail_9208 \
  --oracle 'docker exec -e DATABASE_URL="$DATABASE_URL" dbreduce-wagtail-validation python wagtail_case.py oracle' \
  --oracle-json --confirm 2 --timeout 60 \
  --output /private/tmp/dbreduce-wagtail-9208.min.sql \
  --report /private/tmp/dbreduce-wagtail-9208.report.json
```

No virtual relationships were used.

## Result

- Initial: 150,706 rows, 48 tables, 20 populated tables.
- Final: 3 rows, 48 tables, 1 populated table.
- Reduction: 99.9980%.
- Final populated table: `wagtailcore_collection`.
- Failure identity: preserved.
- Fresh restore: PASS on PostgreSQL 17.
- PostgreSQL 18 integration check: PASS by restoring the minimized SQL and running the same oracle.

The three repeated PostgreSQL 17 runs used the same source dataset, oracle,
hardware, PostgreSQL settings and DBReduce version:

| Run |  Elapsed | Oracle time | Oracle executions | Candidate probes | Accepted | Rejected | Cache hits |
| --- | -------: | ----------: | ----------------: | ---------------: | -------: | -------: | ---------: |
| 1   | 103.316s |     23.495s |                33 |               21 |        9 |       12 |          1 |
| 2   | 103.419s |     23.606s |                33 |               21 |        9 |       12 |          1 |
| 3   | 104.722s |     23.599s |                33 |               21 |        9 |       12 |          1 |

Median elapsed time: 103.419s. Range: 103.316s to 104.722s.

## Notes

- A first oracle draft aborted reduction when a candidate removed application
  metadata and Wagtail raised a non-target exception. The oracle was narrowed so
  non-target exceptions return `{"reproduced": false}`. This is an oracle quality
  requirement, not a DBReduce feature change.
- The default `fixtree` command did not clear the condition in this environment;
  `fixtree --full` did.
- The final dataset is small because the failure depends only on three collection
  rows and the schema constraints allow all unrelated Django/Wagtail rows to be
  deleted.
