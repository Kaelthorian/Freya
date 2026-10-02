# Worker forced finalization

The existing repeated-read, alternating-read, repeated no-op edit and repeated
incomplete cross-task request detectors stop the operational loop. Their
thresholds and byte-based mutation accounting are unchanged: replacing `hola`
with `Lkah` is progress; replacing `hola` with `hola` is not.

Previously `NoProgressDetected` fell through the generic exception handler and
failed the Task immediately. It now enters a dedicated terminal boundary.
Freya emits `worker.no_progress_detected` and
`worker.forced_finalization.started`, then makes exactly one LLM call if call,
token and execution-time budgets remain. No budget is extended. If a budget is
exhausted, the Task fails explicitly without making that call.

The call has `tools=[]`, no private thinking, and the following strict schema:

```json
{
  "decision": "COMPLETED",
  "summary": "No further operational action is needed.",
  "reason": "The Worker has ended this Task's execution.",
  "evidence_refs": [],
  "missing_capability": null
}
```

`decision` is exactly `COMPLETED` or `BLOCKED`. All five fields are required;
extra or duplicate fields, invalid types, blank reasons, unknown evidence IDs,
or native tool requests fail as `ForcedFinalizationInvalidOutput`. Summary and
reason are bounded to 2,000 characters, capability to 200, and the evidence
list to 100 supplied IDs. `COMPLETED` requires a null missing capability.
There are zero repair attempts and no tool/action loop after the response.

The terminal context contains the Task, criteria, actual action count, bounded
action/result tails, observed historical state, change count, changed paths,
no-progress trigger/cause, policy modes, blocked actions and previously recorded
verification evidence. Truncation and historical provenance are explicit.
It performs no new workspace read and creates no Final State Snapshot. Tool
results are untrusted evidence and never override the terminal prompt. All
context, responses and events pass existing sanitization.

`COMPLETED` means no further necessary operational action remains. It returns
Runtime `Success` / `execution_complete`, not semantic acceptance. Scheduler
persists `runtime_success` and releases dependents. After all Tasks in the same
Worker Assignment finish technically, the existing parent prepares Final State
and invokes Evaluator once. Criteria, classification, QA case handling and
semantic acceptance remain unchanged.

`BLOCKED` returns `Failed` with `ForcedFinalizationBlocked`, concrete `reason`
and optional `missing_capability`; it uses the existing Orchestrator/Recovery
failure path. The decision never grants or requests approval for a capability.
The validated decision is retained in structured result metadata and
`worker.forced_finalization.completed`; the event marks semantic acceptance as
pending Evaluator for `COMPLETED`, and not evaluated for `BLOCKED`.
Transport, time, token, cancellation and contract failures
emit `worker.forced_finalization.failed`. No verification actions follow either
outcome. Existing verification evidence is retained, without fabricating a
passed test or current file observation.

LLM tracing uses stage `forced_finalization`, prompt name
`worker_forced_finalization`, version `worker-finalization-v1`, and the existing
global debug setting. Tests cover deterministic model and backend fixtures;
`tests.test_python_execution` additionally runs real venv processes. A live
Ollama/Qwen converter run is a separate integration check.
