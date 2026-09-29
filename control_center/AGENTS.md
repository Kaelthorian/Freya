# Control Center subsystem

This directory owns the local web API, SQLite state and spawned execution
runtime. `frontend/` is its browser client; `tools.py` owns workspace-scoped
filesystem operations and `sandbox.py` runs commands and Git in Docker copies.

`task_spec.py` owns the production tool-free canonical intent contract,
clarification questions, sequential revisions and deterministic rendering. It
defines the Task Analyst response schema, canonical status/source enums, safe
pre-validation normalization and structured contract diagnostics. Runtime-only
history and revision metadata are not model-authored. The clarification reducer
preserves pending IDs, allocates monotonic new IDs, records answered fields once,
and removes resolved, duplicate or optional model questions before persistence.
`storage.py` keeps answers keyed by spec version and treats exact replays as
idempotent. Three answered clarification rounds are the limit; an unresolved
material question then fails the run.
`task_analyst.py` retains the legacy version-3 compatibility contract.
`activity.py` builds the backend orchestration timeline and performance read
model from persisted timestamps/events; phase durations can overlap and must not
be summed into wall-clock totals. Keep actor identity independent of `agent_id`.
`runtime_resources.py` builds the planning catalog from the global capability
registry, global `Toolbox` schemas and `freya-core`. It resolves exact
IDs/aliases, derives tool-compatible capabilities and reports typed resource
mismatches; Skill instructions remain in worker snapshots. It replaces Planner
Skill preferences with `freya-core` and records an ignored-preference warning.
`plan_scope.py` compares semantic work with explicit/clarified Task Spec intent
before resource validation. It removes unsupported external-only tasks and
criteria, rewires dependencies, and rejects mixed or uncovered work.
`skills.py` defines the one active Skill and validates its tool IDs at startup.
`agent_factory.py` assigns it to every dynamic role without Skill capability
prerequisites. `plan_compiler.py`
derives missing resource links, assigns execution IDs and checks semantic
dependencies.
`planner.py` owns the versioned orchestration-plan contract, deterministic
criterion-ID normalization, `T-N` task-ID expansion with reference updates,
and DAG validation. It fills omitted global-link
rows from the plan's authoritative success criteria, assigns absent or
conflicting global/local criterion IDs before final validation, reconciles
unambiguous copied local criteria to task checks, and requires explicit local
coverage for model-declared executable global criteria. Invalid references or
ambiguous substitutions fail before delegation. An Analyst AC ID or description
used as a global-row placeholder is removed only when exact local text can
resolve its references to the plan's authoritative criteria. It reports ID correction counts
through orchestration events. `orchestrator.py` runs the built-in `TaskSpecAnalyst`
first and persists that snapshot before it selects and delegates to existing
agents through `Runtime`; persisted agents and presets do not select or configure
this system component. Its model, loopback endpoint, timeout and explicit offline
fallback have independent `--task-analyst-*` CLI settings. Workers cannot create
agents or bypass tool policy.

`integration.py` owns the canonical global status/action matrix, strict result
validation, one repair, and the narrow exact-check direct-proof decision.
`integration_orchestrator.py` persists bounded validation diagnostics; proof
authority remains in `integration_proof.py` and Storage revalidation. An
archival event reports archival `Success` separately from the run's final
`orchestration_status`.

## Boundaries

- Keep HTTP parsing/same-origin checks in `http.py` and business validation in
  `api.py`/`config.py`.
- Keep all durable state changes in `storage.py`; schema changes must preserve
  existing databases and update storage tests.
- The parent scheduler owns SQLite. Workers report sanitized events over their
  queue and must not write the database.
- Recheck tool permission and path policy inside `worker.py`; API validation is
  not the worker trust boundary.
- Keep `ToolResult.changed` tied to material workspace writes. An allowed
  `edit_file` with identical candidate bytes is successful and already
  satisfied, but must not write or register a workspace mutation.
- Keep `capabilities.py` as the tool-to-action registry and `policy.py` as the
  single decision point. Store effective policies in task snapshots.
- Keep a versioned Semantic Plan separate from the compiled runtime plan.
  Task Analyst supplies canonical intent. Planner supplies task kind, semantic
  operations, dependencies, outcomes, criteria and logical file owners only; it
  never selects capabilities, tools, Skills, or runtime IDs.
- Keep criterion evidence classification in `plan_evidence.py` and enforcement
  in `plan_compiler.py`. A local criterion must be provable by its compiled
  capabilities or reassigned to one compatible dependent verifier. Unordered
  tasks must not share an exact write target.
- Rebuild `RuntimeResourceCatalog` for each orchestration from the global
  capability and Toolbox registries. Keep one canonical semantic-operation to
  capability to tool mapping. `PlanCompiler` alone applies that mapping and
  emits the compiled plan. Recovery and Integration use its shared task
  compiler and remain inside their existing capability budgets.
- Treat Planner resource fields from legacy adapters as non-authoritative
  hints. The compiler derives every runtime requirement from semantic
  operations. Unknown operations, unknown resources and unsupported external
  actions fail closed without model repair. A tool registration never grants
  access; AgentFactory cannot add capabilities or tools, and every invocation
  still passes through `policy.py`.
- Preserve semantic and derived decisions in separate audit events. Keep exact
  `owned_paths`, reject duplicate normalized owners, and log the semantic
  operation to capability to tool mapping produced by the compiler.
- Worker completion is technical only. It reports execution completion or
  failure; only Evaluator acceptance unlocks dependencies and semantic success.
- Keep the Evaluator's durable evidence catalog separate from its compact model
  input. Put bounded diff, read-back and test content beside each unresolved
  criterion in the semantic input; preserve path, provenance and truncation
  markers. A linked content hash alone cannot prove complex file semantics.
- Compare model-proposed external work with explicit or clarified Task Spec
  intent before resolving tools. Do not infer deployment, publication, remote
  hosting or network side effects from web artifact creation. Preserve Task
  Spec validation criteria and fail closed if optional work cannot be removed
  without losing one.
- Use `storage.py` conditional transitions for orchestration state. Save the
  plan with `Planning → Planned` atomically, never reactivate a terminal run,
  and keep cancellation serialized with task submission.
- The production Planner calls loopback Ollama without tools. Deterministic
  fallback requires the explicit `--planner-offline` mode.
- Keep production `/api/chat` calls in `transport.py`: streamed reconstruction,
  per-component timeout/output profiles, refusal-only provider circuit and
  body-free telemetry. A CLI timeout is inactivity, while the hard ceiling
  remains bounded. Preserve injected request functions in adapter tests.
- Production Task Analyst output is a validated CanonicalTaskSpec. It receives
  the source prompt and prior spec, has no tools or workspace authority, and
  conservatively asks when a model fails on an ambiguous request. Planner and
  workers consume only a deterministic rendering of the ready Task Spec; the
  original remains immutable audit evidence.
- The Task Analyst must not infer implementation language or interface when the
  user has not specified them. Ask only when the missing choice materially changes
  the requested product; leave implementation choices to Planner. Preserve every
  explicit and clarified requirement and keep unrelated blockers fail-closed.
- Interactive plans append a dependent QA node with the registered
  `execution.python_script` capability. QA uses `freya-core`; keep Python
  execution on QA while implementation tasks receive
  only the file capabilities they need. The calculator runs one QA case with
  stdin lines `3` and `5`, produces integer output `8`, and creates no helper or
  test files. Its worker stops after one successful command supplies evidence
  for every configured acceptance criterion. More complex code changes may
  still add a read-only audit. The concrete tool is `run_command`; it accepts
  bounded stdin for Python only and closes stdin otherwise so `input()` cannot
  consume the wall-clock deadline.
- Keep context assembly in `agent_context.py`; do not add role-specific global
  prompts to the worker. Repeated non-recoverable tool failures must be
  bounded before consuming the task step budget.
- `worker.py` must stop repeated successful read-only actions and no-op edits
  on one artifact when no new evidence or workspace progress is observed, and
  bound consecutive denied/repeatedly blocked actions.
  It may complete a single-file task after exact matching read-back supports all
  file-presence criteria, or stop a single-case QA task after one controlled
  command supports every configured output and exit-status criterion.
  For structured output, attempt one bounded format repair for prose and
  malformed JSON; emit a sanitized task.result_contract diagnostic when the
  original response needs repair or fallback. Keep that format diagnostic
  separate from task limitations and semantic success.
  `tools.py` and `agent_context.py` must filter Git
  inspection when the task workspace is not inside a checkout.
- `storage.py`'s orchestration log query merges runtime and orchestration event
  timelines without exposing private model reasoning; preserve source labels
  and stable evidence IDs when extending it. Every persisted row must retain
  the normalized actor/workspace trace (`who`, `where`, `when`, `what`, `how`,
  `phase`, `trace_id`) so Task Analyst and every delegated agent are auditable.
- Keep `freya-core` validation and compact rendering in `skills.py`; its tool
  declarations never execute tools or modify policy. Task
  records must retain immutable Skill snapshots and versions.
- All agent Python, test, Ruff and Git commands go through `sandbox.py` on a
  disposable workspace copy. Docker failure is `SandboxUnavailable`; never
  fall back to host execution or copy command writes into the real workspace.
- Preserve spawned-process containment and descendant cleanup for cancel,
  restart, timeout and shutdown.
- Never persist secret values or private model thinking. `secret_env` contains
  a variable name only, and allowed names begin with `ACC_SECRET_`.
- The web server remains loopback-only unless authentication, CSRF, deployment
  and multiuser threat models are designed and documented together.
- Parent-owned approval requests are durable, sanitized and fail-closed; Autonomy and Skills never grant capabilities.

## Validation

From the repository root:

```powershell
python -m unittest tests.test_control_security tests.test_control_storage tests.test_control_runtime tests.test_control_api -v
python -m unittest tests.test_capabilities -v
python -m unittest tests.test_agent_context -v
python -m unittest tests.test_skills -v
python -m unittest tests.test_transport -v
python -m compileall -q control_center
node --check frontend\app.js
python -m control_center --help
```

Update `docs/REPOSITORY_MAP.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`
and `docs/API.md` when responsibilities, routes, state, limits or commands
change.
