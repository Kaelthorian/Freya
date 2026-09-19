# Integration proof contract (4.6, version 2)

An accepted global integration requires every original global criterion exactly
once, satisfied, with nonempty evidence consisting only of proof references
permitted for that criterion. This also applies to a satisfied criterion inside
a non-accepted decision. A known reference alone is not proof.

## Catalog and deterministic association

`integration_proof.py` builds bounded metadata from active accepted tasks and
their immutable evaluations. `task:*` and `evaluation:*` are context only.
`evidence:*` is proof when its evaluation criterion is satisfied, is declared
by that task, and matches the global criterion after whitespace and case
normalization. `verification:*` must be a passed runtime check on a task with
a declared, satisfied local criterion matching the global criterion. The
association is task-scoped; check output and model explanations cannot create
new associations. Unrelated task verification cannot be reused.

At most one evaluator evidence candidate and one runtime verification candidate
are retained per global criterion, plus two context refs per active task.
Only catalogued refs are exposed. Metadata contains type, task ID and relevant
criterion/status, never raw output or logs. The model sees `allowed_proofs`
indexed by the zero-based position in `global_success_criteria`. The immutable
snapshot contains the catalog and `proof_refs_by_criterion`, keyed by normalized
criterion, alongside the existing active task/evaluation IDs and fingerprint.

Two exact deterministic invariants are supported:

- `The graph reaches a terminal state.` → `structural:graph-terminal`
- `All active tasks are accepted.` → `structural:all-active-tasks-accepted`

These refs are created only after accepted-graph preconditions pass. Keyword
matches such as an arbitrary sentence containing “all”, “tasks” and “complete”
are insufficient. Failed checks, including those outside the display limit,
and unavailable required verification still take precedence. Missing proof
candidates produce `blocked` before a model call. A normal appended integration
task can supply the missing matching local criterion and evidence.

## Persistence and success boundary

Storage rechecks state, revision and fingerprint, reconstructs proof authority
from persisted plans, nodes and evaluations in the same write transaction,
compares the submitted snapshot, and validates every accepted criterion.
`finalize_accepted_integration` repeats this validation. Forging a submitted
catalog, modifying existing tasks, deleting tasks or changing plan-level
criteria fails closed. Existing task fields are compared before normalization.

Generic state updates cannot grant graph orchestration Success. Production
uses only `Integrating → finalize_accepted_integration → Success`.
The explicit injected `decide` compatibility path remains legacy: it has no
execution graph and uses `legacy_without_graph=True`. This flag is internal,
not an HTTP option, and is rejected for graph runs. The CLI does not inject
legacy decision hooks. Existing historical version-1 records remain readable;
their missing proof metadata cannot authorize a new finalization.

## Composition, limits and races

Final evidence renders criterion names and permitted refs, never free-form
global-verifier reasons. The presentation model only selects grounded claims.
Invalid output gets at most one repair, and a provider failure or exhausted
composition budget uses the deterministic fallback. Composition metrics are
included in the final-response event.

Verifier, repairs, integration replanning and composition share one model-call
budget. Global recovery shares the 4.5 plan-revision budget. Added tasks retain
normal selector, policy, durable approval, Runtime, Evaluator and 4.5 Recovery
handling. Cancellation, timeout and changed revision/fingerprint discard late
replanning or finalization without creating replacement work.

## Validation and limits

`tests/test_integration_grounding.py` covers empty/context/irrelevant proof,
prompt injection with actual fake-model calls, forged Storage snapshots,
immutable task fields, proof-backed recovery, an appended task retry through
4.5, denied capabilities, real spawned Runtime approval against a loopback fake
provider, shared budgets and late results. Existing integration tests remain.

Proof authority is based on trusted platform records, not an independent
mathematical verification of a program. Exact matching is deliberately
conservative: paraphrased or cross-task semantic criteria can require an
explicit integration task. Tests use fixtures and simulated model responses;
they do not establish behavior or quality of an actual Ollama model.
