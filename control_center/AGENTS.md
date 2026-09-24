# Control Center subsystem

This directory owns the local web API, SQLite state and spawned execution
runtime. `frontend/` is its browser client and `tools.py` owns workspace-scoped
filesystem, command and Git implementations.

`task_spec.py` owns the production tool-free canonical intent contract,
clarification questions, sequential revisions and deterministic rendering. It
defines the Task Analyst response schema, canonical status/source enums, safe
pre-validation normalization and structured contract diagnostics. Runtime-only
history and revision metadata are not model-authored.
`task_analyst.py` retains the legacy version-3 compatibility contract.
`activity.py` builds the backend orchestration timeline and performance read
model from persisted timestamps/events; phase durations can overlap and must not
be summed into wall-clock totals. Keep actor identity independent of `agent_id`.
`runtime_resources.py` builds the planning catalog from the global capability
registry, global `Toolbox` schemas and enabled Store Skills. It resolves exact
IDs/aliases, derives tool-compatible capabilities and reports typed resource
mismatches; Skill instructions remain in worker snapshots. `plan_compiler.py`
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
through orchestration events. `orchestrator.py` runs the Task Analyst first when
an enabled agent has `config.orchestration_role=task_analyst` (with a legacy
name/role fallback), then persists that snapshot before it selects and
delegates to existing agents through `Runtime`; workers cannot create agents or
bypass tool policy.

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
- Keep `capabilities.py` as the tool-to-action registry and `policy.py` as the
  single decision point. Store effective policies in task snapshots.
- Planner `required_capabilities` are declarative requirements. They must use
  the capability registry but must never mutate or bypass agent policy.
- Rebuild the Runtime Resource Catalog for each planning request. Keep it
  derived from the global capability and Toolbox registries plus enabled Skills;
  context-provided tool IDs must not extend the global worker tool catalog.
  Do not duplicate IDs in Planner prompts.
  Validate capability, tool and Skill IDs, plus declared unique aliases,
  immediately after parse and again at the compiler/factory boundary. Unknown
  resource errors are fail-closed and do not trigger an LLM repair call.
- Keep `semantic_needs`, `required_capabilities`, `required_tools` and
  `preferred_skills` distinct. The resolver derives registered compatible
  capabilities when tools are supplied without capabilities; when both are
  supplied, reject incompatible pairs with `ToolCapabilityMismatch`. A tool
  selection never grants access: the Agent Factory derives worker schemas from
  the capability policy and every action still passes through `policy.py`.
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
- A program_creation request without a named language uses an explicit Python
  3.10+ assumption. A code_change follows the existing project stack. Preserve
  explicit languages and keep unrelated missing-input blockers fail-closed.
- Interactive plans append a dependent QA node with the registered
  `execution.python_script` capability. Ordinary interactive QA can select the
  `interactive-testing` Skill; the single-case console calculator omits its
  multi-case guidance. Keep Python execution on QA; implementation tasks receive
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
- `worker.py` must stop repeated successful read-only actions when no workspace
  progress is observed and bound consecutive denied/repeatedly blocked actions.
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
- Keep Skill validation, resolution, compatibility diagnostics and compact
  rendering in `skills.py`; Skills never execute tools or modify policy. Task
  records must retain immutable Skill snapshots and versions.
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
