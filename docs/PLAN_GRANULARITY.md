# Logical Task granularity

A Task is a meaningful unit of progress, recovery and evaluation. It may
require several semantic operations, capabilities, tools and files. A Worker
is an execution resource: `single_worker` does not mean one Task, and grouping
Tasks under one Worker does not resolve mechanical Task fragmentation.

Semantic Plan schema version 5 tells Planner to apply the following test before
separating work: if this Task completed and no successor ever ran, would its
result still be meaningful progress? Create and populate a file together;
create and populate one component together; read, modify and save configuration
together. Parent directories are implicit in registered file creation, not a
new `mkdir` operation. Implementation and independent verification remain
separate Tasks. Verification cases retain their original IDs and stdin.

## Compiler normalization

`plan_granularity.py` operates on a copy of the new semantic proposal after
scope reconciliation and compatible QA-case grouping. The Compiler first
validates registered resources, the delegation decision and conflicting writers;
normalization cannot conceal unknown operations, duplicate creators or races.

The pass repeatedly combines a mechanical prerequisite A and its direct
successor B only when all these conditions hold:

- The plan uses `single_worker`; A has only B as a consumer, and B has only A
  as its direct prerequisite.
- A has only bare presence/readability or scaffold checks and no declared
  independently useful outcome. Pure `create_file` with bare-presence checks
  is mechanical regardless of objective wording or responsibility similarity.
  Other implementation objectives without scaffold indications are retained.
- Both are filesystem implementation/general Tasks. Neither declares an
  approval, policy, security, user-phase or independent recovery boundary.
- They concern the same exact artifact, an explicitly shared `logical_outcome`,
  or files within a declared mechanical component-directory prerequisite.
  Sharing a folder alone is insufficient. A metadata-free read prerequisite
  must explicitly name the successor's artifact.
- B performs registered writes. Independent QA, review, analysis, command
  execution, parallel branches, multi-consumer producers and runtime/frozen
  identity or policy metadata are preserved.

Optional Task metadata `granularity` contains bounded text only:
`logical_outcome`, `independent_value`, and `preserve_boundary`. These fields
describe intent and never grant capabilities. An explicit canonical request for
an intermediate empty artifact or separate phases conservatively preserves
boundaries throughout that proposal.

The survivor retains A's semantic key and prerequisites and B's final objective.
It unions ordered operations, semantic needs and exact write/owned paths;
dependents of B are redirected to the survivor. Directory scaffolding does not
become file ownership. Absorbed objectives/descriptions remain context, and the
original Planner proposal remains audit evidence. Principal local criteria
describe final content/structure, rather than an empty file's existence. If
only mechanical checks remain, a static implementation-content criterion is
used; this does not assert runtime correctness. Exact canonical/global presence
criteria are retained as auxiliary checks, preserving their proof links.

Only then does Compiler generate runtime task IDs, criterion IDs/links, write
owners, foreign-write targets and Worker assignments. Graph validation runs
before and after merging. Diagnostics map proposed positional `task-N` labels
to generated runtime IDs; those source labels are not persisted Runtime IDs.
Existing compiled plans, running graphs and Recovery references are never
renumbered by this pass. Runtime-plan schema remains version 4.

## Ranges, enforcement and observability

Normally `simple` uses 1–2 Tasks and `multi_step` 2–5; `complex` may use more.
Counts above these ranges emit a warning and trigger the same conservative
normalization. Planner must explain excess meaningful outcomes or preserved
boundaries in optional `granularity_reason`. After normalization a `simple` plan
above its range raises `OverfragmentedPlan` unless a concrete explanation is
grounded in preserved graph boundaries or independently meaningful outcomes.
`single_worker` never bypasses this Task gate. The `multi_step` range remains
advisory. Count alone never forces unsafe fusion. After every Compiler rejection,
Planner records the normalized proposal and cause in an orchestration-local
`RejectedSemanticPlanRegistry`. Each repair receives the compact rejected-plan
history and a verifiable `required_plan_delta` for the latest cause.
`SemanticPlanRepairGuard` checks the stable fingerprint and task/action graph
before another Compiler call. Generated IDs, field order, timestamps, metadata
and wording-only edits do not make an equivalent plan new. This catches
rotations such as rejected A → rejected B → A before another Compiler call. A
plan must also satisfy its cause-specific delta; a new fingerprint alone is
insufficient. For `OverfragmentedPlan`, the repair must reduce the task count to
the rejected plan's expected maximum or provide a concrete reason grounded in
independent outcomes or preserved boundaries. Dependency repairs must produce a
valid, acyclic graph. Every accepted material change still passes the normal
Compiler checks.

The loop allows one initial proposal and at most three Planner repairs, whether
the rejection came from the Compiler or the local guard. Exhaustion raises
`PlannerUnableToProduceAcceptablePlan` with a `failure_reason` distinguishing a
repeated plan, an unsatisfied required delta or a new Compiler rejection after
the repair limit. `PlannerUnableToProduceMateriallyDifferentPlan` remains a
compatibility base class, and `RepeatedSemanticPlanError` remains a secondary
exact-repeat defense.

`plan_evidence.verification_mode()` owns the full-match bare-file grammar shared
by normalization and Evaluator. Whitespace, final punctuation spacing and paired
Markdown inline-code delimiters around paths are normalized. Semantic predicates
remain intact: "The file `app.py` is created in the workspace." asserts final
presence; "`app.py` exists and contains correct logic." remains semantic.

The existing orchestration event pipeline publishes:

- `planner.granularity_summary`: before/after counts, complexity, strategy,
  merges, preserved boundaries, grouped operations and task-ID mapping.
- `plan_compiler.granularity_analyzed`: the Compiler normalization summary.
- `plan_compiler.tasks_merged`: `source_task_ids`, `result_task_id`, semantic
  keys, reason, operations and absorbed prerequisite criteria.
- `plan_compiler.granularity_warning`: stage, count, expected range, and any
  justification or missing-justification diagnostic.
- `plan_compiler.overfragmented`: rejection stage and cause before Runtime.
- `planner.plan_fingerprint_created`: normalized and structural fingerprints
  for each Planner response.
- `planner.rejected_plan_registered`: each Compiler, Planner-validation or
  local required-delta rejection added to the orchestration-local history, with
  its bounded normalized plan snapshot and cause.
- `planner.repair_delta_checked`: fingerprints, changed/unchanged fields,
  compiler error, required delta and guard result for each repair response.
- `planner.rejected_plan_repeated`: a proposal matching any earlier rejected
  fingerprint or equivalent task/action graph before Plan Compiler.
- `planner.repair_required_delta_failed`: a distinct proposal that still fails
  the latest cause-specific invariant before Plan Compiler.
- `planner.repair_no_material_change`: locally rejected equivalent plans and
  whether a full replan is required.
- `planner.repair_material_change_accepted`: a repair that satisfies its delta
  and is allowed to reach Plan Compiler.
- `planner.duplicate_plan_rejected`: secondary exact-plan defense; the primary
  guard rejects equivalent repairs before they consume Compiler attempts.

Compiler counts refer to the scope-reconciled proposal after QA-case grouping;
Planner's final count also reflects existing compatible postcompile transforms.

## Temperature converter example

Before:

1. Create `temperature_converter.py`; check existence.
2. Implement Celsius conversion, console input, two-decimal output and invalid
   input handling; depends on creation.
3. Verify inputs `0`, `100` and invalid input; depends on implementation.

After:

1. Implement `temperature_converter.py`, using `create_file` and `modify_file`
   with all derived resources and content/structure criteria.
2. Verify the same three independent cases; depends on Task 1.

Both Tasks belong to one Worker. Creation/implementation source labels map to
`task-1`; verification maps to `task-2`. A producer consumed by multiple branches,
an independently useful intermediate API, separate user phases, approval or
recovery checkpoints, distinct Workers and independent QA remain separate.

## Validation

`tests/test_plan_granularity.py` covers create/populate, empty scaffolds,
component-directory chains, read/modify/save, the converter, multiple operations,
advisory ranges, criterion links/ownership, boundaries and invalid graphs.
Related semantic-pipeline and Worker-assignment fixtures distinguish meaningful
checkpoints from bare scaffolding.

Run Python validation from the repository root, including
`python -m unittest tests.test_plan_granularity tests.test_semantic_pipeline
tests.test_plan_decomposition tests.test_worker_assignments -v`, then the full
suite, compilation and CLI checks. The injected Planner converter regression
does not validate live Ollama/Qwen behavior; that requires a separate real run.
