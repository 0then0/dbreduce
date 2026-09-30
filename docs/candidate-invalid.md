# Candidate validity and the structured oracle contract (v0.5)

An invalid candidate is not evidence that the target bug disappeared. An unknown
application crash is not evidence that a candidate is invalid. Only the application
oracle may explicitly make that distinction. This contract applies to row and
schema candidates; it requires no framework integration or new CLI option.

## Verdicts and compatibility

The v0.4.1 verdicts remain supported without changes:

```json
{ "reproduced": true, "signature": "bug-x" }
```

```json
{ "reproduced": false }
```

v0.5 adds one verdict:

```json
{ "reproduced": false, "outcome": "candidate_invalid" }
```

Its `reproduced` field must be the JSON boolean `false`. `signature` must be
absent, including a null signature. The only supported explicit `outcome` is
`candidate_invalid`; unknown values and non-string values, including null,
are protocol errors. A non-null `error` still means infrastructure error and
cannot be overridden by `outcome`. Other extra fields retain the old ignored-field
behavior. There is no reason/message field interpreted or saved by DBReduce.

For comparison, the old success-verdict schema required an object with a boolean
`reproduced`, and a nonempty string `signature` of at most 256 characters when
`reproduced` was true. A non-null `error` rejected the verdict. The new success
schema adds these constraints when `outcome` is present:

```json
{
  "if": { "required": ["outcome"] },
  "then": {
    "properties": {
      "outcome": { "const": "candidate_invalid" },
      "reproduced": { "const": false }
    },
    "not": { "required": ["signature"] }
  }
}
```

Both `--oracle-json` and `--oracle-framed-json` use the same validation. Strict
mode requires one JSON object on stdout. Framed mode requires exactly one line:

```text
Rails log...
DBREDUCE_VERDICT {"reproduced":false,"outcome":"candidate_invalid"}
Rails log...
```

Zero/multiple framed verdicts, malformed JSON, nonzero exit status, launch failure,
signal, output overflow or timeout remain infrastructure failures. As in v0.4.1,
a structured command must exit **zero**; even a valid invalid-candidate verdict
cannot override nonzero status. Legacy any-nonzero and regex matchers have no
candidate-invalid semantics. DBReduce never inspects stderr to infer validity.

## Outcomes and confirmation

`OracleOutcome` is a typed enum. Its existing report values are preserved:

- `SAME_FAILURE` (`same_failure`): the exact target identity survived; accept only
  after N/N fresh-state executions under `--confirm N`.
- `NOT_REPRODUCED` (`passed`): a successful negative verdict; reject immediately.
- `CANDIDATE_INVALID` (`candidate_invalid`): application preconditions failed under
  the declared validity contract; reject immediately, without setting an identity.
- `DIFFERENT_FAILURE` (`different_failure`): another signature; reject, never treat
  it as invalid merely because the database changed.
- `INFRASTRUCTURE_ERROR` (`infrastructure_error`), or its existing `timeout`
  classification: no trustworthy verdict. Schema probes reject and continue;
  data probes, baseline and final errors retain their existing abort policy.

Database reconstruction rejection remains separate: schema counters
`invalid_schema` and `constraint_rejected` do not mean application-invalid.

A same-failure execution followed by candidate-invalid rejects the candidate.
Because invalid and negative verdicts reject early, a later same-failure or
infrastructure outcome is not sampled after them. This is a deterministic-oracle
contract, not a probabilistic stability guarantee; no extra retries are added.
The final result still requires an uncached fresh logical restore and N/N matching
failure identities. Final candidate-invalid aborts without publishing SQL/report.

## Reports, minimality and cache

`oracle_stats.outcomes.candidate_invalid` counts actual oracle executions.
`schema_reduction.candidates.candidate_invalid` counts schema probes rejected by
that verdict, separately from `bug_disappeared`, `different_failure`, `invalid_schema`,
`constraint_rejected` and `oracle_infrastructure_error`. Data-phase
`candidate_stats.candidate_invalid` counts uncached probes rejected by that verdict;
cache hits are still included in total attempted/rejected probes, not executions.

Schema minimality adds `blocked_by_candidate_invalid` and `infrastructure_errors`.
`locally_irreducible` is false when application validity, restore rejection,
infrastructure errors or different failures prevent a target-negative check.
Data validity/different-failure barriers also make data local irreducibility false,
reported in `minimality_by_phase`. The top-level `minimality` string then reads
`local irreducibility not established`. Otherwise the existing wording remains.
Phase counters/barriers are cumulative, conservatively including earlier states;
DBReduce does not claim that an invalid removal is necessary for the target bug.
A negative target check and an unassessable candidate are different evidence.

Snapshot caching retains the complete normalized SQL fingerprint, including
schema, sequence state and row multiplicities. It caches acceptance/rejection,
so an explicit invalid verdict becomes a cached rejection under the existing
per-run determinism assumption. The first uncached invalid probe already records
the phase's validity barrier. No identities based only on removed object names
are introduced. Clone and schema caching remain disabled. Final confirmation
always bypasses the cache. Raw stdout/stderr, signatures, URLs and secrets are
not added to reports.

## Application-specific wrappers

A wrapper may run before application boot. It must establish a narrowly declared
application invariant, not classify every startup exception or match arbitrary
exception text. Connection, credentials and service errors must propagate.

The [Mastodon wrapper](../examples/mastodon-37059/wrapper.rb) queries the candidate
before Rails boot. This case declares `public.accounts` (boot) and
`public.settings` (target migration) required. Only a successful metadata query
proving one absent emits candidate-invalid. Otherwise it executes the real Rails
oracle; unknown boot/migration failures propagate without a verdict.

The [NetBox wrapper](../examples/netbox-17498/wrapper.py) checks the four tables
required by the specific CSV form path, then executes the [real form oracle](../examples/netbox-17498/oracle.py).
It runs inside the existing NetBox environment, with `PYTHONPATH=/opt/netbox/netbox`
and the normal Docker configuration. It does not install DBReduce or framework
packages. Only the target `Manufacturer.MultipleObjectsReturned` exception means
same failure; unknown exceptions propagate.

These preconditions deliberately describe what these wrappers can evaluate;
absence does not prove a table causes the target defect. Their known-table sets
are case-specific, not a universal Rails/Django diagnosis. Service failure after
a missing-table preflight is not observed because the candidate was already
rejected; preflight does not certify the rest of the environment as healthy.
See the case studies for controls, measured distributions and remaining limits.
