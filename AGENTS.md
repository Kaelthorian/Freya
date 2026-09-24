# Agent Control Center repository instructions

This repository contains one product: a local Ollama agent platform. Its flow is
`frontend/` → `control_center/` HTTP API and SQLite scheduler → spawned worker →
Ollama and allowlisted tools → selected agent workspace.

## Boundaries

- Keep model-controlled reads and writes inside the task workspace. An agent may
  use an existing absolute folder or a generated per-task folder.
- Create and persist a validated orchestration plan before delegation. Planner
  capability declarations are requirements only and never grant access.
- Preserve the human prompt as audit evidence. Use the versioned, validated
  CanonicalTaskSpec as the sole downstream intent source; render worker/model
  context deterministically from it.
- For program_creation without a named language, record Python 3.10+ as an
  explicit assumption. For code_change, inspect and preserve the existing
  project language; use Python only if no stack exists for standalone work.
  Preserve explicit user choices and do not clear unrelated blockers.
- Treat `NEEDS_CLARIFICATION` as a hard planning gate: persist structured
  questions and answers on the same orchestration, create no plan or worker
  until the Task Spec reaches `READY_FOR_PLANNING`.
- Do not invent hidden prerequisite files for the canonical Task Spec. A task
  may consume a workspace artifact only when the user or an explicit producer
  task establishes it; a deterministic missing-file error must fail or replan,
  not retry unchanged with another agent.
- Interactive behavior must use a dependent QA task and bounded `run_command`
  stdin. Never wait on a worker terminal or add a free-form shell.
- Run the tool-free Task Analyst before production planning. It resolves user
  intent and asks material questions, but never chooses agents, capabilities,
  Skills, dependencies or runtime IDs. Planner chooses strategy; Plan Compiler
  assigns internal IDs and validates the DAG. Capability policy remains the
  sole action authority. A Skill never grants access.
- Keep orchestration state transitions conditional and terminal states final.
  Cancellation must serialize with delegation creation and include children
  waiting for approval.
- Keep tool dispatch allowlisted. Never add `shell=True` or a free-form shell
  tool. `run_command` accepts argv arrays and a small command set.
- Resolve every tool request to a concrete capability and pass it through the
  single fail-closed policy engine before invoking the underlying tool.
- Keep identity, behavior, autonomy, verification, and output as structured
  agent configuration. Autonomy records preferences only and never grants a
  capability.
- Keep reusable Skills declarative. `skills.py` validates and resolves them;
  required/recommended capabilities are diagnostics and never grants.
- Treat Git inspection as workspace-applicable only: non-Git task workspaces
  must not advertise `git_diff` or inject Git Inspection guidance.
- Stop workers that repeat successful read-only actions without workspace
  progress and persist a grounded no-progress cause instead of extending the
  step loop indefinitely.
- Keep Ollama inference loopback-only and preserve token and timing metrics.
- Keep the web server loopback-only and the worker/database ownership boundary.
- Preserve serialization per agent and per selected workspace.
- Preserve durable approvals and WaitingForApproval; never auto-grant a requested or new capability.
- Keep the execution pipeline ordered as Analyst → implementation → conditional
  QA → read-only audit. Failure analysis and replanning never grant permissions.
- Diagnose terminal task-graph failures from bounded, sanitized persisted logs only.
  The post-failure pass is tool-free, runs once, cites supplied log IDs, and must
  never re-execute work, grant capabilities, or reopen a terminal state.
- Treat Runtime `Success` as technical completion only; unlock dependencies and
  emit semantic task success only after the evaluator accepts required evidence.
- Keep Python support at 3.10+ and avoid runtime dependencies unless setup and
  documentation are updated.

## Validation

Run from the repository root in PowerShell:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q control_center
node --check frontend\app.js
node --check frontend\core.js
node --check frontend\components.js
node --check frontend\views.js
node --check frontend\dialogs.js
node --check frontend\icons.js
python -m control_center --help
```

See [docs/REPOSITORY_MAP.md](docs/REPOSITORY_MAP.md) for entry points,
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for runtime and safety boundaries,
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for commands, and
[docs/API.md](docs/API.md) for routes.

## Documentation maintenance

Keep the repository map and operational documents current when tools, module
responsibilities, safety boundaries, commands, or result formats change.
