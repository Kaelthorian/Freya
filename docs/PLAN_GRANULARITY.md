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
  independently useful outcome. An implementation objective with no scaffold
  indication is conservatively retained even if its criteria are weak.
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

## Advisory ranges and observability

Normally `simple` uses 1–2 Tasks and `multi_step` 2–5; `complex` may use more.
Counts above these ranges emit a warning and trigger the same conservative
normalization. Planner must explain excess meaningful outcomes or preserved
boundaries in optional `granularity_reason`. A missing explanation is reported
explicitly; count alone does not reject the plan or force unsafe fusion.

The existing orchestration event pipeline publishes:

- `planner.granularity_summary`: before/after counts, complexity, strategy,
  merges, preserved boundaries, grouped operations and task-ID mapping.
- `plan_compiler.granularity_analyzed`: the Compiler normalization summary.
- `plan_compiler.tasks_merged`: `source_task_ids`, `result_task_id`, semantic
  keys, reason, operations and absorbed prerequisite criteria.
- `plan_compiler.granularity_warning`: stage, count, expected range, and any
  justification or missing-justification diagnostic.

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
