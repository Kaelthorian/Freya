# Integration proof contract (4.6, version 3)

An accepted global integration requires every original global criterion exactly
once, satisfied, with nonempty evidence consisting only of proof references
permitted for that criterion. This also applies to a satisfied criterion inside
a non-accepted decision. A known reference alone is not proof.

## Catalog and deterministic association

Plan schema version 1 persists `criterion_links.global` entries with stable IDs and
`criterion_links.local` entries with stable IDs, a `task_id`, and explicit
`supports_global_criteria` IDs. Criterion text remains descriptive. The Planner
asks for these links; validation rejects duplicate IDs, unknown targets and
links that substitute a task criterion. Legacy plans without this field are
normalized with deterministic `gc-N` and `tc-<task>-N` IDs. Legacy local-to-global
links are inferred only for equal normalized text, preserving the old safety
boundary without guessing paraphrases.

`integration_proof.py` associates an accepted task's satisfied local evaluation
criterion with global criteria only through those persisted IDs. Evaluation
criterion text must still match its own declared task criterion. Nonempty local
evaluator evidence may then produce `evidence:*` proof refs. A passed runtime
check may produce `verification:*` proof refs only when it explicitly names the
local criterion through `supports_acceptance_criteria`, or when the task has a
single declared criterion. Failed or unavailable verification suppresses proof.
`task:*` and `evaluation:*` remain context refs, never proof. A model explanation
or check output cannot create or alter a link.

At most one evaluator evidence candidate and one runtime verification candidate
are retained per global criterion, plus two context refs per active task. The
catalog records task ID, local criterion ID and supported global criterion IDs,
without raw output or logs. The model sees `allowed_proofs` with both criterion
index and ID. The immutable snapshot includes the catalog, global IDs, and
criterion-specific permitted proof refs.

Two exact deterministic invariants remain supported:

- `The graph reaches a terminal state.` → `structural:graph-terminal`
- `All active tasks are accepted.` → `structural:all-active-tasks-accepted`

These refs arise only after the accepted-graph preconditions pass. Failed
checks and unavailable required verification take precedence. Missing proof
candidates produce `blocked` before a model call; accepted child tasks alone
never grant global acceptance. An appended integration task can close the gap
with new persisted local links.

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
legacy decision hooks. Existing historical version-1 and version-2 records remain readable;
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
mathematical verification of a program. Legacy exact matching is deliberately conservative: paraphrased criteria
require a validated explicit ID link or an integration task. Tests use fixtures and simulated model responses;
they do not establish behavior or quality of an actual Ollama model.
