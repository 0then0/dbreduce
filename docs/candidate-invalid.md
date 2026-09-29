# Application-invalid schema candidates

This design discussion describes a limitation observed in schema reduction and
a possible extension to the oracle protocol. The proposed outcome is not
implemented. For the supported verdict format, see [the guide](guide.md#failure-identity).

## Evidence

Two maintainer-validated application cases have candidates that cannot reach an application
verdict because a required schema object is absent:

- NetBox #17498: 70 of 83 schema candidates ended in oracle infrastructure errors.
- Mastodon #37059: 24 of 601 schema candidates ended in oracle infrastructure
  errors. A separate control that removed Mastodon's `accounts` table failed
  during Rails boot with `PG::UndefinedTable` before `oracle.rb` could emit its
  framed verdict.

These outcomes differ from the target bug disappearing and from a broken oracle
environment such as an unavailable service. DBReduce currently rejects all of
them as infrastructure errors, so it does not claim local schema irreducibility
for either case. This is correct but makes schema reduction less informative.

## Proposed outcome (not supported)

An application oracle could explicitly classify a candidate it can recognize as
invalid before reaching the target behavior:

```json
{ "reproduced": false, "outcome": "candidate_invalid" }
```

The reducer could record that outcome separately while treating it as a rejected
candidate. It must not infer it from exception text: startup failures can also
mean a missing service, bad credentials or an application defect. Any design
would need a stable contract, treatment of pre-oracle boot failures, reports and
tests across independent applications.

The proposal remains open until usage reports establish how often this distinction
is needed and how application oracles can classify it reliably. It would extend
the language-independent oracle contract rather than require a framework adapter.
