"""Freya orchestration service: structured planning, delegation and integration."""
from __future__ import annotations

import json
import inspect
import threading
import time
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .agent_context import build_effective_agent, capability_summary
from .agent_factory import AgentFactory
from .agent_selector import AgentSelector
from .execution_graph import ExecutionGraph
from .integration import GlobalVerifier, IntegrationReplanner, ResultIntegrator
from .integration_orchestrator import IntegrationOrchestrationMixin
from .recovery import (FAILURE_ANALYSIS_VERSION, RECOVERY_VERSION,
                       FailureAnalyzer, RecoveryController, Replanner,
                       allowed_replan_scope, build_retry_prompt,
                       deterministic_failure_diagnosis,
                       semantic_failure_fingerprint)
from .security import sanitize
from .evaluator import EVALUATION_FIELDS, EVALUATOR_VERSION, Evaluator, technical_failure_evaluation
from .planner import MAX_PLAN_TASKS, PLAN_SCHEMA_VERSION, Planner
from .plan_compiler import planned_write_target_grants
from .runtime_resources import planner_resource_context
from .skills import skill_summary
from .storage import ORCHESTRATION_ACTIVE_STATUSES, ORCHESTRATION_TERMINAL_STATUSES, utcnow
from .task_spec import TaskSpecAnalyst, render_task_spec, validate_task_spec
from .cross_task import owned_path_key
from .cross_task import CrossTaskIntentMatcher
from .project_state import ProjectStateManager
from .plan_context import render_plan_context
from .worker_assignment import (
    occupied_workers, worker_assignment_map, worker_id_for_task,
)


ACTIVE_DELEGATED_TASK_STATUSES = {"Queued", "Running", "WaitingForApproval", "Paused"}


class Orchestrator(IntegrationOrchestrationMixin):
    def __init__(self, store, runtime, decide: Callable | None = None, config: dict | None = None,
                 planner: Planner | None = None, clock: Callable[[], float] | None = None,
                 wait: Callable[[float], None] | None = None,
                 selector: AgentSelector | None = None,
                 evaluator: Evaluator | None = None,
                 recovery: RecoveryController | None = None,
                 replanner: Replanner | None = None,
                 failure_analyzer: FailureAnalyzer | None = None,
                 task_analyst: TaskSpecAnalyst | None = None,
                 global_verifier: GlobalVerifier | None = None,
                 integration_replanner: IntegrationReplanner | None = None,
                 result_integrator: ResultIntegrator | None = None,
                 agent_factory: AgentFactory | None = None,
                 cross_task_intent_matcher: CrossTaskIntentMatcher | None = None):
        self.store, self.runtime = store, runtime
        self.decide = decide
        self.planner = planner or Planner()
        self.selector = selector or AgentSelector()
        # Compatibility fallback is evidence-only: it cannot accept Runtime
        # Success unless configured objective verification actually passed.
        self.evaluator = evaluator or Evaluator(offline=True)
        self.clock = clock or time.monotonic
        self.wait = wait or time.sleep
        self.recovery = recovery or RecoveryController(offline=True)
        self.replanner = replanner or Replanner()
        self.failure_analyzer = failure_analyzer or FailureAnalyzer(offline=True)
        self.task_analyst = task_analyst
        self.global_verifier = global_verifier or GlobalVerifier(offline=True)
        self.integration_replanner = integration_replanner or IntegrationReplanner()
        self.result_integrator = result_integrator or ResultIntegrator()
        self.agent_factory = agent_factory or AgentFactory(store)
        self.cross_task_intent_matcher = cross_task_intent_matcher or CrossTaskIntentMatcher()
        self.lock = threading.RLock()
        self._orchestration_workspaces: dict[str, str] = {}
        self.planner_lock = threading.Lock()
        self.evaluator_lock = threading.Lock()
        self.integration_lock = threading.Lock()
        self.config = {"max_rounds": 6, "max_delegated_tasks": MAX_PLAN_TASKS,
                       "max_parallel_tasks": 4, "max_model_calls": 12,
                       "max_wallclock_seconds": 900,
                       "max_semantic_attempts_per_task": 3,
                       "max_plan_revisions": 2, "max_recovery_actions": 8,
                       "max_recovery_model_calls": 16, "max_integration_rounds": 2,
                       "max_integration_model_calls": 12,
                       "project_context_max_manifest_files": 500,
                       "project_context_max_manifest_depth": 8,
                       "project_context_max_snapshot_artifacts": 500,
                       "project_context_max_prompt_chars": 12_000}
        self.recovery_lock = threading.Lock()
        self.failure_analysis_lock = threading.Lock()
        self.config.update(config or {})
        self.project_state = ProjectStateManager(
            store,
            max_manifest_files=self.config["project_context_max_manifest_files"],
            max_manifest_depth=self.config["project_context_max_manifest_depth"],
            max_snapshot_artifacts=self.config["project_context_max_snapshot_artifacts"],
            max_prompt_chars=self.config["project_context_max_prompt_chars"],
        )
        for field in ("max_delegated_tasks", "max_parallel_tasks", "max_semantic_attempts_per_task",
                      "max_plan_revisions", "max_recovery_actions",
                      "max_recovery_model_calls", "max_integration_rounds",
                      "max_integration_model_calls"):
            value = self.config[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field} must be a positive integer.")
        self.config["max_parallel_tasks"] = min(
            self.config["max_parallel_tasks"], self.config["max_delegated_tasks"],
        )

    def submit(self, prompt: str, workspace_path: str | None = None) -> dict:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Orchestration prompt must contain at least one character.")
        config = {**self.config, "workspace_path": workspace_path or ""}
        run = self.store.create_orchestration(prompt.strip(), config)
        threading.Thread(target=self._run, args=(run["id"],), daemon=True,
                         name="freya-orchestrator").start()
        return run

    def answer_clarification(self, oid: str, answers: dict[str, str]) -> dict:
        """Resume the same orchestration with answers to its pending questions."""
        with self.lock:
            run = self.store.record_clarification_answers(oid, answers)
            if run.pop("_clarification_replayed", False):
                return run
            self.store.add_orchestration_event(oid, {
                "event_type": "task_analysis.clarification_received",
                "status": "Analyzing", "spec_version": run["task_spec"]["version"],
                "answers": [{"question_id": key, "answer": value} for key, value in answers.items()],
                "message": "User answered the pending clarification questions.",
            })
        threading.Thread(target=self._run, args=(oid, dict(answers)), daemon=True,
                         name="freya-clarification").start()
        return run

    def revise_task_spec(self, oid: str, *, field: str, value: str,
                         user_message: str) -> dict:
        """Replan a user change only while no immutable plan or worker exists."""
        with self.lock:
            run = self.store.revise_unplanned_task_spec(
                oid, field=field, value=value, user_message=user_message)
            spec = run["task_spec"]
            self.store.add_orchestration_event(oid, {
                **self._task_analyst_identity(),
                "event_type": "task_analysis.updated", "status": "Queued",
                "spec_version": spec["version"], "task_spec": spec,
                "revision_change": spec["revision_changes"][-1],
                "message": "User revised the ready Task Spec before planning.",
            })
        threading.Thread(target=self._run, args=(oid,), daemon=True,
                         name="freya-spec-revision").start()
        return run

    def _task_spec_analyst(self) -> TaskSpecAnalyst:
        analyzer = self.task_analyst
        if analyzer is not None and hasattr(analyzer, "analyze_spec"):
            return analyzer
        return TaskSpecAnalyst()

    @staticmethod
    def _task_analyst_identity() -> dict:
        return {
            "agent_id": None,
            "actor_type": "task_analyst",
            "actor_name": "Task Analyst",
            "actor_role": "Task Analyst",
            "system_component": True,
        }

    def _analyze_task_spec(self, oid: str, run: dict,
                           answers: dict[str, str]) -> tuple[dict, dict]:
        analyzer = self._task_spec_analyst()
        previous = run.get("task_spec")
        identity = self._task_analyst_identity()
        self.store.add_orchestration_event(oid, {
            **identity,
            "event_type": "task_analysis.started", "status": "Analyzing",
            "analysis_version": 1, "spec_version": previous["version"] if previous else 0,
            "message": "The built-in Task Analyst is updating canonical user intent.",
        })
        try:
            spec = analyzer.analyze_spec(run["prompt"], previous=previous, answers=answers)
        finally:
            for diagnostic_event in getattr(analyzer, "diagnostic_events", []) or []:
                self.store.add_orchestration_event(oid, {
                    **identity, **diagnostic_event,
                })
        spec = validate_task_spec(spec)
        if spec["status"] == "ANALYZING":
            raise ValueError("Task Analyst did not resolve the analysis state.")
        metrics = dict(getattr(analyzer, "metrics", {}) or {})
        metrics.setdefault("component", "task_analyst")
        metrics.setdefault("system_component", True)
        metrics.setdefault("model_calls", 0)
        return spec, metrics

    def _workspace_for_run(self, run: dict) -> str | None:
        """Return the one workspace shared by every node in an orchestration."""
        config = run.get("config") or {}
        configured = str(config.get("workspace_path") or "").strip()
        if configured:
            return configured
        oid = str(run.get("id") or "")
        cached = self._orchestration_workspaces.get(oid)
        if cached:
            return cached
        data_dir = getattr(self.runtime, "data_dir", None)
        if data_dir is None:
            # Lightweight test runtimes and extension hooks may allocate their
            # own workspace when no production data directory is available.
            return None
        root = Path(data_dir) / "workspaces"
        root.mkdir(parents=True, exist_ok=True)
        workspace = root / uuid4().hex
        workspace.mkdir(parents=True, exist_ok=False)
        selected = str(workspace)
        setter = getattr(self.store, "set_orchestration_workspace", None)
        if callable(setter):
            persisted = setter(oid, selected)
            persisted_path = str(((persisted or {}).get("config") or {}).get("workspace_path") or "").strip()
            if persisted_path:
                selected = persisted_path
        self._orchestration_workspaces[oid] = selected
        return selected
    @staticmethod
    def _execution_prompt(operational_prompt: str, planned_task: dict,
                          attempt_prompt: str = "",
                          project_context: str = "",
                          responsibility_context: str = "",
                          worker_context: dict[str, Any] | None = None) -> str:
        """Send the Task Analyst's operational brief to every delegated worker."""
        spec_text = str(operational_prompt or "").strip()
        canonical = spec_text.startswith("{") and '"deliverables":' in spec_text
        heading = ("CANONICAL TASK SPEC (authoritative user intent):\n" if canonical else
                   "TASK ANALYST OPERATIONAL BRIEF (authoritative for execution):\n")
        sections = [
            heading + spec_text,
            "DELEGATED PLAN STEP:\n" + str(planned_task.get("objective") or "").strip(),
        ]
        description = str(planned_task.get("description") or "").strip()
        criteria = planned_task.get("success_criteria") or []
        if description:
            sections.append("Step context:\n" + description)
        if criteria:
            sections.append("Step success criteria:\n" + "\n".join("- " + str(item) for item in criteria))
        if responsibility_context:
            sections.append(responsibility_context)
        if attempt_prompt.strip():
            sections.append("RECOVERY INSTRUCTIONS:\n" + attempt_prompt.strip())
        if project_context.strip():
            sections.append(project_context.strip())
        if worker_context:
            rendered_worker_context = json.dumps(
                sanitize(worker_context), ensure_ascii=False, separators=(",", ":"),
            )
            if len(rendered_worker_context) > 12_000:
                rendered_worker_context = rendered_worker_context[:11_960] + "...[truncated]"
            sections.append(
                "ACTIVE WORKER TASK CONTEXT (history is factual context; the current task scope is authoritative):\n"
                + rendered_worker_context
            )
        sections.append("Complete this step without inventing requirements outside the " +
                        ("Canonical Task Spec." if canonical else "Task Analyst operational brief."))
        return "\n\n".join(section for section in sections if section.strip())

    def _worker_task_context(self, plan: dict[str, Any], task: dict[str, Any],
                             assignment: dict[str, Any] | None,
                             nodes: list[dict[str, Any]], agent: dict[str, Any],
                             *, active_tools: list[str] | None = None,
                             active_capabilities: list[str] | None = None
                             ) -> dict[str, Any] | None:
        if not assignment:
            return None
        task_id = str(task.get("id") or "")
        node_by_id = {item.get("plan_task_id"): item for item in nodes}
        plan_by_id = {item.get("id"): item for item in plan.get("tasks", [])
                      if isinstance(item, dict)}
        previous_completed = []
        relevant_artifacts = []
        for previous_id in assignment["task_ids"]:
            if previous_id == task_id:
                break
            previous_node = node_by_id.get(previous_id, {})
            if previous_node.get("state") != "success":
                continue
            previous_task = plan_by_id.get(previous_id, {})
            runtime_id = previous_node.get("runtime_task_id")
            runtime_task = self.store.get_task(runtime_id) if runtime_id else {}
            result = runtime_task.get("result") if isinstance(runtime_task, dict) else {}
            result = result if isinstance(result, dict) else {}
            summary = str(result.get("summary") or result.get("final") or "").strip()
            if not summary:
                summary = str(runtime_task.get("result") or "").strip()
            previous_completed.append({
                "task_id": previous_id,
                "objective": str(previous_task.get("objective") or "")[:500],
                "summary": sanitize(summary)[:1_000],
            })
            for item in result.get("artifacts", [])[:40] if isinstance(result.get("artifacts"), list) else []:
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    relevant_artifacts.append({
                        "path": item["path"][:500],
                        "change_type": str(item.get("change_type") or "")[:80],
                        "task_id": previous_id,
                    })
        predecessor_summaries = [item for item in previous_completed
                                 if item["task_id"] in (task.get("depends_on") or [])]
        config = agent.get("config", {}) if isinstance(agent.get("config"), dict) else {}
        effective_tools = (active_tools if active_tools is not None else
                           config.get("active_task_tools", []))
        effective_capabilities = (
            active_capabilities if active_capabilities is not None else
            config.get("active_task_capabilities", task.get("required_capabilities") or [])
        )
        return sanitize({
            "worker_id": assignment["worker_id"],
            "assigned_task_ids": list(assignment["task_ids"]),
            "current_task": {
                "task_id": task_id,
                "objective": str(task.get("objective") or "")[:1_000],
                "dependencies": list(task.get("depends_on") or []),
                "required_capabilities": list(task.get("required_capabilities") or []),
                "active_capabilities": list(effective_capabilities),
                "active_tools": list(effective_tools),
                "success_criteria": list(task.get("success_criteria") or []),
                "write_targets": list(task.get("write_targets") or []),
                "evidence_requirements": list(task.get("evidence_requirements") or []),
                "non_goals": list(task.get("non_goals") or []),
            },
            "previous_completed_tasks": previous_completed[-20:],
            "previous_completed_task_ids": [item["task_id"] for item in previous_completed],
            "relevant_artifacts": relevant_artifacts[-80:],
            "relevant_predecessor_summaries": predecessor_summaries[-20:],
            "conversation_context_note": (
                "Ollama calls do not have a server-side conversation session; summaries and verified "
                "artifact references from accepted tasks are supplied here."
            ),
        })

    @staticmethod
    def _responsibility_context(plan: dict[str, Any], task: dict[str, Any],
                                nodes: list[dict[str, Any]] | None = None) -> str:
        """Render a bounded snapshot of the complete effective plan."""
        return render_plan_context(plan, task, nodes)[0]

    def _plan_context_for_dispatch(self, orchestration_id: str,
                                   plan: dict[str, Any], task: dict[str, Any]) -> str:
        try:
            graph = self.store.get_execution_graph(orchestration_id)
            nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        except (KeyError, ValueError):
            nodes = []
        # An approved owner-scoped change and legacy delegation may be an
        # execution step outside the immutable compiled task list. Show it as
        # the current step in the read-only view without modifying the plan.
        visible_plan = plan
        if task.get("id") not in {item.get("id") for item in plan.get("tasks", [])
                                   if isinstance(item, dict)}:
            visible_plan = {**plan, "tasks": [*plan.get("tasks", []), task]}
        rendered, metadata = render_plan_context(visible_plan, task, nodes)
        self.store.add_orchestration_event(orchestration_id, {
            "event_type": "worker.plan_context_prepared", "status": "Success",
            "plan_task_id": task.get("id"), **metadata,
        })
        return rendered

    def _project_context_for_dispatch(self, orchestration_id: str,
                                     plan: dict[str, Any], task: dict[str, Any],
                                     run: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        workspace = self._workspace_for_run(run)
        if not workspace:
            return None, ""
        graph = None
        try:
            graph_snapshot = self.store.get_execution_graph(orchestration_id)
            graph = graph_snapshot.get("nodes", []) if isinstance(graph_snapshot, dict) else None
        except (KeyError, ValueError):
            graph = None
        snapshot, rendered = self.project_state.snapshot_for_dispatch(
            orchestration_id, plan, task, workspace, graph,
        )
        return {"project_state_snapshot": snapshot}, rendered

    def _submit_runtime_task(self, agent_id: str, prompt: str, workspace: str | None,
                             runtime_context: dict[str, Any] | None = None) -> dict[str, Any]:
        submit = self.runtime.submit
        if runtime_context is None:
            return submit(agent_id, prompt, workspace)
        try:
            parameters = inspect.signature(submit).parameters.values()
            accepts_context = any(
                item.name == "runtime_context" or item.kind == inspect.Parameter.VAR_KEYWORD
                for item in parameters
            )
        except (TypeError, ValueError):
            accepts_context = False
        if accepts_context:
            return submit(agent_id, prompt, workspace, runtime_context=runtime_context)
        # Small injected runtimes used by extensions and tests may implement
        # the historical three-argument protocol.
        return submit(agent_id, prompt, workspace)

    def _snapshot_delegation(self, delegation_id: str, task: dict) -> None:
        result = {
            "result": task.get("result"),
            "error": task.get("error") or "",
            "verification": task.get("verification"),
        }
        self.store.update_delegation(
            delegation_id, status=task["status"], result=result,
            finished_at=task.get("finished_at"),
        )

    @staticmethod
    def _recovery_workspace_state(task: dict | None) -> dict[str, Any]:
        """Build bounded, auditable state for a subsequent recovery attempt."""
        if not isinstance(task, dict):
            return {}
        result = task.get("result")
        result_dict = result if isinstance(result, dict) else {}
        return sanitize({
            "workspace": task.get("workspace") or "",
            "status": task.get("status") or "",
            "verification": task.get("verification") or {},
            "workspace_diffs": result_dict.get("workspace_diffs") or [],
            "artifacts": result_dict.get("artifacts") or [],
            "actions": result_dict.get("actions") or [],
            "error": task.get("error") or "",
        })

    @staticmethod
    def _selection_failure_message(selection: dict, agents: list[dict]) -> str:
        """Turn the persisted selector snapshot into a concise log-visible cause."""
        names = {str(agent.get("id")): str(agent.get("name") or agent.get("id"))
                 for agent in agents if isinstance(agent, dict) and agent.get("id")}
        details: list[str] = []
        for candidate in selection.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            label = names.get(str(candidate.get("agent_id")), str(candidate.get("agent_id") or "Agent"))
            warnings = [str(item).strip() for item in candidate.get("warnings", [])
                        if str(item).strip()]
            if warnings:
                details.append(label + ": " + "; ".join(warnings[:4]))
        if not details:
            details = [str(item).strip() for item in selection.get("warnings", [])
                       if str(item).strip()]
        base = "No eligible or conditional agent is available for the planned task."
        return sanitize(base + ((" Cause: " + " | ".join(details[:5])) if details else ""))[:4000]

    @staticmethod
    def _compact_failure_log(source: str, event: dict, *, task_id: str = "") -> dict:
        stored_payload = event.get("payload_json") if isinstance(event, dict) else None
        if isinstance(stored_payload, str) and stored_payload.strip():
            try:
                decoded_payload = json.loads(stored_payload)
            except (TypeError, ValueError):
                decoded_payload = {}
            if isinstance(decoded_payload, dict):
                decoded_payload.update({key: value for key, value in event.items()
                                        if key != "payload_json"})
                event = decoded_payload
        event_id = event.get("id")
        log_id = (f"orchestration:{event_id}" if source == "orchestration" else
                  f"task:{task_id}:{event_id}")
        compact = {"log_id": log_id, "source": source}
        for key in ("timestamp", "event_type", "level", "status", "task_id", "agent_id",
                    "runtime_task_id", "tool", "capability", "requested_capability",
                    "policy_decision", "policy_reason", "error_class", "step_id",
                    "step_number", "attempt", "message", "reason", "error", "resource"):
            value = event.get(key)
            if value not in (None, "", [], {}):
                compact[key] = str(value)[:2000]
        for key in ("output", "input"):
            value = event.get(key)
            if value not in (None, "", [], {}):
                bounded = sanitize(value)
                if isinstance(bounded, (dict, list)):
                    bounded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"), default=str)
                compact[key] = str(bounded)[:4000]
        return sanitize(compact)

    def _failure_logs(self, oid: str, graph: ExecutionGraph) -> list[dict]:
        """Return a bounded log-only diagnostic context for a failed graph."""
        run = self.store.get_orchestration(oid)
        logs = [self._compact_failure_log("orchestration", event)
                for event in run.get("events", [])]
        for node in graph.serialize():
            runtime_task_id = str(node.get("runtime_task_id") or "")
            if not runtime_task_id:
                continue
            for event in self.store.list_events(task_id=runtime_task_id, limit=200):
                logs.append(self._compact_failure_log(
                    "task", event, task_id=runtime_task_id,
                ))
        logs.sort(key=lambda item: (str(item.get("timestamp") or ""), str(item["log_id"])))
        return logs[-250:]

    def _analyze_graph_failure(self, oid: str, graph: ExecutionGraph) -> tuple[dict, dict, str]:
        """Run one model review from persisted logs, then fail over deterministically."""
        logs = self._failure_logs(oid, graph)
        mode = "model"
        try:
            with self.failure_analysis_lock:
                diagnosis = self.failure_analyzer.analyze(logs)
            metrics = dict(self.failure_analyzer.metrics)
            if self.failure_analyzer.deterministic:
                mode = "deterministic"
        except Exception as exc:
            diagnosis = deterministic_failure_diagnosis(logs)
            metrics = dict(getattr(self.failure_analyzer, "metrics", {}) or {})
            metrics.setdefault("model_calls", 1)
            metrics["fallback_error"] = sanitize(str(exc))[:1000]
            mode = "deterministic_fallback"
        return diagnosis, metrics, mode

    def cancel(self, oid: str) -> dict:
        """Idempotently cancel a run and every active child under one process lock."""
        with self.lock:
            run = self.store.get_orchestration(oid)
            if run["status"] in ORCHESTRATION_TERMINAL_STATUSES:
                return run
            cancelled = self.store.transition_orchestration(
                oid, ORCHESTRATION_ACTIVE_STATUSES, "Cancelled", error="Cancelled by user.",
            )
            if cancelled is None:
                return self.store.get_orchestration(oid)
            self.store.cancel_cross_task_modification_requests(oid, "Cancelled by user.")
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.cancelled", "status": "Cancelled",
                "message": "Freya orchestration cancelled by user.",
            })
            for delegation in cancelled.get("delegations", []):
                task_id = delegation.get("task_id")
                if task_id and delegation.get("status") in ACTIVE_DELEGATED_TASK_STATUSES:
                    try:
                        task = self.runtime.cancel(task_id)
                        self._snapshot_delegation(delegation["id"], task)
                    except (KeyError, ValueError):
                        pass
            if cancelled.get("plan"):
                persisted = self.store.get_execution_graph(oid)
                if persisted["nodes"]:
                    graph = ExecutionGraph(cancelled.get("effective_plan") or cancelled["plan"], persisted["nodes"])
                    graph.cancel_nonterminal(utcnow(), "Cancelled by user.")
                    self.store.save_execution_graph(oid, graph.serialize())
                    self._close_terminal_attempts(oid, graph)
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.graph.completed", "status": "Cancelled",
                        "message": "The execution graph was cancelled by the user.",
                        "summary": graph.summary(),
                    })
            self._archive_dynamic_agents(oid)
            return self.store.get_orchestration(oid)

    def resolve_cross_task_approval(self, approval_id: str, resolution: str) -> dict[str, Any]:
        """Resolve the existing durable approval row for a cross-task request."""
        with self.lock:
            request = self.store.get_cross_task_modification_request_by_approval(approval_id)
            if request is None:
                raise KeyError(approval_id)
            run = self.store.get_orchestration(request["orchestration_id"])
            if run["status"] not in ORCHESTRATION_ACTIVE_STATUSES:
                raise ValueError("Cannot resolve a cross-task request after its orchestration ended.")
            if resolution != "denied":
                plan = run.get("effective_plan") or run.get("plan") or {}
                owner = next((item for item in plan.get("tasks", [])
                              if item.get("id") == request.get("target_owner_plan_task_id")), None)
                if owner is None:
                    raise ValueError("The owning task is not present in the current plan.")
                self._cross_task_change_task(plan, owner, request)
                if self._cross_task_cycle_would_form(plan, request):
                    raise ValueError("The cross-task request would create a dependency cycle.")
            resolved = self.store.resolve_cross_task_modification_approval(approval_id, resolution)
            request = resolved["request"]
            event_type = {
                "approved_once": "cross_task_modification.approved_once",
                "approved_file_intent": "cross_task_modification.approved_intent",
                "denied": "cross_task_modification.denied",
            }[resolution]
            self._cross_task_event(
                request["orchestration_id"], event_type, request,
                status=request["status"], approval_source=request["approval_source"],
                grant_id=request.get("grant_id"), human_resolution=resolution,
                message="The operator resolved the cross-task modification request.",
            )
            return resolved

    @staticmethod
    def _agent_candidates(agents):
        candidates = []
        for agent in agents:
            try:
                effective = build_effective_agent(agent)
                identity = effective["identity"]
                candidates.append({
                    "id": agent["id"], "name": identity["name"], "role": identity["role"],
                    "purpose": identity["purpose"], "description": identity["description"],
                    "skills": [skill_summary(item) for item in effective["skills"] if isinstance(item, dict)],
                    "capabilities_summary": capability_summary(effective["capability_policy"]),
                    "availability": agent.get("status", "Offline"), "enabled": bool(agent.get("enabled")),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return candidates

    def _planning_context(self, agents=None) -> dict:
        # Include semantic summaries only. Full Skill instructions and
        # procedures, policy, task history, logs, and model transcripts stay out.
        skills = self.store.list_skills(enabled=True) if self.store is not None else None
        return planner_resource_context(skills)

    def _record_planning_resources(self, oid: str, context: dict) -> dict:
        summary = {
            "resource_catalog_version": context.get("resource_catalog_version", ""),
            "available_capability_ids": sorted(
                item["id"] for item in context.get("capabilities", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)),
            "available_tool_ids": sorted(
                item["id"] for item in context.get("tools", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)),
            "available_skill_ids": sorted(
                item["id"] for item in context.get("skills", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)),
        }
        self.store.add_orchestration_event(oid, {
            "event_type": "freya.planning.resources", "status": "Planning",
            **summary,
            "message": "Freya supplied the current runtime resource catalog to the Planner.",
        })
        return summary

    def _decision(self, prompt, agents, results):
        candidates = self._agent_candidates(agents)
        if self.decide:
            return self.decide(prompt, candidates, results)
        task = (prompt if isinstance(prompt, dict) else {
            "id": "compatibility-task", "objective": str(prompt), "description": str(prompt),
            "required_capabilities": [], "preferred_skills": [],
        })
        selection = self.selector.select_agent(task, agents)
        if selection["selected_agent_id"] is None:
            return {"action": "respond", "message": "No eligible agent is available for this request.",
                    "selection": selection}
        return {"action": "delegate", "tasks": [{
            "agent_id": selection["selected_agent_id"],
            "objective": task["objective"], "selection": selection,
        }]}

    def _selection_context(self, run: dict) -> dict:
        workloads: dict[str, int] = {}
        active_runtime_task_ids: set[str] = set()
        suspended_cross_task_ids = {
            item.get("requester_runtime_task_id")
            for item in self.store.list_cross_task_modification_requests(run.get("id", ""))
        } if run.get("id") else set()
        for task in self.store.list_tasks(limit=10000):
            if task.get("status") in ACTIVE_DELEGATED_TASK_STATUSES:
                if task.get("id") in suspended_cross_task_ids:
                    continue
                agent_id = task.get("agent_id")
                if isinstance(agent_id, str):
                    workloads[agent_id] = workloads.get(agent_id, 0) + 1
                task_id = task.get("id")
                if isinstance(task_id, str):
                    active_runtime_task_ids.add(task_id)
        # A selected ready node is a scheduling reservation. Active graph nodes
        # are also included when their Runtime task is not yet visible, while
        # Runtime task IDs prevent double-counting the normal persisted path.
        orchestration_id = run.get("id")
        if isinstance(orchestration_id, str):
            for node in self.store.get_execution_graph(orchestration_id)["nodes"]:
                agent_id = node.get("selected_agent_id")
                if not isinstance(agent_id, str):
                    continue
                reserved = (node.get("state") == "ready" and node.get("selection_id"))
                missing_runtime = (
                    node.get("state") in {"running", "evaluating"}
                    and node.get("runtime_task_id") not in active_runtime_task_ids
                )
                if reserved or missing_runtime:
                    workloads[agent_id] = workloads.get(agent_id, 0) + 1
        return {
            "workspace_path": run.get("config", {}).get("workspace_path") or "",
            "workloads": workloads,
        }

    def _create_dynamic_agent(self, oid: str, task: dict, attempt: int,
                              *, variant: int = 0,
                              worker_assignment: dict[str, Any] | None = None,
                              worker_creation_reason: str = "assignment_started") -> dict:
        planned_task_id = str(task.get("id") or "")
        self.store.add_orchestration_event(oid, {
            "event_type": "freya.agent_factory.started", "status": "Running",
            "task_id": planned_task_id, "attempt": attempt,
            "worker_id": worker_assignment.get("worker_id") if worker_assignment else None,
            "execution_strategy": self._execution_strategy(oid),
            "required_capabilities": list(task.get("required_capabilities", [])),
            "message": (
                "Freya is creating one stable Worker for its compiled assignment."
                if worker_assignment else
                "Freya is building a least-privilege agent for the planned task."
            ),
        })
        try:
            arguments = {
                "orchestration_id": oid, "attempt": attempt, "variant": variant,
            }
            if worker_assignment is not None:
                arguments["worker_assignment"] = worker_assignment
            created = self.agent_factory.create(task, **arguments)
        except Exception as exc:
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.agent_factory.failed", "status": "Failed",
                "task_id": planned_task_id, "attempt": attempt,
                "message": "Dynamic agent creation failed: " + str(exc),
            })
            raise
        agent = created["agent"]
        for omission in created.get("skill_omissions", []):
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.agent_factory.skill_omitted", "status": "Warning",
                "task_id": planned_task_id, "agent_id": agent["id"],
                "skill_id": omission["skill_id"], "reason": omission["reason"],
                "missing_capabilities": omission["missing_capabilities"],
                "message": "A preferred Skill was omitted because its required capabilities are unavailable.",
            })
        event = {
            "event_type": "freya.agent_created", "status": "Running",
            "task_id": planned_task_id, "plan_task_id": planned_task_id,
            "agent_id": agent["id"], "role": created["role"],
            "skill_ids": created["skill_ids"],
            "planner_preferred_skills": list(task.get("preferred_skills", [])),
            "resolved_skills": created["skill_ids"],
            "required_capabilities": created["required_capabilities"],
            "active_capabilities": created.get(
                "active_capabilities", created["required_capabilities"],
            ),
            "effective_tools": created["effective_tools"],
            "worker_id": worker_assignment.get("worker_id") if worker_assignment else None,
            "worker_generation": worker_assignment.get("generation") if worker_assignment else None,
            "attempt": attempt, "factory_version": created["factory_version"],
            "warnings": created["warnings"],
            "message": (
                "Freya created the stable Worker identity assigned to this compiled task group."
                if worker_assignment else
                "Freya created a task-specific ephemeral agent."
            ),
        }
        self.store.add_orchestration_event(oid, event)
        self.store.add_orchestration_event(oid, {
            **event,
            "event_type": "freya.agent_policy.validated",
            "message": "The dynamic agent policy and derived tool surface were validated.",
        })
        if worker_assignment is not None:
            worker_event = {
                "event_type": "worker.created", "status": "Running",
                "worker_id": worker_assignment["worker_id"],
                "agent_id": agent["id"], "task_id": planned_task_id,
                "orchestration_id": oid,
                "execution_strategy": self._execution_strategy(oid),
                "active_tools": list(created["effective_tools"]),
                "active_capabilities": list(created.get(
                    "active_capabilities", created["required_capabilities"],
                )),
                "worker_generation": worker_assignment.get("generation", 1),
                "worker_creation_reason": worker_creation_reason,
                "message": "Created one logical Worker for its compiled assignment.",
            }
            self.store.add_orchestration_event(oid, worker_event)
            if worker_creation_reason.startswith("explicit_recovery_"):
                self.store.add_orchestration_event(oid, {
                    **worker_event,
                    "event_type": "worker.recreated",
                    "message": "Recovery explicitly recreated the assigned Worker identity.",
                })
        return created

    def _execution_strategy(self, oid: str) -> str | None:
        try:
            run = self.store.get_orchestration(oid)
            plan = run.get("effective_plan") or run.get("plan") or {}
            return plan.get("execution_strategy")
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _assignment_matches(agent: dict[str, Any], oid: str,
                            assignment: dict[str, Any]) -> bool:
        config = agent.get("config") if isinstance(agent.get("config"), dict) else {}
        provenance = config.get("provenance") if isinstance(config.get("provenance"), dict) else {}
        stored = config.get("worker_assignment") if isinstance(config.get("worker_assignment"), dict) else {}
        stored_task_ids = list(stored.get("task_ids") or [])
        current_task_ids = list(assignment.get("task_ids") or [])
        return (
            provenance.get("generated_by_freya") is True
            and provenance.get("orchestration_id") == oid
            and stored.get("worker_id") == assignment.get("worker_id")
            and bool(stored_task_ids)
            and current_task_ids[:len(stored_task_ids)] == stored_task_ids
        )

    def _worker_agent_for_assignment(self, oid: str, assignment: dict[str, Any]
                                     ) -> tuple[dict[str, Any] | None, str | None]:
        agents = [agent for agent in self.store.list_agents()
                  if self._assignment_matches(agent, oid, assignment)]
        if not agents:
            return None, None
        agents.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
        attempts = [item for item in self.store.list_execution_attempts(oid)
                    if item.get("plan_task_id") in assignment["task_ids"]]
        attempts.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
        latest = attempts[-1] if attempts else None
        if latest:
            matching = next((agent for agent in agents
                             if agent.get("id") == latest.get("selected_agent_id")), None)
            if matching is not None:
                return matching, str(latest.get("plan_task_id") or "")
        latest_agent = agents[-1]
        provenance = latest_agent.get("config", {}).get("provenance", {})
        return latest_agent, str(provenance.get("plan_task_id") or "")

    @staticmethod
    def _event_payloads(run: dict[str, Any]) -> list[dict[str, Any]]:
        payloads = []
        for event in run.get("events", []):
            raw = event.get("payload_json") if isinstance(event, dict) else None
            if not isinstance(raw, str):
                continue
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
        return payloads

    def _worker_generation(self, oid: str, assignment: dict[str, Any]) -> int:
        generations = [
            int(agent.get("config", {}).get("worker_assignment", {}).get("generation", 0))
            for agent in self.store.list_agents()
            if self._assignment_matches(agent, oid, assignment)
            and isinstance(agent.get("config", {}).get("worker_assignment", {}).get(
                "generation", 0), int)
        ]
        run = self.store.get_orchestration(oid)
        generations.extend(
            int(event.get("worker_generation", 0))
            for event in self._event_payloads(run)
            if event.get("event_type") in {"worker.created", "worker.recreated"}
            and event.get("worker_id") == assignment.get("worker_id")
            and isinstance(event.get("worker_generation"), int)
        )
        return max(generations, default=0) + 1

    @staticmethod
    def _factory_task_scope(task: dict[str, Any], plan: dict[str, Any],
                            recovery_workspace_state: dict[str, Any] | None = None,
                            recovery_reason: str = "", recovery_failure_class: str = "",
                            recovery_cause: str = "") -> dict[str, Any]:
        scoped = dict(task)
        task_id = str(task.get("id") or "")
        scoped["_planned_write_targets"] = planned_write_target_grants(plan, task_id)
        scoped["_write_owners"] = dict(plan.get("write_owners") or {
            owned_path_key(path): owner["id"]
            for owner in plan.get("tasks", []) if isinstance(owner, dict)
            for path in owner.get("owned_paths", [])
        })
        if recovery_workspace_state:
            scoped["_recovery_workspace_state"] = recovery_workspace_state
        if recovery_reason:
            scoped["_recovery_reason"] = recovery_reason
        if recovery_failure_class:
            scoped["_recovery_failure_class"] = recovery_failure_class
        if recovery_cause:
            scoped["_recovery_cause"] = recovery_cause
        return scoped

    def _archive_dynamic_agents(self, oid: str) -> list[dict]:
        archiver = getattr(self.store, "archive_dynamic_agents", None)
        if not callable(archiver):
            return []
        try:
            run = self.store.get_orchestration(oid)
            plan = run.get("effective_plan") or run.get("plan") or {}
            strategy = plan.get("execution_strategy")
            agents = [agent for agent in self.store.list_agents()
                      if self._assignment_matches(agent, oid,
                          agent.get("config", {}).get("worker_assignment", {}))]
            grouped: dict[str, list[dict[str, Any]]] = {}
            for agent in agents:
                assignment = agent.get("config", {}).get("worker_assignment", {})
                worker_id = assignment.get("worker_id")
                if isinstance(worker_id, str) and worker_id:
                    grouped.setdefault(worker_id, []).append(agent)
            existing_completed = {
                str(event.get("worker_id")) for event in self._event_payloads(run)
                if event.get("event_type") == "worker.completed"
            }
            attempts = self.store.list_execution_attempts(oid)
            for worker_id, candidates in grouped.items():
                if worker_id in existing_completed:
                    continue
                candidates.sort(key=lambda item: (str(item.get("created_at") or ""),
                                                  str(item.get("id") or "")))
                agent = candidates[-1]
                assignment = agent.get("config", {}).get("worker_assignment", {})
                task_ids = list(assignment.get("task_ids") or [])
                worker_attempts = [item for item in attempts
                                   if item.get("plan_task_id") in task_ids]
                worker_attempts.sort(key=lambda item: (str(item.get("created_at") or ""),
                                                       str(item.get("id") or "")))
                current_task_id = (str(worker_attempts[-1].get("plan_task_id") or "")
                                   if worker_attempts else "")
                config = agent.get("config", {})
                self.store.add_orchestration_event(oid, {
                    "event_type": "worker.completed",
                    "status": "Success" if run.get("status") == "Success" else run.get("status"),
                    "worker_id": worker_id, "agent_id": agent["id"],
                    "task_id": current_task_id or config.get("provenance", {}).get("plan_task_id"),
                    "orchestration_id": oid, "execution_strategy": strategy,
                    "active_tools": list(config.get("active_task_tools") or agent.get("tools") or []),
                    "active_capabilities": list(config.get("active_task_capabilities") or []),
                    "assigned_task_ids": task_ids,
                    "message": "All orchestration work is terminal; the logical Worker is being destroyed.",
                })
        except Exception as exc:
            self.store.add_orchestration_event(oid, {
                "event_type": "worker.completed", "status": "Warning",
                "orchestration_id": oid,
                "message": "Worker completion telemetry was incomplete: " + str(exc),
            })
        try:
            archived = archiver(oid)
        except Exception as exc:
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.agent_factory.failed",
                "status": self.store.get_orchestration(oid)["status"],
                "message": "Ephemeral agent archival failed: " + str(exc),
            })
            return []
        status = self.store.get_orchestration(oid)["status"]
        for agent in archived:
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.dynamic_agent.archived", "status": "Success",
                "orchestration_status": status,
                "agent_id": agent["agent_id"],
                "worker_id": agent.get("worker_id"),
                "worker_generation": agent.get("worker_generation"),
                "task_id": agent.get("plan_task_id"),
                "plan_task_id": agent.get("plan_task_id"),
                "attempt": agent.get("attempt"),
                "factory_version": agent.get("factory_version"),
                "message": "Freya archived an ephemeral agent after orchestration completion.",
            })
        return archived

    def _analyze_prompt(self, oid: str, prompt: str) -> tuple[dict | None, dict]:
        """Legacy planner adapter over the built-in canonical Task Analyst."""
        identity = self._task_analyst_identity()
        with self.lock:
            if self.store.get_orchestration(oid)["status"] != "Planning":
                return None, {"mode": "cancelled", "model_calls": 0,
                              "component": "task_analyst", "system_component": True}
            self.store.add_orchestration_event(oid, {
                **identity,
                "event_type": "freya.task_analysis.started", "status": "Planning",
                "message": "The built-in Task Analyst is structuring the human request.",
            })
        analyzer = self._task_spec_analyst()
        try:
            spec = validate_task_spec(analyzer.analyze_spec(prompt))
            ready = spec["status"] == "READY_FOR_PLANNING"
            analysis = {
                "analysis_version": 1,
                "task_spec": spec,
                "objective": spec["objective"],
                "requirements": spec["requirements"],
                "assumptions": spec["assumptions"],
                "operational_prompt": render_task_spec(spec) if ready else "",
                "ready_for_execution": ready,
                "blocking_reason": None if ready else (
                    "Task Spec requires clarification: " + "; ".join(
                        item["question"] for item in spec["clarification_questions"]
                    )
                ),
            }
            metrics = dict(getattr(analyzer, "metrics", {}) or {})
            metrics.setdefault("component", "task_analyst")
            metrics.setdefault("system_component", True)
            metrics.setdefault("model_calls", 0)
        except Exception as exc:
            with self.lock:
                if self.store.get_orchestration(oid)["status"] == "Planning":
                    self.store.add_orchestration_event(oid, {
                        **identity,
                        "event_type": "freya.task_analysis.failed", "status": "Failed",
                        "message": "Built-in Task Analyst failed: " + sanitize(str(exc))[:1000],
                    })
            raise
        finally:
            for diagnostic_event in getattr(analyzer, "diagnostic_events", []) or []:
                self.store.add_orchestration_event(oid, {
                    **identity, **diagnostic_event,
                })
        with self.lock:
            if self.store.get_orchestration(oid)["status"] == "Planning":
                self.store.add_orchestration_event(oid, {
                    **identity,
                    "event_type": "freya.task_analysis.completed", "status": "Planning",
                    "analysis_version": analysis["analysis_version"],
                    "analysis_mode": metrics.get("mode", "llm"),
                    "metrics": metrics,
                    "task_analysis": analysis,
                    "message": "The built-in Task Analyst produced the legacy planning summary.",
                })
        return analysis, metrics

    def _require_ready_analysis(self, oid: str, analysis: dict | None) -> dict:
        """Fail closed when the Task Analyst requires user input before planning."""
        if not isinstance(analysis, dict):
            raise ValueError("Task Analyst did not produce a structured analysis.")
        if analysis.get("ready_for_execution") is True:
            return analysis
        reason = str(analysis.get("blocking_reason") or "The Task Analyst did not authorize execution.").strip()
        message = "Task Analyst blocked execution: " + reason
        with self.lock:
            if self.store.get_orchestration(oid)["status"] == "Planning":
                self.store.add_orchestration_event(oid, {
                    **self._task_analyst_identity(),
                    "event_type": "freya.task_analysis.blocked", "status": "Failed",
                    "blocking_reason": reason,
                    "message": message,
                })
        raise ValueError(message)

    def _select_planned_task(self, oid: str, task: dict, run: dict) -> dict | None:
        planned_task_id = task["id"]
        with self.lock:
            if self.store.get_orchestration(oid)["status"] != "Running":
                return None
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.agent_selection.started", "status": "Running",
                "task_id": planned_task_id,
                "message": "Freya is ranking existing agents for the planned task.",
            })
        try:
            selection = self.selector.select_agent(
                task, self.store.list_agents(), self._selection_context(run),
            )
        except Exception as exc:
            with self.lock:
                if self.store.get_orchestration(oid)["status"] == "Running":
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.agent_selection.failed", "status": "Failed",
                        "task_id": planned_task_id,
                        "message": "Agent selection failed: " + str(exc),
                    })
            raise
        with self.lock:
            selection_id = self.store.save_agent_selection(oid, selection)
            if selection_id is None:
                return None
            selected_agent_id = selection["selected_agent_id"]
            if selected_agent_id is None:
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.agent_selection.failed", "status": "Failed",
                    "task_id": planned_task_id, "selector_version": selection["selector_version"],
                    "message": "No eligible or conditional agent is available for the planned task.",
                })
                self._fail_running(
                    oid, f"No eligible agent is available for planned task {planned_task_id}.",
                )
                return None
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.agent_selected", "status": "Running",
                "task_id": planned_task_id, "agent_id": selected_agent_id,
                "score": selection["score"], "selector_version": selection["selector_version"],
                "classification": selection["classification"],
                "approval_required": selection["approval_required"],
                "message": "Freya selected an existing agent for the planned task.",
            })
        return {
            "agent_id": selected_agent_id,
            "objective": task["objective"],
            "planned_task_id": planned_task_id,
            "selection_id": selection_id,
            "selection": selection,
        }

    def _record_planner_normalization(self, oid: str, planning_metrics: dict) -> None:
        normalization = planning_metrics.get("normalization")
        stable_ids = normalization.get("stable_ids") if isinstance(normalization, dict) else None
        if not isinstance(stable_ids, dict) or not stable_ids.get("assigned"):
            return
        self.store.add_orchestration_event(oid, {
            "event_type": "freya.planner.normalized", "status": "Planned",
            "message": "Freya assigned stable IDs to planner criteria.",
            "stable_ids": stable_ids,
        })

    def _record_plan_compiler_activity(self, oid: str, planning_metrics: dict) -> None:
        attempts = planning_metrics.get("semantic_compiler_attempts")
        if not isinstance(attempts, list):
            single = planning_metrics.get("semantic_compiler")
            attempts = [single] if isinstance(single, dict) else []
        if not attempts:
            return
        for compiler in attempts:
            if not isinstance(compiler, dict) or not compiler.get("started_at"):
                continue
            common = {
                "phase": "plan_compiler", "actor_type": "runtime",
                "attempt": compiler.get("attempt"),
                "task_ids": compiler.get("task_ids", []),
                "execution_strategy": compiler.get("execution_strategy"),
                "task_count": compiler.get("task_count"),
                "worker_count": compiler.get("worker_count"),
                "worker_assignments": compiler.get("worker_assignments", []),
                "global_criteria": compiler.get("global_criteria"),
                "duration_seconds": compiler.get("duration_seconds"),
                "semantic_plan_schema_version": compiler.get("semantic_plan_schema_version"),
                "compiled_plan_schema_version": compiler.get("compiled_plan_schema_version"),
            }
            semantic_plan = compiler.get("planner_semantic_plan")
            if isinstance(semantic_plan, dict) and semantic_plan.get("tasks"):
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.planner.semantic_plan_proposed",
                    "phase": "planner",
                    "actor_type": "model" if compiler.get("semantic_plan_source") == "planner_model"
                    else "runtime",
                    "semantic_plan_source": compiler.get("semantic_plan_source", "unknown"),
                    "semantic_plan_schema_version": compiler.get("semantic_plan_schema_version"),
                    "semantic_plan": semantic_plan,
                    "message": "Planner supplied task meaning and semantic operations.",
                })
                self.store.add_orchestration_event(oid, {
                    "event_type": "planner.semantic_plan_proposed",
                    "phase": "planner", "actor_type": "model"
                    if compiler.get("semantic_plan_source") == "planner_model" else "runtime",
                    "task_count": semantic_plan.get("task_count", 0),
                    "message": "Planner proposed a bounded semantic task plan.",
                })
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.plan_compiler.started", "timestamp": compiler["started_at"],
                "status": "Running", "message": "Plan Compiler started runtime preparation.",
                "planner_semantic_plan": compiler.get("planner_semantic_plan", {}),
                **common,
            })
            for diagnostic in compiler.get("compiler_events", []):
                if isinstance(diagnostic, dict) and isinstance(diagnostic.get("event_type"), str):
                    self.store.add_orchestration_event(oid, {
                        **diagnostic, "phase": "plan_compiler", "actor_type": "runtime",
                        "status": "Success" if compiler.get("status") == "Success" else "Warning",
                        "message": diagnostic.get("message", "Plan Compiler recorded a bounded decision."),
                    })
            for adjustment in compiler.get("scope_adjustments", []):
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.plan.scope_adjusted", "status": "Warning",
                    "phase": "plan_compiler", "actor_type": "runtime",
                    "task_key": adjustment.get("task_key", ""),
                    "scope_action": adjustment.get("action", ""),
                    "reason": adjustment.get("reason", ""),
                    "category": adjustment.get("category", ""),
                    "message": "Planner work outside the canonical Task Spec was omitted.",
                })
            succeeded = compiler.get("status") == "Success"
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.plan_compiler.completed" if succeeded
                else "freya.plan_compiler.failed",
                "timestamp": compiler.get("completed_at"),
                "status": "Success" if succeeded else "Failed",
                "message": "Plan Compiler completed runtime preparation." if succeeded
                else "Plan Compiler could not prepare the runtime plan.",
                **common,
                **({"error": compiler["error"]} if compiler.get("error") else {}),
                **({"preferred_skill_warnings": compiler["preferred_skill_warnings"]}
                   if compiler.get("preferred_skill_warnings") else {}),
                **({"resource_resolutions": compiler["resource_resolutions"]}
                   if compiler.get("resource_resolutions") else {}),
            })
            if succeeded and compiler.get("resource_resolutions"):
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.plan.resources_resolved",
                    "phase": "plan_compiler", "actor_type": "runtime",
                    "resource_resolutions": compiler["resource_resolutions"],
                    "resource_catalog_version": planning_metrics.get("resource_catalog_version"),
                    "message": "Runtime catalog derived tools and capabilities from semantic operations.",
                })
            if succeeded and compiler.get("ownership_resolutions"):
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.plan.ownership_resolved",
                    "phase": "plan_compiler", "actor_type": "runtime",
                    "ownership_resolutions": compiler["ownership_resolutions"],
                    "message": "Plan Compiler normalized and validated task-owned paths.",
                })
            if succeeded and compiler.get("compiled_runtime_plan"):
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.plan.compiled",
                    "phase": "plan_compiler", "actor_type": "runtime",
                    "compiled_plan_schema_version": compiler.get("compiled_plan_schema_version"),
                    "compiled_runtime_plan": compiler["compiled_runtime_plan"],
                    "message": "Plan Compiler produced the validated runtime contract.",
                })
        for diagnostic in planning_metrics.get("planner_events", []):
            if isinstance(diagnostic, dict) and isinstance(diagnostic.get("event_type"), str):
                self.store.add_orchestration_event(oid, {
                    **diagnostic, "phase": "planner", "actor_type": "runtime",
                    "status": "Success", "message": "Planner completed a bounded semantic repair step.",
                })

    def _fail_planning(self, oid: str, exc: Exception,
                       planning_metrics: dict | None = None) -> None:
        with self.lock:
            message = str(exc)
            metrics = planning_metrics or {}
            failed = self.store.transition_orchestration(
                oid, ("Analyzing", "Planning"), "Failed", error=message,
                planning_metrics=metrics,
            )
            if failed is None:
                return
            failure = {}
            if hasattr(exc, "resource_type"):
                failure = {
                    "resource_type": getattr(exc, "resource_type", ""),
                    "error_type": getattr(exc, "error_type", type(exc).__name__),
                    "unknown_resource_id": getattr(exc, "unknown_resource_id", ""),
                    "semantic_need": getattr(exc, "semantic_need", ""),
                }
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.planning.failed", "status": "Failed", "message": message,
                "planning_metrics": metrics, **failure,
            })
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.failed", "status": "Failed", "message": message,
            })
            self._archive_dynamic_agents(oid)

    def _fail_running(self, oid: str, message: str) -> None:
        with self.lock:
            if self.store.get_orchestration(oid)["status"] != "Running":
                return
            self._cancel_active_children(oid)
            failed = self.store.transition_orchestration(oid, ("Running",), "Failed", error=message)
            if failed is not None:
                self.store.cancel_cross_task_modification_requests(
                    oid, "The orchestration entered a terminal failure.",
                )
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.failed", "status": "Failed", "message": message,
                })
                self._archive_dynamic_agents(oid)

    def _cancel_active_children(self, oid: str) -> None:
        for delegation in self.store.get_orchestration(oid).get("delegations", []):
            task_id = delegation.get("task_id")
            if not task_id:
                continue
            try:
                task = self.store.get_task(task_id)
                if task["status"] in ACTIVE_DELEGATED_TASK_STATUSES:
                    task = self.runtime.cancel(task_id)
                self._snapshot_delegation(delegation["id"], task)
            except (KeyError, ValueError):
                continue

    def _close_terminal_attempts(self, oid: str, graph: ExecutionGraph) -> None:
        """Keep the append-only attempt ledger consistent with terminal graph state."""
        terminal = {"success", "failed", "blocked", "cancelled", "skipped", "superseded"}
        for node in graph.serialize():
            attempt = int(node.get("attempt", 0))
            if attempt > 0 and node.get("state") in terminal:
                self.store.update_execution_attempt(
                    oid, node["plan_task_id"], attempt, status=node["state"],
                    finished_at=node.get("finished_at") or utcnow(),
                )

    def _timeout(self, oid: str) -> None:
        graph = None
        with self.lock:
            run = self.store.get_orchestration(oid)
            if run["status"] != "Running":
                return
            self._cancel_active_children(oid)
            self.store.cancel_cross_task_modification_requests(
                oid, "The orchestration reached its wall-clock deadline.",
            )
            persisted = self.store.get_execution_graph(oid)
            if run.get("plan") and persisted["nodes"]:
                graph = ExecutionGraph(run.get("effective_plan") or run["plan"], persisted["nodes"])
                graph.cancel_nonterminal(
                    utcnow(), "Orchestration time limit reached.",
                )
                self.store.save_execution_graph(oid, graph.serialize())
                self._close_terminal_attempts(oid, graph)
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.timeout", "status": "Failed",
                    "message": "Orchestration time limit reached; active delegated tasks were cancelled.",
                    "summary": graph.summary(),
                })
        if graph is not None:
            # Use the ordinary graph failure path so a timeout receives the
            # same bounded root-cause report as every other terminal failure.
            self._finish_graph(oid, graph)
        else:
            self._fail_running(oid, "Orchestration time limit reached; active delegated tasks were cancelled.")

    def _abort_graph(self, oid: str, message: str) -> None:
        """Fail closed on an internal scheduler error without ghost-active nodes."""
        with self.lock:
            run = self.store.get_orchestration(oid)
            if run["status"] != "Running":
                return
            self._cancel_active_children(oid)
            persisted = self.store.get_execution_graph(oid)
            if run.get("plan") and persisted["nodes"]:
                graph = ExecutionGraph(run.get("effective_plan") or run["plan"], persisted["nodes"])
                graph.cancel_nonterminal(utcnow(), message)
                self.store.save_execution_graph(oid, graph.serialize())
                self._close_terminal_attempts(oid, graph)
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.graph.completed", "status": "Failed",
                    "message": "The execution graph stopped after an internal scheduler error.",
                    "summary": graph.summary(),
                })
            self._fail_running(oid, message)

    def _select_graph_task(self, oid: str, task: dict, run: dict) -> dict | None:
        """Create or reuse one dynamic agent, then validate it with AgentSelector."""
        planned_task_id = task["id"]
        with self.lock:
            current_run = self.store.get_orchestration(oid)
            if current_run["status"] != "Running":
                return None
            node = next(
                item for item in self.store.get_execution_graph(oid)["nodes"]
                if item["plan_task_id"] == planned_task_id
            )
            selection_attempt = int(node.get("attempt", 0)) + 1
            context = self._selection_context(current_run)
            required_agent_id = None
            excluded_agent_ids: list[str] = []
            recovery_workspace_state: dict[str, Any] = {}
            recovery_reason = ""
            recovery_cause = ""
            recovery_failure_class = ""
            recovery_action = None
            recovery_action_kind = None
            reused_worker = False
            worker_previous_task_id = ""
            worker_creation_reason = "assignment_started"
            plan_snapshot = current_run.get("effective_plan") or current_run.get("plan") or {}
            assignments_by_task = worker_assignment_map(plan_snapshot)
            raw_assignment = assignments_by_task.get(planned_task_id)
            worker_assignment = dict(raw_assignment) if raw_assignment else None
            if node.get("recovery_action_id"):
                recovery_action = self.store.get_recovery(node["recovery_action_id"])
                recovery_action_kind = recovery_action.get("action")
                recovery_reason = str(recovery_action.get("reason") or "").strip()
                recovery_workspace_state = dict(
                    (recovery_action.get("snapshot") or {}).get("workspace_state") or {}
                )
                prior = next((
                    item for item in self.store.list_execution_attempts(oid)
                    if item["plan_task_id"] == planned_task_id
                    and int(item["attempt"]) == int(recovery_action["source_attempt"])
                ), None)
                source_runtime_task = (
                    self.store.get_task(prior.get("runtime_task_id"))
                    if prior and prior.get("runtime_task_id") else None
                )
                if isinstance(source_runtime_task, dict):
                    recovery_failure_class = str(
                        source_runtime_task.get("failure_class")
                        or source_runtime_task.get("error_class")
                        or ""
                    ).strip()
                    recovery_cause = str(source_runtime_task.get("error") or "").strip()
                    verification = source_runtime_task.get("verification")
                    if not recovery_cause and isinstance(verification, dict):
                        recovery_cause = str(verification.get("summary") or "").strip()
                recovery_cause = recovery_cause or recovery_reason
                prior_agent_id = prior.get("selected_agent_id") if prior else None
                if recovery_action["action"] == "retry_same_agent":
                    required_agent_id = prior_agent_id
                elif recovery_action["action"] == "retry_different_agent":
                    prior_agent_ids = [
                        item.get("selected_agent_id")
                        for item in self.store.list_execution_attempts(oid)
                        if item["plan_task_id"] == planned_task_id
                    ]
                    excluded_agent_ids = list(dict.fromkeys([
                        *recovery_action.get("exclude_agent_ids", []),
                        *prior_agent_ids,
                    ]))
                    excluded_agent_ids = [item for item in excluded_agent_ids if item]
                    context["excluded_agent_ids"] = excluded_agent_ids

            try:
                if worker_assignment is not None:
                    factory_task = self._factory_task_scope(
                        task, plan_snapshot, recovery_workspace_state,
                        recovery_reason, recovery_failure_class, recovery_cause,
                    )
                    if recovery_action_kind == "retry_different_agent":
                        worker_assignment["generation"] = self._worker_generation(
                            oid, worker_assignment,
                        )
                        created = self._create_dynamic_agent(
                            oid, factory_task, selection_attempt,
                            variant=max(0, selection_attempt - 1),
                            worker_assignment=worker_assignment,
                            worker_creation_reason="explicit_recovery_replacement",
                        )
                        worker_creation_reason = "explicit_recovery_replacement"
                    else:
                        existing_worker = None
                        if required_agent_id:
                            try:
                                existing_worker = self.store.get_agent(required_agent_id)
                            except KeyError:
                                existing_worker = None
                            if existing_worker is None:
                                worker_creation_reason = "explicit_recovery_recreation"
                                required_agent_id = None
                        else:
                            existing_worker, worker_previous_task_id = (
                                self._worker_agent_for_assignment(oid, worker_assignment)
                            )
                            if existing_worker is not None:
                                worker_previous_task_id = (
                                    worker_previous_task_id
                                    or str(existing_worker.get("config", {}).get(
                                        "provenance", {}).get("plan_task_id") or "")
                                )
                        if existing_worker is None:
                            previous_attempts = [
                                item for item in self.store.list_execution_attempts(oid)
                                if item.get("plan_task_id") in worker_assignment["task_ids"]
                            ]
                            if (previous_attempts and not required_agent_id
                                    and not worker_creation_reason.startswith("explicit_recovery_")):
                                raise RuntimeError(
                                    "The assigned Worker is unavailable; Recovery must explicitly recreate it."
                                )
                            worker_assignment["generation"] = self._worker_generation(
                                oid, worker_assignment,
                            )
                            created = self._create_dynamic_agent(
                                oid, factory_task, selection_attempt,
                                variant=max(0, selection_attempt - 1),
                                worker_assignment=worker_assignment,
                                worker_creation_reason=worker_creation_reason,
                            )
                        else:
                            previous_provenance = existing_worker.get("config", {}).get(
                                "provenance", {},
                            )
                            stored_assignment = existing_worker.get("config", {}).get(
                                "worker_assignment", {},
                            )
                            worker_assignment["generation"] = int(
                                stored_assignment.get("generation") or 1
                            )
                            worker_previous_task_id = (
                                worker_previous_task_id
                                or str(previous_provenance.get("plan_task_id") or "")
                            )
                            activate = getattr(self.agent_factory, "activate_task", None)
                            if not callable(activate):
                                raise RuntimeError(
                                    "AgentFactory cannot activate task scope on a stable Worker."
                                )
                            created = activate(
                                existing_worker["id"], factory_task,
                                orchestration_id=oid,
                                worker_assignment=worker_assignment,
                                attempt=selection_attempt,
                                variant=max(0, selection_attempt - 1),
                            )
                            reused_worker = True
                            if required_agent_id:
                                worker_previous_task_id = str(
                                    previous_provenance.get("plan_task_id") or planned_task_id
                                )
                    agents = [created["agent"]]
                    if worker_assignment:
                        self.store.add_orchestration_event(oid, {
                            "event_type": "freya.agent_policy.validated",
                            "status": "Running", "task_id": planned_task_id,
                            "plan_task_id": planned_task_id,
                            "worker_id": worker_assignment["worker_id"],
                            "agent_id": created["agent"]["id"],
                            "role": created["role"],
                            "required_capabilities": created["required_capabilities"],
                            "active_capabilities": created.get(
                                "active_capabilities", created["required_capabilities"],
                            ),
                            "effective_tools": created["effective_tools"],
                            "message": "The reused Worker now has only this task's validated policy and tools.",
                        })
                elif required_agent_id:
                    agents = [self.store.get_agent(required_agent_id)]
                else:
                    factory_task = self._factory_task_scope(
                        task, plan_snapshot, recovery_workspace_state,
                        recovery_reason, recovery_failure_class, recovery_cause,
                    )
                    created = self._create_dynamic_agent(
                        oid, factory_task, selection_attempt,
                        variant=max(0, selection_attempt - 1),
                    )
                    agents = [created["agent"]]
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                return {"error": "Dynamic agent creation or reuse failed: " + str(exc)}

            self.store.add_orchestration_event(oid, {
                "event_type": "freya.agent_selection.started", "status": "Running",
                "task_id": planned_task_id,
                "agent_id": agents[0].get("id") if agents else None,
                "worker_id": worker_assignment.get("worker_id") if worker_assignment else None,
                "message": (
                    "Freya is validating the assigned Worker against "
                    "capabilities, Skills, runtime tools and policy."
                ),
            })
        try:
            selection = self.selector.select_agent(task, agents, context)
            selected = selection.get("selected_agent_id")
            if ((required_agent_id and selected not in {None, required_agent_id})
                    or selected in excluded_agent_ids):
                raise ValueError("Agent selector violated semantic recovery constraints.")
            selection = dict(selection)
            selection["attempt"] = selection_attempt
        except Exception as exc:
            with self.lock:
                if self.store.get_orchestration(oid)["status"] == "Running":
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.agent_selection.failed", "status": "Failed",
                        "task_id": planned_task_id,
                        "message": "Agent selection failed: " + str(exc),
                    })
            return {"error": "Agent selection failed: " + str(exc)}
        with self.lock:
            if selection["selected_agent_id"] is None:
                selection["failure_reason"] = self._selection_failure_message(
                    selection, agents
                )
            selection_id = self.store.save_agent_selection(oid, selection)
            if selection_id is None:
                return None
            selected_agent_id = selection["selected_agent_id"]
            if selected_agent_id is None:
                failure_reason = selection["failure_reason"]
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.agent_selection.failed", "status": "Failed",
                    "task_id": planned_task_id,
                    "selector_version": selection["selector_version"],
                    "selection_id": selection_id,
                    "message": failure_reason,
                })
            else:
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.agent_selected", "status": "Running",
                    "task_id": planned_task_id, "agent_id": selected_agent_id,
                    "worker_id": worker_assignment.get("worker_id") if worker_assignment else None,
                    "selection_id": selection_id, "score": selection["score"],
                    "selector_version": selection["selector_version"],
                    "classification": selection["classification"],
                    "approval_required": selection["approval_required"],
                    "message": "Freya validated and selected the dynamic agent.",
                })
                if worker_assignment is not None and reused_worker:
                    common = {
                        "worker_id": worker_assignment["worker_id"],
                        "agent_id": selected_agent_id,
                        "task_id": planned_task_id,
                        "orchestration_id": oid,
                        "execution_strategy": self._execution_strategy(oid),
                        "active_tools": list(created.get("effective_tools", [])),
                        "active_capabilities": list(created.get(
                            "active_capabilities", created.get("required_capabilities", []),
                        )),
                    }
                    self.store.add_orchestration_event(oid, {
                        "event_type": "worker.reused", "status": "Running",
                        **common,
                        "previous_task_id": worker_previous_task_id or planned_task_id,
                        "current_task_id": planned_task_id,
                        "message": "Reused the same Worker identity and activated this task's policy.",
                    })
                    if worker_previous_task_id and worker_previous_task_id != planned_task_id:
                        self.store.add_orchestration_event(oid, {
                            "event_type": "worker.task_switched", "status": "Running",
                            **common,
                            "previous_task_id": worker_previous_task_id,
                            "current_task_id": planned_task_id,
                            "message": "Switched the stable Worker to its next assigned task.",
                        })
        return {
            "selection_id": selection_id, "selection": selection,
            "worker_id": worker_assignment.get("worker_id") if worker_assignment else None,
            "active_tools": list(created.get("effective_tools", [])) if worker_assignment else [],
            "active_capabilities": list(created.get(
                "active_capabilities", created.get("required_capabilities", []),
            )) if worker_assignment else [],
            "previous_task_id": worker_previous_task_id,
        }

    def _record_graph_transitions(self, oid: str, transitions: list[dict]) -> None:
        for transition in transitions:
            task_id = transition["task_id"]
            if transition["to"] == "ready":
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.task.ready", "status": "Running", "task_id": task_id,
                    "message": "All dependencies succeeded; the planned task is ready.",
                })
            elif transition["to"] == "blocked":
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.task.blocked", "status": "Failed", "task_id": task_id,
                    "message": "The planned task was blocked by a failed dependency.",
                })

    @staticmethod
    def _evaluation_log_input(snapshot: dict) -> dict:
        """Build a bounded, sanitized record of the evidence shown to evaluation."""
        if not isinstance(snapshot, dict):
            return {}
        planned = snapshot.get("planned_task")
        planned = planned if isinstance(planned, dict) else {}
        runtime = snapshot.get("runtime_task")
        runtime = runtime if isinstance(runtime, dict) else {}
        verification = runtime.get("verification")
        verification = verification if isinstance(verification, dict) else {}
        evidence = []
        raw_evidence = verification.get("evidence", [])
        if not isinstance(raw_evidence, list):
            raw_evidence = []
        for item in raw_evidence[:8]:
            if not isinstance(item, dict):
                continue
            bounded = {
                key: item[key] for key in ("check", "status", "type", "tool", "exit_code")
                if key in item
            }
            for key in ("command", "supports_acceptance_criteria"):
                value = item.get(key)
                if isinstance(value, list):
                    bounded[key] = [str(part)[:300] for part in value[:10]]
            bounded["output"] = str(item.get("output") or "")[:1_200]
            evidence.append(bounded)
        result = str(runtime.get("result") or "")
        return sanitize({
            "planned_task": {
                key: planned.get(key)
                for key in ("id", "objective", "description", "success_criteria")
                if key in planned
            },
            "runtime_task": {
                "status": runtime.get("status"),
                "error": str(runtime.get("error") or "")[:1_000],
                "result_preview": result[:3_000],
                "verification": {
                    key: verification.get(key)
                    for key in ("requested", "attempted", "passed", "failed", "unavailable")
                    if key in verification
                } | {
                    "skipped_with_reason": str(verification.get("skipped_with_reason") or "")[:1_000],
                    "evidence": evidence,
                },
            },
            "context_truncated": bool(snapshot.get("context_truncated")),
        })

    def _evaluate_graph_node(self, oid: str, plan: dict, target: dict,
                             deadline: float) -> None:
        """Evaluate one technical success without holding the orchestration lock."""
        task_id = target["plan_task_id"]
        planned_task = next(task for task in plan["tasks"] if task["id"] == task_id)
        evaluation_id = str(uuid4())
        with self.lock:
            if self.store.get_orchestration(oid)["status"] != "Running":
                return
            current = ExecutionGraph(
                plan, self.store.get_execution_graph(oid)["nodes"],
            ).node(task_id)
            if (current["state"] != "evaluating" or current.get("evaluation_id")
                    or current.get("runtime_task_id") != target.get("runtime_task_id")
                    or int(current.get("attempt", 0)) != int(target.get("attempt", 0))):
                return
            runtime_task = self.store.get_task(target["runtime_task_id"])
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.evaluation.started", "status": "Running",
                "task_id": task_id, "plan_task_id": task_id,
                "agent_id": target.get("selected_agent_id"),
                "runtime_task_id": target.get("runtime_task_id"),
                "evaluation_id": evaluation_id,
                "evaluation_status": "evaluating",
                "evaluator_version": EVALUATOR_VERSION,
                "message": "Freya started evidence-first semantic evaluation.",
            })
        technical_error = None
        try:
            evaluation_task = dict(planned_task)
            criterion_links = plan.get("criterion_links")
            local_links = criterion_links.get("local", []) if isinstance(criterion_links, dict) else []
            if isinstance(local_links, list):
                evaluation_task["acceptance_criteria"] = [
                    {"id": item["id"], "criterion": item["criterion"]}
                    for item in local_links if isinstance(item, dict)
                    and item.get("task_id") == task_id
                    and isinstance(item.get("id"), str)
                    and isinstance(item.get("criterion"), str)
                    and item.get("criterion") in planned_task.get("success_criteria", [])
                ]
            with self.evaluator_lock:
                outcome = self.evaluator.evaluate(
                    planned_task=evaluation_task, runtime_task=runtime_task,
                    execution_node=target,
                )
        except Exception as exc:
            technical_error = str(exc)
            outcome = technical_failure_evaluation(
                technical_error, list(planned_task.get("success_criteria") or []),
            )
            outcome.update(
                metrics=dict(getattr(self.evaluator, "metrics", {}) or {}),
                context_truncated=bool(
                    getattr(self.evaluator, "last_context", {}).get("context_truncated", False)
                ),
                deterministic=False,
                context_snapshot=dict(getattr(self.evaluator, "last_context", {}) or {}),
                events=list(getattr(self.evaluator, "events", []) or []),
            )
        if self.clock() >= deadline:
            self._timeout(oid)
            return

        evaluation = {key: outcome[key] for key in EVALUATION_FIELDS if key in outcome}
        if outcome.get("status") == "error":
            evaluation = {key: outcome[key] for key in (
                "status", "confidence", "summary", "criteria", "issues",
                "missing_evidence", "evaluation_status", "failure_class",
                "recommended_runtime_action",
            ) if key in outcome}
        metrics = dict(outcome.get("metrics") or {})
        snapshot = {
            "evaluator_version": EVALUATOR_VERSION,
            "input": outcome.get("context_snapshot") or {},
            "evaluation": evaluation,
            "criterion_details": outcome.get("criterion_details") or [],
        }
        with self.lock:
            run = self.store.get_orchestration(oid)
            if run["status"] != "Running":
                return
            graph = ExecutionGraph(plan, self.store.get_execution_graph(oid)["nodes"])
            current = graph.node(task_id)
            if (current["state"] != "evaluating" or current.get("evaluation_id")
                    or current.get("runtime_task_id") != target.get("runtime_task_id")
                    or int(current.get("attempt", 0)) != int(target.get("attempt", 0))):
                return
            graph.apply_evaluation(
                task_id, evaluation_id, evaluation["status"], evaluation["summary"], utcnow(),
            )
            record = self.store.commit_evaluation(
                evaluation_id, oid, task_id,
                runtime_task_id=target["runtime_task_id"],
                agent_id=target["selected_agent_id"], attempt=int(target["attempt"]),
                evaluator_version=EVALUATOR_VERSION, evaluation=evaluation,
                metrics=metrics, snapshot=snapshot,
                context_truncated=bool(outcome.get("context_truncated")),
                deterministic=bool(outcome.get("deterministic")),
            )
            if record is None:
                return
            if evaluation["status"] == "accepted" and runtime_task.get("workspace"):
                self.project_state.accept_task_update(
                    oid, plan, planned_task, runtime_task,
                    target.get("selected_agent_id"),
                )
            event_type = ("freya.evaluation.failed" if technical_error
                          else "freya.evaluation.completed")
            evaluation_output = {
                "decision": evaluation,
                "deterministic": bool(outcome.get("deterministic")),
                "context_truncated": bool(outcome.get("context_truncated")),
                "metrics": metrics,
            }
            if evaluation["status"] != "accepted":
                evaluation_output["input"] = self._evaluation_log_input(
                    outcome.get("context_snapshot") or {},
                )
            if evaluation["status"] == "error":
                self.store.add_orchestration_event(oid, {
                    "event_type": "evaluation.infrastructure_failed", "status": "Failed",
                    "task_id": task_id, "evaluation_id": evaluation_id,
                    "evaluation_status": "error", "message": evaluation["summary"],
                })
            for evaluator_event in outcome.get("events") or []:
                self.store.add_orchestration_event(oid, {
                    **evaluator_event, "status": evaluator_event.get("status", "Success"),
                    "task_id": task_id, "evaluation_id": evaluation_id,
                })
            self.store.add_orchestration_event(oid, {
                "event_type": event_type,
                "status": "Failed" if evaluation["status"] != "accepted" else "Success",
                "task_id": task_id, "plan_task_id": task_id,
                "agent_id": target.get("selected_agent_id"),
                "runtime_task_id": target.get("runtime_task_id"),
                "evaluation_id": evaluation_id,
                "evaluation_status": evaluation["status"],
                "evaluator_version": EVALUATOR_VERSION,
                "message": evaluation["summary"],
                "output": evaluation_output,
            })
            if evaluation["status"] == "accepted":
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.task.succeeded", "status": "Success",
                    "task_id": task_id, "plan_task_id": task_id,
                    "agent_id": target.get("selected_agent_id"),
                    "runtime_task_id": target.get("runtime_task_id"),
                    "evaluation_id": evaluation_id,
                    "evaluation_status": evaluation["status"],
                    "message": "Semantic evaluation accepted the planned task.",
                })


    def _recover_graph_node(self, oid: str, plan: dict, target: dict,
                            deadline: float) -> None:
        """Decide and commit recovery without allowing a late result to reopen state."""
        task_id = target["plan_task_id"]
        planned_task = next(task for task in plan["tasks"] if task["id"] == task_id)
        evaluation = self.store.get_evaluation(target["evaluation_id"])
        previous_runtime_task = self.store.get_task(target.get("runtime_task_id")) if target.get("runtime_task_id") else None
        workspace_state = self._recovery_workspace_state(previous_runtime_task)
        recoveries = self.store.list_recoveries(oid)
        revisions = self.store.list_plan_revisions(oid)
        history = [item for item in recoveries
                   if item["plan_task_id"] == task_id]
        def recorded_model_calls(item: dict) -> int:
            value = (item.get("metrics") or {}).get("model_calls", 0)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

        model_calls_used = sum(recorded_model_calls(item) for item in [*recoveries, *revisions])
        if len(recoveries) >= int(self.config["max_recovery_actions"]):
            reason = "Recovery action budget exhausted."
            with self.lock:
                if self.store.fail_recovery_pending(
                        oid, task_id, attempt=int(target["attempt"]),
                        evaluation_id=target["evaluation_id"], reason=reason):
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.recovery.exhausted", "status": "Failed",
                        "task_id": task_id, "evaluation_id": target["evaluation_id"],
                        "message": reason,
                    })
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.task.failed", "status": "Failed",
                        "task_id": task_id, "evaluation_id": target["evaluation_id"],
                        "message": reason,
                    })
            return
        recovery_id = str(uuid4())
        with self.lock:
            if self.store.get_orchestration(oid)["status"] != "Running":
                return
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.recovery.started", "status": "Running",
                "task_id": task_id, "evaluation_id": target["evaluation_id"],
                "attempt": target["attempt"], "recovery_id": recovery_id,
                "message": "Freya started bounded semantic recovery.",
            })
        limits = {
            "max_semantic_attempts_per_task": self.config["max_semantic_attempts_per_task"],
            "max_plan_revisions": self.config["max_plan_revisions"],
            "max_recovery_actions": self.config["max_recovery_actions"],
            "max_recovery_model_calls": self.config["max_recovery_model_calls"],
            "recovery_action_count": len(recoveries),
            "plan_revision_count": len(revisions),
            "recovery_model_calls_used": model_calls_used,
        }
        try:
            current_agents = []
            current_agent_id = target.get("selected_agent_id")
            if current_agent_id:
                try:
                    current_agents = [self.store.get_agent(current_agent_id)]
                except KeyError:
                    current_agents = []
            with self.recovery_lock:
                decision = self.recovery.decide(
                    planned_task=planned_task, execution_node=target,
                    evaluation=evaluation, history=history,
                    available_agents=current_agents, plan=plan, limits=limits,
                    can_create_agent=True,
                    workspace_state=workspace_state,
                )
        except Exception as exc:
            decision = {
                "action": "fail", "reason": "Recovery decision failed strict validation: " + str(exc),
                "instructions": "", "exclude_agent_ids": [],
                "affected_task_ids": [task_id],
                "fingerprint": semantic_failure_fingerprint(
                    task_id, target.get("selected_agent_id") or "", evaluation,
                ),
                "metrics": dict(getattr(self.recovery, "metrics", {}) or {}),
            }
        if self.clock() >= deadline:
            self._timeout(oid)
            return
        allowed_scope: set[str] = set()
        protected_scope: set[str] = set()
        if decision["action"] == "replan_subgraph":
            graph_snapshot = self.store.get_execution_graph(oid)["nodes"]
            try:
                allowed_scope = allowed_replan_scope(
                    task_id, plan, graph_snapshot, self.store.list_execution_attempts(oid),
                )
                protected_scope = {item["plan_task_id"] for item in graph_snapshot} - allowed_scope
                requested = set(decision["affected_task_ids"])
                if task_id not in requested or not requested <= allowed_scope:
                    raise ValueError("Recovery requested tasks outside the safe replanning scope.")
            except ValueError as exc:
                decision = {
                    **decision, "action": "fail", "instructions": "",
                    "exclude_agent_ids": [], "affected_task_ids": [task_id],
                    "reason": str(exc),
                }
        retry_prompt = ""
        if decision["action"] in {"retry_same_agent", "retry_different_agent"}:
            retry_prompt = build_retry_prompt(
                planned_task, evaluation, decision["instructions"],
                attempt=int(target["attempt"]) + 1,
                workspace_state=workspace_state,
            )
        with self.lock:
            run = self.store.get_orchestration(oid)
            if run["status"] != "Running":
                return
            current = next(item for item in self.store.get_execution_graph(oid)["nodes"]
                           if item["plan_task_id"] == task_id)
            if (current["state"] != "recovery_pending"
                    or current.get("evaluation_id") != target.get("evaluation_id")
                    or int(current.get("attempt", 0)) != int(target.get("attempt", 0))
                    or current.get("recovery_action_id")):
                return
            record = self.store.commit_recovery_action(
                recovery_id, oid, task_id, source_attempt=int(target["attempt"]),
                source_evaluation_id=target["evaluation_id"], decision=decision,
                recovery_version=RECOVERY_VERSION, prompt=retry_prompt,
                snapshot={"evaluation_status": evaluation.get("status"), "limits": limits,
                          "allowed_replan_scope": sorted(allowed_scope),
                          "workspace_state": workspace_state},
            )
            if record is None:
                return
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.recovery.decided", "status": "Running",
                "task_id": task_id, "recovery_id": recovery_id,
                "action": decision["action"], "reason": decision["reason"],
                "workspace_state": workspace_state,
                "selected_strategy": decision["action"],
                "message": "Freya committed a bounded recovery decision.",
            })
            if decision["action"] == "fail":
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.recovery.exhausted", "status": "Failed",
                    "task_id": task_id, "recovery_id": recovery_id,
                    "message": decision["reason"],
                })
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.task.failed", "status": "Failed",
                    "task_id": task_id, "evaluation_id": target["evaluation_id"],
                    "message": decision["reason"],
                })
                return
            if decision["action"] in {"retry_same_agent", "retry_different_agent"}:
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.recovery.retry_scheduled", "status": "Running",
                    "task_id": task_id, "recovery_id": recovery_id,
                    "action": decision["action"], "next_attempt": int(target["attempt"]) + 1,
                    "message": "Freya scheduled a new, independently selected execution attempt.",
                })
                return

        try:
            accepted = {item["plan_task_id"] for item in self.store.get_execution_graph(oid)["nodes"]
                        if item["state"] == "success"}
            historical = {item["plan_task_id"] for item in self.store.get_execution_graph(oid)["nodes"]}
            with self.recovery_lock:
                revision = self.replanner.create_revision(
                    current_plan=plan, source_task_id=task_id,
                    affected_task_ids=decision["affected_task_ids"],
                    allowed_task_ids=allowed_scope, protected_task_ids=protected_scope,
                    accepted_task_ids=accepted, historical_task_ids=historical,
                    context={"evaluation": evaluation, "instructions": decision["instructions"]},
                    max_tasks=int(self.config["max_delegated_tasks"]),
                    max_model_calls=min(
                        2, int(self.config["max_recovery_model_calls"])
                        - model_calls_used - recorded_model_calls(decision)
                    ),
                )
            if self.clock() >= deadline:
                self._timeout(oid)
                return
            revision_id = str(uuid4())
            with self.lock:
                run = self.store.get_orchestration(oid)
                current = next(item for item in self.store.get_execution_graph(oid)["nodes"]
                               if item["plan_task_id"] == task_id)
                if (run["status"] != "Running" or current["state"] != "recovery_pending"
                        or current.get("recovery_action_id") != recovery_id):
                    return
                saved = self.store.commit_plan_revision(
                    revision_id, oid, recovery_id, summary=revision["summary"],
                    plan=revision["plan"], superseded_task_ids=revision["superseded_task_ids"],
                    metrics=revision.get("metrics"),
                )
                if saved is None:
                    return
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.recovery.replan_created", "status": "Running",
                    "id_allocation": revision.get("id_allocation", {}),
                    "task_id": task_id, "recovery_id": recovery_id,
                    "plan_revision_id": revision_id, "revision": saved["revision"],
                    "resource_resolutions": revision.get("resource_resolutions", []),
                    "message": "Freya committed a validated effective-plan revision.",
                })
        except Exception as exc:
            with self.lock:
                if self.store.get_orchestration(oid)["status"] == "Running" and self.store.fail_recovery_action(
                        recovery_id, "Subgraph replanning failed: " + str(exc)):
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.recovery.exhausted", "status": "Failed",
                        "task_id": task_id, "recovery_id": recovery_id,
                        "message": "Subgraph replanning failed: " + str(exc),
                    })

    def _finish_graph(self, oid: str, graph: ExecutionGraph) -> None:
        summary = graph.summary()
        unsuccessful = (summary["failed"] + summary["blocked"] + summary["cancelled"]
                        + summary["skipped"])
        if unsuccessful:
            semantic_failures = [
                node for node in graph.serialize()
                if node.get("evaluation_status") not in (None, "accepted")
            ]
            if semantic_failures:
                message = (
                    "Execution completed, but semantic evaluation rejected one or more "
                    "planned tasks. "
                )
            else:
                message = "Execution graph completed unsuccessfully. "
            message += (
                f"{summary['failed']} failed, {summary['blocked']} blocked, "
                f"{summary['cancelled']} cancelled and {summary['skipped']} skipped nodes."
            )
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.failure_analysis.started", "status": "Running",
                "analysis_version": FAILURE_ANALYSIS_VERSION,
                "message": "Freya started one bounded review using persisted sanitized logs only.",
            })
            diagnosis, analysis_metrics, analysis_mode = self._analyze_graph_failure(oid, graph)
            report = (
                "Failure diagnosis\n"
                f"Cause: {diagnosis['cause']}\n"
                f"Evidence logs: {', '.join(diagnosis['evidence_log_ids'])}\n"
                f"Retryable: {'yes' if diagnosis['retryable'] else 'no'}\n"
                f"Recommended action: {diagnosis['recommended_action']}"
            )
            message += " Cause: " + diagnosis["cause"]
            with self.lock:
                if self.store.get_orchestration(oid)["status"] != "Running":
                    return
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.failure_analysis.completed", "status": "Success",
                    "analysis_version": FAILURE_ANALYSIS_VERSION,
                    "analysis_mode": analysis_mode,
                    "cause": diagnosis["cause"],
                    "evidence_log_ids": diagnosis["evidence_log_ids"],
                    "retryable": diagnosis["retryable"],
                    "recommended_action": diagnosis["recommended_action"],
                    "metrics": analysis_metrics,
                    "message": report,
                })
                completed = self.store.transition_orchestration(
                    oid, ("Running",), "Failed", error=message, response=report,
                )
            status = "Failed"
        else:
            raise ValueError("Successful graphs must pass through global integration.")
        if completed is None:
            return
        self.store.add_orchestration_event(oid, {
            "event_type": "freya.graph.completed", "status": status,
            "message": "The execution graph reached a terminal state.", "summary": summary,
        })
        self.store.add_orchestration_event(oid, {
            "event_type": "freya.completed" if status == "Success" else "freya.failed",
            "status": status, "message": message,
        })

        self._archive_dynamic_agents(oid)

    def _cross_task_event(self, oid: str, event_type: str, request: dict[str, Any],
                          *, status: str | None = None, **extra: Any) -> None:
        payload = {
            "event_type": event_type, "status": status or request.get("status", "Pending"),
            "request_id": request.get("id"), "approval_id": request.get("approval_id"),
            "requester_plan_task_id": request.get("requester_plan_task_id"),
            "requester_runtime_task_id": request.get("requester_runtime_task_id"),
            "target_owner_plan_task_id": request.get("target_owner_plan_task_id"),
            "target_path": request.get("target_path"),
            "requester_observed_revision": request.get("requester_observed_revision", 0),
            "requested_operation": request.get("requested_operation"),
            "requested_change": request.get("requested_change"),
            "reason": request.get("reason"), "needed_for": request.get("needed_for"),
            "blocking": request.get("blocking"),
            "approval_source": request.get("approval_source", ""),
            "grant_id": request.get("grant_id"),
        }
        payload.update(extra)
        payload.setdefault("message", event_type.replace(".", " "))
        self.store.add_orchestration_event(oid, sanitize(payload))

    @staticmethod
    def _cross_task_reaches(edges: dict[str, set[str]], start: str, target: str) -> bool:
        pending = [start]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(edges.get(current, set()) - visited)
        return False

    def _cross_task_cycle_would_form(self, plan: dict[str, Any], request: dict[str, Any]) -> bool:
        requester = str(request.get("requester_plan_task_id") or "")
        owner = str(request.get("target_owner_plan_task_id") or "")
        if not requester or not owner or requester == owner:
            return True
        edges: dict[str, set[str]] = {}
        for task in plan.get("tasks", []):
            task_id = str(task.get("id") or "")
            edges[task_id] = set(task.get("depends_on", []))
        for existing in self.store.list_cross_task_modification_requests(
                request["orchestration_id"]):
            if (existing.get("id") == request.get("id") or not existing.get("blocking")
                    or existing.get("status") in {"completed", "denied", "blocked", "cycle_detected", "cancelled"}):
                continue
            source = str(existing.get("requester_plan_task_id") or "")
            target = str(existing.get("target_owner_plan_task_id") or "")
            if source and target:
                edges.setdefault(source, set()).add(target)
        # A requester waits on its owner, so owner -> requester closes a cycle.
        return self._cross_task_reaches(edges, owner, requester)

    def _resolve_pending_cross_task_intents(self, oid: str) -> None:
        """Match reusable grants outside the orchestration lock; uncertainty asks a human."""
        run = self.store.get_orchestration(oid)
        if run["status"] != "Running":
            return
        plan = run.get("effective_plan") or run.get("plan") or {}
        planned = {str(item.get("id")): item for item in plan.get("tasks", [])}
        for request in self.store.list_cross_task_modification_requests(oid, ["pending"]):
            self._cross_task_event(oid, "cross_task_modification.requested", request,
                                   status="Pending", approval_id=request.get("approval_id"))
            owner = planned.get(request.get("target_owner_plan_task_id"))
            owned = {owned_path_key(path) for path in (owner or {}).get("owned_paths", [])}
            ownership_index = plan.get("write_owners") or {
                owned_path_key(path): item["id"] for item in plan.get("tasks", [])
                for path in item.get("owned_paths", [])}
            indexed_owner = ownership_index.get(owned_path_key(request.get("target_path")))
            cycle = owner is not None and self._cross_task_cycle_would_form(plan, request)
            in_owner_scope = (owner is not None and indexed_owner == owner.get("id")
                              and owned_path_key(request.get("target_path")) in owned)
            if in_owner_scope:
                self._cross_task_event(oid, "cross_task_modification.owner_resolved", request,
                                       status="Running")
            owner_can_write = False
            if in_owner_scope:
                try:
                    self._cross_task_change_task(plan, owner, request)
                    owner_can_write = True
                except (KeyError, TypeError, ValueError):
                    owner_can_write = False
            if not in_owner_scope or not owner_can_write or cycle:
                reason = ("cross_task_dependency_cycle" if cycle else
                          "The scoped owner action cannot serve this request."
                          if in_owner_scope else "The request does not match a plan-owned file.")
                blocked = self.store.block_cross_task_modification_request(
                    request["id"], status="cycle_detected" if cycle else "blocked", reason=reason,
                    expected_statuses=["pending"],
                )
                if blocked:
                    self._cross_task_event(
                        oid, "cross_task_modification.cycle_detected" if cycle
                        else "cross_task_modification.blocked", blocked,
                        status=blocked["status"], message=reason,
                    )
                continue
            snapshot = {key: owner.get(key) for key in (
                "id", "objective", "description", "depends_on", "required_capabilities",
                "required_tools", "semantic_needs", "preferred_skills", "success_criteria",
                "owned_paths", "task_kind", "task_characteristics",
            ) if key in owner}
            self.store.update_cross_task_modification_request(
                request["id"], expected_statuses=["pending"], status="pending",
                owner_task_snapshot=snapshot,
            )
            grants = self.store.find_cross_task_intent_grants(request)
            if not grants:
                awaiting = self.store.update_cross_task_modification_request(
                    request["id"], expected_statuses=["pending"], status="awaiting_human",
                    approval_source="no_reusable_match",
                    intent_match={"same_intent": None, "reason": "No reusable grant exists in this scope.",
                                  "confidence": 0.0, "method": "no_grant"},
                )
                if awaiting:
                    self._cross_task_event(
                        oid, "cross_task_modification.awaiting_human", awaiting,
                        status="WaitingForApproval",
                        message="No reusable same-purpose grant matched; operator approval is required.",
                    )
                continue
            last_match: dict[str, Any] = {
                "same_intent": False, "reason": "No grant matched the new purpose.",
                "confidence": 0.0, "method": "reusable_grant_miss",
            }
            auto_approved = None
            for grant in grants:
                try:
                    match = self.cross_task_intent_matcher.match(
                        grant.get("approved_intent") or {}, request,
                    )
                    match = {**match, "grant_id": grant["id"]}
                except Exception as exc:
                    match = {
                        "same_intent": False,
                        "reason": "Intent matcher unavailable; request remains with the human.",
                        "confidence": 0.0, "method": "unavailable",
                        "matcher_error": sanitize(str(exc))[:300], "grant_id": grant["id"],
                    }
                last_match = match
                self._cross_task_event(
                    oid, "cross_task_modification.intent_match", request,
                    status="Running", grant_id=grant["id"], intent_match=match,
                    message="Freya recorded the deterministic or semantic reusable-intent comparison.",
                )
                if match.get("same_intent") is True and float(match.get("confidence", 0)) >= 0.90:
                    auto_approved = self.store.auto_approve_cross_task_modification_request(
                        request["id"], grant_id=grant["id"], intent_match=match,
                    )
                    if auto_approved:
                        break
            if auto_approved:
                self._cross_task_event(
                    oid, "cross_task_modification.auto_approved", auto_approved,
                    status="Approved", approval_source="automatic_reuse",
                    grant_id=auto_approved.get("grant_id"), intent_match=last_match,
                    message="An existing same-scope human grant matched the approved intent.",
                )
            else:
                awaiting = self.store.update_cross_task_modification_request(
                    request["id"], expected_statuses=["pending"], status="awaiting_human",
                    approval_source="reusable_match_uncertain", intent_match=last_match,
                )
                if awaiting:
                    self._cross_task_event(
                        oid, "cross_task_modification.awaiting_human", awaiting,
                        status="WaitingForApproval", intent_match=last_match,
                        message="Intent match was different or uncertain; operator approval is required.",
                    )

    @staticmethod
    def _cross_task_change_task(plan: dict[str, Any], owner: dict[str, Any],
                                request: dict[str, Any]) -> dict[str, Any]:
        """Build an exact-file agent action for the permanent plan-task owner."""
        target_path = str(request.get("target_path") or "")
        target_key = owned_path_key(target_path)
        owner_paths = {owned_path_key(item) for item in owner.get("owned_paths", [])}
        if target_key not in owner_paths:
            raise ValueError("The requested file is outside the owning task's declared scope.")
        ownership_index = plan.get("write_owners") or {
            owned_path_key(path): item["id"] for item in plan.get("tasks", [])
            for path in item.get("owned_paths", [])}
        indexed_owner = ownership_index.get(target_key)
        if indexed_owner != owner.get("id"):
            raise ValueError("The permanent plan ownership index disagrees with the owner task.")
        operation = str(request.get("requested_operation") or "")
        requested_capability = "filesystem." + operation
        if operation not in {"create", "modify", "overwrite"}:
            raise ValueError("The requested filesystem write operation is invalid.")
        write_capabilities = [requested_capability]
        requester = str(request.get("requester_plan_task_id") or "requester")
        change = str(request.get("requested_change") or "").strip()
        reason = str(request.get("reason") or "").strip()
        needed_for = str(request.get("needed_for") or "").strip()
        owners = dict(ownership_index)
        return {
            # Keep the original owner plan-task identity in agent provenance so
            # Worker enforces the same declared owner at the filesystem gate.
            "id": str(owner["id"]),
            "objective": f"Apply the approved change to {target_path}: {change}",
            "description": (
                f"A requesting plan task ({requester}) needs a scoped change to this owner task's file. "
                f"Requested change: {change}\nReason: {reason}\nNeeded for: {needed_for}\n"
                f"Modify only {target_path}. Preserve the existing project conventions. Read the file back "
                "after the edit. Do not run commands, touch other files, or expand this request."
            ),
            "depends_on": [],
            "required_capabilities": ["filesystem.read", *write_capabilities],
            "required_tools": [],
            "semantic_needs": ["Apply and read back the explicitly approved change to the exact owned file."],
            "owned_paths": [target_path],
            "write_targets": [target_path],
            "foreign_write_targets": [],
            "preferred_skills": list(owner.get("preferred_skills", [])),
            "success_criteria": [
                f"The approved requested change is applied to {target_path}.",
                f"The updated {target_path} is read back successfully after the edit.",
            ],
            "task_kind": "code_change",
            "task_characteristics": {},
            "_write_owners": owners,
        }

    def _resume_cross_task_requester(self, oid: str, request: dict[str, Any],
                                     outcome: str, explanation: str) -> bool:
        """Start a fresh normal graph attempt after the owner handoff resolves."""
        with self.lock:
            run = self.store.get_orchestration(oid)
            if run["status"] != "Running":
                return False
            plan = run.get("effective_plan") or run.get("plan") or {}
            graph_record = self.store.get_execution_graph(oid)
            graph = ExecutionGraph(plan, graph_record["nodes"])
            task_id = str(request.get("requester_plan_task_id") or "")
            try:
                node = graph.node(task_id)
            except KeyError:
                return False
            if node["state"] != "waiting_for_approval":
                return False
            if outcome == "completed":
                instruction = (
                    "The file owner applied and independently verified the approved change. Re-read the file "
                    "if needed, continue the original task, and do not repeat the coordinated edit."
                )
            else:
                instruction = (
                    "The coordinated edit did not proceed: " + sanitize(explanation)[:1000] +
                    " Choose another permitted approach or report the remaining blocker. Do not retry the "
                    "same cross-task write unchanged."
                )
            prompt = (
                "CROSS-TASK FILE COORDINATION RESULT\n"
                f"Target file: {request.get('target_path', '')}\n"
                f"Owner task: {request.get('target_owner_plan_task_id', '')}\n"
                f"Outcome: {outcome}\n{instruction}"
            )
            graph.resume_cross_task_wait(task_id, prompt, utcnow())
            self.store.save_execution_graph(oid, graph.serialize())
            self._cross_task_event(
                oid, "cross_task_modification.requester_resumed", request,
                status="Running", outcome=outcome,
                message="The requester will continue in a fresh normal execution attempt.",
            )
            return True

    def _dispatch_cross_task_owner_change(self, oid: str, request: dict[str, Any],
                                          deadline: float, operational_prompt: str) -> None:
        run = self.store.get_orchestration(oid)
        if run["status"] != "Running":
            return
        plan = run.get("effective_plan") or run.get("plan") or {}
        owner = next((item for item in plan.get("tasks", [])
                      if item.get("id") == request.get("target_owner_plan_task_id")), None)
        if owner is not None and self._cross_task_cycle_would_form(plan, request):
            reason = "The effective plan now creates a cross-task dependency cycle."
            blocked = self.store.block_cross_task_modification_request(
                request["id"], status="cycle_detected", reason=reason,
                expected_statuses=["owner_selecting"],
            )
            if blocked:
                self._cross_task_event(oid, "cross_task_modification.cycle_detected", blocked,
                                       status="cycle_detected", message=reason)
                self._resume_cross_task_requester(oid, blocked, "cycle_detected", reason)
            return
        if owner is None:
            reason = "The owning plan task is no longer present in the effective plan."
            blocked = self.store.block_cross_task_modification_request(
                request["id"], status="blocked", reason=reason,
                expected_statuses=["owner_selecting"],
            )
            if blocked:
                self._cross_task_event(oid, "cross_task_modification.blocked", blocked,
                                       status="blocked", message=reason)
                self._resume_cross_task_requester(oid, blocked, "blocked", reason)
            return
        runtime_task = None
        try:
            task = self._cross_task_change_task(plan, owner, request)
            created = self._create_dynamic_agent(oid, task, 1)
            self._cross_task_event(oid, "cross_task_modification.owner_agent_created", request,
                                   status="Running", owner_agent_id=created["agent"]["id"])
            context = self._selection_context(run)
            selection = self.selector.select_agent(task, [created["agent"]], context)
            selection = dict(selection)
            selection["task_id"] = task["id"]
            selection["attempt"] = 1
            with self.lock:
                if self.store.get_orchestration(oid)["status"] != "Running":
                    self._archive_dynamic_agents(oid)
                    return
                selection_id = self.store.save_agent_selection(oid, selection)
                selected_agent_id = selection.get("selected_agent_id")
                if selection_id is None or not selected_agent_id:
                    reason = self._selection_failure_message(selection, [created["agent"]])
                    blocked = self.store.block_cross_task_modification_request(
                        request["id"], status="blocked", reason=reason,
                        expected_statuses=["owner_selecting"],
                    )
                    if blocked:
                        self._cross_task_event(oid, "cross_task_modification.blocked", blocked,
                                               status="blocked", message=reason)
                    self._resume_cross_task_requester(oid, request, "blocked", reason)
                    return
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.agent_selected", "status": "Running",
                    "task_id": task["id"], "agent_id": selected_agent_id,
                    "selection_id": selection_id, "score": selection.get("score"),
                    "selector_version": selection.get("selector_version"),
                    "message": "AgentSelector validated the owner-scoped change agent.",
                    "cross_task_request_id": request["id"],
                })
            if self.clock() >= deadline:
                self._timeout(oid)
                return
            runtime_context, project_context = self._project_context_for_dispatch(
                oid, plan, task, run,
            )
            prompt = self._execution_prompt(
                operational_prompt,
                task,
                "Perform only the approved, exact-file change recorded by Freya. "
                "The approval covers this request; it does not grant capabilities or broader file access. "
                f"Coordination request ID: {request['id']}. "
                f"The requester observed artifact revision {request.get('requester_observed_revision', 0)}; "
                "inspect the current project context and read the latest file before editing.",
                project_context,
                self._plan_context_for_dispatch(oid, plan, task),
            )
            with self.lock:
                if self.store.get_orchestration(oid)["status"] != "Running":
                    self._archive_dynamic_agents(oid)
                    return
                runtime_task = self._submit_runtime_task(
                    selected_agent_id, prompt, self._workspace_for_run(run), runtime_context,
                )
                delegation_id = self.store.add_delegation(
                    oid, selected_agent_id, prompt, runtime_task["id"],
                )
                if delegation_id is None:
                    self.runtime.cancel(runtime_task["id"])
                    return
                dispatched = self.store.update_cross_task_modification_request(
                    request["id"], expected_statuses=["owner_selecting"],
                    status="owner_running", owner_runtime_task_id=runtime_task["id"],
                    owner_agent_id=selected_agent_id,
                )
                if dispatched is None:
                    self.runtime.cancel(runtime_task["id"])
                    return
                self._cross_task_event(oid, "cross_task_modification.owner_action_started",
                                       dispatched, status="Running",
                                       owner_runtime_task_id=runtime_task["id"])
                self.store.add_orchestration_event(oid, {
                    "event_type": "cross_task_modification.dispatched",
                    "status": runtime_task["status"],
                    "request_id": request["id"], "owner_runtime_task_id": runtime_task["id"],
                    "owner_agent_id": selected_agent_id,
                    "delegation_id": delegation_id,
                    "message": "Freya dispatched an ephemeral agent using only the owner's declared file capabilities.",
                })
        except Exception as exc:
            if runtime_task is not None:
                try:
                    self.runtime.cancel(runtime_task["id"])
                except (KeyError, ValueError):
                    pass
            reason = "Owner-scoped change agent could not be dispatched: " + sanitize(str(exc))[:800]
            blocked = self.store.block_cross_task_modification_request(
                request["id"], status="blocked", reason=reason,
                expected_statuses=["owner_selecting"],
            )
            if blocked:
                self._cross_task_event(oid, "cross_task_modification.failed", blocked,
                                       status="blocked", message=reason)
                self._cross_task_event(oid, "cross_task_modification.blocked", blocked,
                                       status="blocked", message=reason)
                self._resume_cross_task_requester(oid, blocked, "blocked", reason)

    def _evaluate_cross_task_owner_change(self, oid: str, request: dict[str, Any],
                                          deadline: float) -> None:
        run = self.store.get_orchestration(oid)
        if run["status"] != "Running":
            return
        plan = run.get("effective_plan") or run.get("plan") or {}
        owner = next((item for item in plan.get("tasks", [])
                      if item.get("id") == request.get("target_owner_plan_task_id")), None)
        if owner is None:
            outcome = {"status": "rejected", "summary": "The owner task is missing from the effective plan."}
        else:
            try:
                planned = self._cross_task_change_task(plan, owner, request)
                runtime_task = self.store.get_task(request["owner_runtime_task_id"])
                execution_node = {
                    "selected_agent_id": request.get("owner_agent_id"),
                    "runtime_task_id": request.get("owner_runtime_task_id"),
                    "attempt": 1,
                }
                with self.evaluator_lock:
                    outcome = self.evaluator.evaluate(
                        planned_task=planned, runtime_task=runtime_task,
                        execution_node=execution_node,
                        context={"cross_task_request_id": request["id"],
                                 "target_path": request["target_path"],
                                 "approved_intent": {key: request.get(key) for key in
                                     ("requested_change", "reason", "needed_for")}},
                    )
            except Exception as exc:
                outcome = technical_failure_evaluation(
                    "Cross-task owner evaluation failed: " + sanitize(str(exc))[:800],
                    [f"The approved requested change is applied to {request.get('target_path', '')}.",
                     f"The updated {request.get('target_path', '')} is read back successfully after the edit."],
                )
        if self.clock() >= deadline:
            self._timeout(oid)
            return
        accepted = outcome.get("status") == "accepted"
        status = "completed" if accepted else "blocked"
        reason = str(outcome.get("summary") or "The owner change did not pass independent evaluation.")
        if accepted and owner is not None:
            owner_runtime_task = self.store.get_task(request["owner_runtime_task_id"])
            owner_planned_task = self._cross_task_change_task(plan, owner, request)
            if owner_runtime_task.get("workspace"):
                self.project_state.accept_task_update(
                    oid, plan, owner_planned_task, owner_runtime_task,
                    request.get("owner_agent_id"), mark_task_accepted=False,
                )
        updated = self.store.update_cross_task_modification_request(
            request["id"], expected_statuses=["owner_evaluating"], status=status,
            owner_evaluation=outcome,
            **({"approval_source": request.get("approval_source", "")} if accepted else {}),
        )
        if updated:
            self._cross_task_event(oid, "cross_task_modification.owner_action_evaluated", updated,
                                   status=status, owner_evaluation=outcome)
            self._cross_task_event(oid, "cross_task_modification.fulfilled" if accepted
                                   else "cross_task_modification.failed", updated,
                                   status=status, message=reason)
            self._cross_task_event(
                oid, "cross_task_modification.completed" if accepted
                else "cross_task_modification.evaluation_rejected", updated,
                status=status, owner_evaluation=outcome,
                message=reason,
            )
            self._resume_cross_task_requester(oid, updated, status, reason)

    def _advance_cross_task_requests(self, oid: str, deadline: float,
                                     operational_prompt: str) -> None:
        """Advance one durable handoff state per graph iteration."""
        run = self.store.get_orchestration(oid)
        if run["status"] != "Running":
            return
        requests = self.store.list_cross_task_modification_requests(oid)
        for request in requests:
            status = request.get("status")
            if status in {"approved_once", "approved_intent", "auto_approved"}:
                target_key = owned_path_key(request["target_path"])
                if any(other["id"] != request["id"]
                       and owned_path_key(other["target_path"]) == target_key
                       and other.get("status") in {"owner_selecting", "owner_running", "owner_evaluating"}
                       for other in requests):
                    continue
                graph_nodes = self.store.get_execution_graph(oid)["nodes"]
                owner_node = next((item for item in graph_nodes
                                   if item.get("plan_task_id") == request.get("target_owner_plan_task_id")), None)
                if owner_node is not None and owner_node.get("state") not in {
                        "success", "failed", "blocked", "cancelled", "skipped", "superseded"}:
                    # Let the original owner finish before its scoped change
                    # agent runs, so later owner writes cannot overwrite it.
                    continue
                with self.lock:
                    current_requests = self.store.list_cross_task_modification_requests(oid)
                    if any(other["id"] != request["id"]
                           and owned_path_key(other["target_path"]) == target_key
                           and other.get("status") in {
                               "owner_selecting", "owner_running", "owner_evaluating"}
                           for other in current_requests):
                        continue
                    claimed = self.store.update_cross_task_modification_request(
                        request["id"], expected_statuses=[status], status="owner_selecting",
                    )
                if claimed:
                    self._dispatch_cross_task_owner_change(
                        oid, claimed, deadline, operational_prompt,
                    )
                    return
            elif status == "owner_running":
                try:
                    task = self.store.get_task(request["owner_runtime_task_id"])
                except (KeyError, TypeError):
                    task = None
                if task and task["status"] in {"Success", "Failed", "Cancelled"}:
                    if task["status"] == "Success":
                        self._cross_task_event(oid, "cross_task_modification.owner_action_completed",
                                               request, status="Running",
                                               owner_runtime_task_id=request["owner_runtime_task_id"])
                        with self.lock:
                            evaluating = self.store.update_cross_task_modification_request(
                                request["id"], expected_statuses=["owner_running"],
                                status="owner_evaluating",
                            )
                        if evaluating:
                            self._evaluate_cross_task_owner_change(oid, evaluating, deadline)
                    else:
                        reason = "The owner change task ended with status " + task["status"] + ". " + str(task.get("error") or "")
                        blocked = self.store.block_cross_task_modification_request(
                            request["id"], status="blocked", reason=sanitize(reason)[:1000],
                            expected_statuses=["owner_running"],
                        )
                        if blocked:
                            self._cross_task_event(oid, "cross_task_modification.failed", blocked,
                                                   status="blocked", message=sanitize(reason)[:1000])
                            self._cross_task_event(oid, "cross_task_modification.owner_failed", blocked,
                                                   status="blocked", message=sanitize(reason)[:1000])
                            self._resume_cross_task_requester(oid, blocked, "blocked", reason)
                    return
            elif status == "owner_evaluating":
                self._evaluate_cross_task_owner_change(oid, request, deadline)
                return
            elif status in {"denied", "blocked", "cycle_detected"}:
                self._resume_cross_task_requester(
                    oid, request, status,
                    request.get("human_resolution") or status.replace("_", " "),
                )
                return

    def _run_graph(self, oid: str, running: dict, deadline: float,
                   operational_prompt: str = "") -> None:
        operational_prompt = (
            str(operational_prompt).strip()
            or str((running.get("plan") or {}).get("goal") or "").strip()
        )
        if not operational_prompt:
            raise ValueError("Execution graph has no Task Analyst operational prompt.")
        while True:
            if self.clock() >= deadline:
                self._timeout(oid)
                return

            self._resolve_pending_cross_task_intents(oid)
            self._advance_cross_task_requests(oid, deadline, operational_prompt)

            selection_target = None
            evaluation_target = None
            recovery_target = None
            completed_graph = None
            with self.lock:
                run = self.store.get_orchestration(oid)
                if run["status"] != "Running":
                    return
                persisted = self.store.get_execution_graph(oid)
                plan = run.get("effective_plan") or run["plan"]
                assignments_by_task = worker_assignment_map(plan)
                graph = ExecutionGraph(plan, persisted["nodes"])
                changed = False

                for node in graph.active_nodes():
                    if node["state"] == "evaluating":
                        continue
                    runtime_task = self.store.get_task(node["runtime_task_id"])
                    previous = node["state"]
                    if graph.apply_runtime_status(
                            node["plan_task_id"], runtime_task["status"],
                            result=runtime_task.get("result"), error=runtime_task.get("error"),
                            timestamp=utcnow()):
                        changed = True
                    if node.get("delegation_id"):
                        self._snapshot_delegation(node["delegation_id"], runtime_task)
                    current = graph.node(node["plan_task_id"])["state"]
                    self.store.update_execution_attempt(
                        oid, node["plan_task_id"], int(node.get("attempt", 0)),
                        status=current,
                    )
                    if current != previous:
                        event_type = {
                            "waiting_for_approval": "freya.task.waiting_for_approval",
                            "failed": "freya.task.failed",
                            "cancelled": "freya.task.failed",
                        }.get(current)
                        if event_type:
                            event_message = f"Planned task entered {current}."
                            event_error = ""
                            if current == "failed" and runtime_task.get("error"):
                                event_error = str(runtime_task["error"])
                                event_message += " Cause: " + event_error
                            self.store.add_orchestration_event(oid, {
                                "event_type": event_type,
                                "status": runtime_task["status"],
                                "task_id": node["plan_task_id"],
                                "agent_id": node.get("selected_agent_id"),
                                "runtime_task_id": node.get("runtime_task_id"),
                                "message": event_message,
                                "error": event_error,
                            })
                        if current in {"evaluating", "failed", "cancelled"}:
                            runtime_config = runtime_task.get("config", {})
                            runtime_context = runtime_config.get("runtime_context", {})
                            active_context = runtime_context.get("active_task_context", {})
                            self.store.add_orchestration_event(oid, {
                                "event_type": "worker.task_completed",
                                "status": runtime_task.get("status"),
                                "worker_id": worker_id_for_task(plan, node["plan_task_id"]),
                                "agent_id": node.get("selected_agent_id"),
                                "task_id": node["plan_task_id"],
                                "orchestration_id": oid,
                                "execution_strategy": plan.get("execution_strategy"),
                                "active_tools": list(active_context.get("active_tools") or []),
                                "active_capabilities": list(active_context.get("active_capabilities") or []),
                                "attempt": int(node.get("attempt", 0)),
                                "evaluation_status": "pending" if current == "evaluating" else "not_applicable",
                                "message": (
                                    "Runtime finished this task; semantic evaluation remains pending."
                                    if current == "evaluating" else
                                    "Runtime task ended without a semantic evaluation."
                                ),
                            })

                transitions = graph.refresh_dependencies(utcnow())
                if transitions:
                    changed = True
                    self._record_graph_transitions(oid, transitions)
                if changed:
                    self.store.save_execution_graph(oid, graph.serialize())

                if graph.summary()["complete"]:
                    completed_graph = graph

                for node in graph.serialize():
                    if node["state"] == "recovery_pending" and not node.get("recovery_action_id"):
                        recovery_target = node
                        break

                if recovery_target is None:
                    for node in graph.serialize():
                        if node["state"] == "evaluating" and not node.get("evaluation_id"):
                            evaluation_target = node
                            break


                if recovery_target is None and evaluation_target is None:
                    reserved_workers = occupied_workers(plan, graph.serialize())
                    for task in graph.ready_tasks():
                        worker_id = worker_id_for_task(plan, task["id"])
                        if (not graph.node(task["id"]).get("selection_id")
                                and (worker_id is None or worker_id not in reserved_workers)):
                            selection_target = task
                            break

            if completed_graph is not None:
                self._complete_or_integrate(oid, completed_graph, deadline)
                return

            if evaluation_target is not None:
                self._evaluate_graph_node(oid, plan, evaluation_target, deadline)
                continue

            if recovery_target is not None:
                self._recover_graph_node(oid, plan, recovery_target, deadline)
                continue

            if selection_target is not None:
                selected = self._select_graph_task(oid, selection_target, run)
                if selected is None:
                    return
                with self.lock:
                    if self.store.get_orchestration(oid)["status"] != "Running":
                        return
                    graph = ExecutionGraph(
                        plan, self.store.get_execution_graph(oid)["nodes"],
                    )
                    node = graph.node(selection_target["id"])
                    if node["state"] != "ready" or node.get("selection_id"):
                        continue
                    if selected.get("error"):
                        graph.mark_failed(selection_target["id"], selected["error"], utcnow())
                        self.store.add_orchestration_event(oid, {
                            "event_type": "freya.task.failed", "status": "Failed",
                            "task_id": selection_target["id"], "message": selected["error"],
                        })
                    else:
                        selection = selected["selection"]
                        if selection["selected_agent_id"] is None:
                            node["selection_id"] = selected["selection_id"]
                            failure_reason = selection.get("failure_reason") or (
                                "No eligible agent is available for this planned task."
                            )
                            graph.mark_failed(
                                selection_target["id"], failure_reason, utcnow(),
                            )
                            self.store.add_orchestration_event(oid, {
                                "event_type": "freya.task.failed", "status": "Failed",
                                "task_id": selection_target["id"],
                                "message": failure_reason,
                                "error": failure_reason,
                            })
                        else:
                            graph.mark_selected(
                                selection_target["id"], selection["selected_agent_id"],
                                selected["selection_id"], utcnow(),
                            )
                    self.store.save_execution_graph(oid, graph.serialize())

            dispatched = 0
            with self.lock:
                if self.store.get_orchestration(oid)["status"] != "Running":
                    return
                graph = ExecutionGraph(plan, self.store.get_execution_graph(oid)["nodes"])
                active_nodes = graph.active_nodes()
                cross_waiter_ids = {
                    item.get("requester_runtime_task_id")
                    for item in self.store.list_cross_task_modification_requests(oid)
                }
                active_node_ids = {
                    node.get("runtime_task_id") for node in active_nodes
                    if node.get("state") in {"running", "evaluating"}
                }
                active_runtime = [item for item in self.store.list_tasks(limit=10000)
                                  if item.get("status") in ACTIVE_DELEGATED_TASK_STATUSES
                                  and item.get("id") not in cross_waiter_ids]
                active_runtime_ids = {item.get("id") for item in active_runtime}
                slots = max(0, int(self.config["max_parallel_tasks"])
                            - len(active_node_ids | active_runtime_ids))
                active_agents = {
                    node.get("selected_agent_id") for node in active_nodes
                    if node.get("state") in {"running", "evaluating"}
                }
                active_agents.update(item.get("agent_id") for item in active_runtime
                                     if item.get("agent_id"))
                active_workers = occupied_workers(
                    plan, active_nodes, include_selected_ready=False)
                for task in graph.ready_tasks():
                    if slots <= 0:
                        break
                    node = graph.node(task["id"])
                    agent_id = node.get("selected_agent_id")
                    if not node.get("selection_id") or not agent_id:
                        continue
                    worker_id = worker_id_for_task(plan, task["id"])
                    if worker_id is not None and worker_id in active_workers:
                        graph.set_waiting_reason(
                            task["id"], "Assigned worker is executing another task.", utcnow(),
                        )
                        continue
                    try:
                        agent = self.store.get_agent(agent_id)
                    except KeyError:
                        graph.mark_failed(task["id"], "Selected agent no longer exists.", utcnow())
                        self.store.add_orchestration_event(oid, {
                            "event_type": "freya.task.failed", "status": "Failed",
                            "task_id": task["id"], "agent_id": agent_id,
                            "message": "Selected agent no longer exists.",
                        })
                        continue
                    if agent.get("enabled") is not True:
                        message = "Selected agent became disabled after selection."
                        graph.mark_failed(task["id"], message, utcnow())
                        self.store.add_orchestration_event(oid, {
                            "event_type": "freya.task.failed", "status": "Failed",
                            "task_id": task["id"], "agent_id": agent_id,
                            "message": message,
                        })
                        continue
                    if agent.get("status") in {"Paused", "Offline"}:
                        graph.set_waiting_reason(
                            task["id"], f"Selected agent is {agent['status']}.", utcnow(),
                        )
                        continue
                    if agent_id in active_agents:
                        graph.set_waiting_reason(
                            task["id"], "Selected agent is executing another task.", utcnow(),
                        )
                        continue
                    runtime_context, project_context = self._project_context_for_dispatch(
                        oid, plan, task, run,
                    )
                    worker_assignment = assignments_by_task.get(task["id"])
                    active_tools = list(agent.get("config", {}).get(
                        "active_task_tools", agent.get("tools", []),
                    ))
                    active_capabilities = list(agent.get("config", {}).get(
                        "active_task_capabilities", task.get("required_capabilities", []),
                    ))
                    worker_context = self._worker_task_context(
                        plan, task, worker_assignment, graph.serialize(), agent,
                        active_tools=active_tools,
                        active_capabilities=active_capabilities,
                    )
                    execution_prompt = self._execution_prompt(
                        operational_prompt, task, node.get("attempt_prompt") or "",
                        project_context, self._plan_context_for_dispatch(oid, plan, task),
                        worker_context,
                    )
                    if worker_context is not None:
                        runtime_context = dict(runtime_context or {})
                        runtime_context["active_task_context"] = worker_context
                    try:
                        runtime_task = self._submit_runtime_task(
                            agent_id, execution_prompt,
                            self._workspace_for_run(run), runtime_context,
                        )
                    except ValueError as exc:
                        refreshed = self.store.get_agent(agent_id)
                        if refreshed.get("status") in {"Paused", "Offline"}:
                            graph.set_waiting_reason(
                                task["id"], f"Selected agent is {refreshed['status']}.", utcnow(),
                            )
                        else:
                            graph.mark_failed(task["id"], str(exc), utcnow())
                            self.store.add_orchestration_event(oid, {
                                "event_type": "freya.task.failed", "status": "Failed",
                                "task_id": task["id"], "agent_id": agent_id,
                                "message": "Runtime submission failed: " + str(exc),
                            })
                        continue
                    except Exception as exc:
                        graph.mark_failed(task["id"], str(exc), utcnow())
                        self.store.add_orchestration_event(oid, {
                            "event_type": "freya.task.failed", "status": "Failed",
                            "task_id": task["id"], "agent_id": agent_id,
                            "message": "Runtime submission failed: " + str(exc),
                        })
                        continue
                    try:
                        delegation_id = self.store.add_delegation(
                            oid, agent_id, execution_prompt, runtime_task["id"],
                        )
                        if delegation_id is None:
                            self.runtime.cancel(runtime_task["id"])
                            return
                        graph.mark_running(
                            task["id"], runtime_task["id"], delegation_id, utcnow(),
                        )
                        # Persist the dispatch before another selection can run.
                        # If any post-submit step fails, cancel the created task
                        # and fail closed instead of leaving a ready duplicate.
                        self.store.save_execution_graph(oid, graph.serialize())
                        attempt_record = self.store.record_execution_attempt(
                            oid, task["id"], selected_agent_id=agent_id,
                            selection_id=node["selection_id"],
                            runtime_task_id=runtime_task["id"], delegation_id=delegation_id,
                            attempt=int(graph.node(task["id"])["attempt"]),
                            prompt=execution_prompt,
                            recovery_action_id=graph.node(task["id"]).get("recovery_action_id"),
                        )
                        if attempt_record is None:
                            raise RuntimeError("Execution attempt persistence lost its precondition.")
                    except Exception:
                        try:
                            self.runtime.cancel(runtime_task["id"])
                        except (KeyError, ValueError):
                            pass
                        raise
                    active_agents.add(agent_id)
                    if worker_id is not None:
                        active_workers.add(worker_id)
                    slots -= 1
                    dispatched += 1
                    self.store.add_orchestration_event(oid, {
                        "event_type": "worker.task_started", "status": runtime_task["status"],
                        "worker_id": worker_id,
                        "agent_id": agent_id,
                        "task_id": task["id"],
                        "orchestration_id": oid,
                        "execution_strategy": plan.get("execution_strategy"),
                        "active_tools": active_tools,
                        "active_capabilities": active_capabilities,
                        "attempt": int(graph.node(task["id"])["attempt"]),
                        "message": "The assigned Worker started its active task with task-scoped tools.",
                    })
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.task.dispatched", "status": runtime_task["status"],
                        "task_id": task["id"], "runtime_task_id": runtime_task["id"],
                        "agent_id": agent_id, "selection_id": node["selection_id"],
                        "worker_id": worker_id,
                        "message": "Ready planned task was dispatched exactly once.",
                    })
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.delegated", "status": runtime_task["status"],
                        "task_id": runtime_task["id"], "planned_task_id": task["id"],
                        "agent_id": agent_id, "selection_id": node["selection_id"],
                        "message": "Delegated objective to agent.",
                    })
                self.store.save_execution_graph(oid, graph.serialize())

            if self.clock() >= deadline:
                self._timeout(oid)
                return
            # Interleave selection and dispatch. A newly dispatched task is now
            # visible to the next AgentSelector workload calculation, so fill
            # remaining slots before yielding to the polling wait.
            if dispatched and slots > 0:
                continue
            self.wait(min(.2, max(0, deadline - self.clock())))

    def _run(self, oid, answers: dict[str, str] | None = None):
        from .llm_trace import bind_llm_trace
        with bind_llm_trace(lambda event: self.store.add_orchestration_event(oid, event)):
            return self._run_impl(oid, answers)

    def _run_impl(self, oid, answers: dict[str, str] | None = None):
        # The injected legacy decision callback retains its historical test API.
        if self.decide is not None:
            return self._run_legacy(oid)
        if not hasattr(self.planner, "create_plan_for_spec"):
            return self._run_with_legacy_analysis(oid)
        started = self.clock()
        deadline = started + float(self.config["max_wallclock_seconds"])
        answers = answers or {}
        with self.lock:
            run = self.store.get_orchestration(oid)
            reuse_ready = bool(run.get("task_spec") and
                               run["task_spec"].get("status") == "READY_FOR_PLANNING")
            if run["status"] == "Queued":
                target = "Planning" if reuse_ready else "Analyzing"
                run = self.store.transition_orchestration(oid, ("Queued",), target)
            elif run["status"] != "Analyzing":
                return
        if run is None:
            return
        planning_metrics: dict = {}
        try:
            if reuse_ready:
                spec = validate_task_spec(run["task_spec"])
                analysis_metrics = {"mode": "user_revision", "model_calls": 0}
                with self.lock:
                    if self.store.get_orchestration(oid)["status"] != "Planning":
                        return
                    self.store.add_orchestration_event(oid, {
                        **self._task_analyst_identity(),
                        "event_type": "task_analysis.ready", "status": "Planning",
                        "spec_version": spec["version"],
                        "readiness_reason": spec["readiness_reason"],
                        "message": "Revised Task Spec is ready for replanning.",
                    })
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.planning.started", "status": "Planning",
                        "spec_version": spec["version"],
                        "message": "Planner is using the revised Task Spec.",
                    })
            else:
                spec, analysis_metrics = self._analyze_task_spec(oid, run, answers)
                with self.lock:
                    if self.store.get_orchestration(oid)["status"] != "Analyzing":
                        return
                    updated = self.store.save_task_spec(oid, spec)
                    if updated is None:
                        return
                    self.store.add_orchestration_event(oid, {
                        **self._task_analyst_identity(),
                        "event_type": "task_analysis.updated", "status": updated["status"],
                        "analysis_version": 1, "spec_version": spec["version"],
                        "task_spec": spec, "metrics": analysis_metrics,
                        "fields_resolved": list(spec["user_decisions"]),
                        "assumptions": spec["assumptions"],
                        "readiness_reason": spec["readiness_reason"],
                        "message": "Task Analyst persisted the canonical Task Spec.",
                    })
                    if spec["status"] == "NEEDS_CLARIFICATION":
                        self.store.add_orchestration_event(oid, {
                            **self._task_analyst_identity(),
                            "event_type": "task_analysis.clarification_required",
                            "status": "NeedsClarification", "spec_version": spec["version"],
                            "questions": spec["clarification_questions"],
                            "message": "Freya needs user clarification before planning.",
                        })
                        return
                    self.store.add_orchestration_event(oid, {
                        **self._task_analyst_identity(),
                        "event_type": "task_analysis.ready", "status": "Planning",
                        "spec_version": spec["version"], "readiness_reason": spec["readiness_reason"],
                        "message": "Task Spec is ready for planning.",
                    })
                    self.store.add_orchestration_event(oid, {
                        "event_type": "freya.planning.started", "status": "Planning",
                        "spec_version": spec["version"],
                        "message": "Planner is creating work from the canonical Task Spec.",
                    })
            context = self._planning_context()
            resource_summary = self._record_planning_resources(oid, context)
            operational_prompt = render_task_spec(spec)
            with self.planner_lock:
                try:
                    plan = self.planner.create_plan_for_spec(spec, context)
                finally:
                    planning_metrics = dict(self.planner.metrics)
                    self._record_plan_compiler_activity(oid, planning_metrics)
            planning_metrics["task_analysis"] = {
                key: value for key, value in analysis_metrics.items()
                if key in {"mode", "model_calls", "prompt_tokens", "generated_tokens",
                           "total_tokens", "duration_seconds"}}
            planning_metrics.update(resource_summary)
            if len(plan["tasks"]) > int(self.config["max_delegated_tasks"]):
                raise ValueError(
                    f"Plan contains {len(plan['tasks'])} tasks but max_delegated_tasks is "
                    f"{self.config['max_delegated_tasks']}.")
        except Exception as exc:
            with self.lock:
                current = self.store.get_orchestration(oid)
                expected_spec = locals().get("spec", run.get("task_spec"))
                if current.get("task_spec") != expected_spec:
                    return
            self._fail_planning(oid, exc, planning_metrics)
            return

        try:
            with self.lock:
                planned = self.store.save_orchestration_plan(
                    oid, plan, PLAN_SCHEMA_VERSION, planning_metrics,
                    expected_task_spec=spec,
                )
                if planned is None:
                    return
                self._record_planner_normalization(oid, planning_metrics)
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.plan.created", "status": "Planned",
                    "message": "Freya created and saved the structured plan.",
                    "goal": plan["goal"], "complexity": plan["complexity"],
                    "task_count": len(plan["tasks"]),
                    "task_ids": [task["id"] for task in plan["tasks"]],
                    "plan_schema_version": PLAN_SCHEMA_VERSION,
                    "planning_metrics": planning_metrics,
                    "selected_capabilities": sorted({
                        capability for task in plan["tasks"]
                        for capability in task.get("required_capabilities", [])}),
                    "selected_skills": sorted({
                        skill for task in plan["tasks"]
                        for skill in task.get("preferred_skills", [])}),
                    "selected_tools": sorted({
                        tool for task in plan["tasks"]
                        for tool in task.get("required_tools", [])}),
                    "resource_catalog_version": resource_summary.get("resource_catalog_version"),
                })
                workspace = self._workspace_for_run(planned)
                if workspace:
                    self.project_state.initialize(oid, plan, workspace)
                graph = ExecutionGraph(plan)
                self.store.initialize_execution_graph(oid, graph.serialize())
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.graph.initialized", "status": "Planned",
                    "message": "Freya initialized the durable execution graph.",
                    "summary": graph.summary(),
                })
                self._record_graph_transitions(oid, [
                    {"task_id": task["id"], "from": "pending", "to": "ready"}
                    for task in graph.ready_tasks()
                ])
                running = self.store.transition_orchestration(oid, ("Planned",), "Running")
                if running is None:
                    return
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.analyzing", "status": "Running",
                    "message": "Freya is scheduling ready tasks from the execution graph.",
                })
            self._run_graph(oid, running, deadline, operational_prompt)
        except Exception as exc:
            self._abort_graph(oid, str(exc))

    def _run_with_legacy_analysis(self, oid):
        # Explicitly injected decision functions retain the pre-4.3 test and
        # extension contract. The production path is the durable DAG scheduler.
        if self.decide is not None:
            return self._run_legacy(oid)
        started = self.clock()
        deadline = started + float(self.config["max_wallclock_seconds"])
        with self.lock:
            planning = self.store.transition_orchestration(oid, ("Queued",), "Planning")
            if planning is None:
                return
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.planning.started", "status": "Planning",
                "message": "Freya is creating a structured plan.",
            })
        planning_metrics: dict = {}
        try:
            analysis, analysis_metrics = self._analyze_prompt(oid, planning["prompt"])
            # A Task Analyst call may be in flight when cancellation arrives;
            # never start a planner call after the run has left Planning.
            with self.lock:
                if self.store.get_orchestration(oid)["status"] != "Planning":
                    return
            context = self._planning_context()
            resource_summary = self._record_planning_resources(oid, context)
            analysis = self._require_ready_analysis(oid, analysis)
            context["task_analysis"] = analysis
            operational_prompt = str(analysis.get("operational_prompt") or "").strip()
            if not operational_prompt:
                raise ValueError("Task Analyst operational prompt is empty.")
            with self.planner_lock:
                try:
                    plan = self.planner.create_plan(operational_prompt, context)
                finally:
                    planning_metrics = dict(self.planner.metrics)
            planning_metrics["task_analysis"] = {
                key: value for key, value in analysis_metrics.items()
                if key in {"mode", "model_calls", "prompt_tokens", "generated_tokens",
                           "total_tokens", "duration_seconds"}
            }
            planning_metrics.update(resource_summary)
            if len(plan["tasks"]) > int(self.config["max_delegated_tasks"]):
                raise ValueError(
                    f"Plan contains {len(plan['tasks'])} tasks but max_delegated_tasks is "
                    f"{self.config['max_delegated_tasks']}.",
                )
        except Exception as exc:
            self._fail_planning(oid, exc, planning_metrics)
            return

        try:
            with self.lock:
                planned = self.store.save_orchestration_plan(
                    oid, plan, PLAN_SCHEMA_VERSION, planning_metrics,
                )
                if planned is None:
                    return
                self._record_planner_normalization(oid, planning_metrics)
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.plan.created", "status": "Planned",
                    "message": "Freya created and saved the structured plan.",
                    "goal": plan["goal"], "complexity": plan["complexity"],
                    "task_count": len(plan["tasks"]),
                    "task_ids": [task["id"] for task in plan["tasks"]],
                    "plan_schema_version": PLAN_SCHEMA_VERSION,
                    "planning_metrics": planning_metrics,
                    "selected_capabilities": sorted({
                        capability for task in plan["tasks"]
                        for capability in task.get("required_capabilities", [])}),
                    "selected_skills": sorted({
                        skill for task in plan["tasks"]
                        for skill in task.get("preferred_skills", [])}),
                    "selected_tools": sorted({
                        tool for task in plan["tasks"]
                        for tool in task.get("required_tools", [])}),
                    "resource_catalog_version": resource_summary.get("resource_catalog_version"),
                })
                workspace = self._workspace_for_run(planned)
                if workspace:
                    self.project_state.initialize(oid, plan, workspace)
                graph = ExecutionGraph(plan)
                self.store.initialize_execution_graph(oid, graph.serialize())
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.graph.initialized", "status": "Planned",
                    "message": "Freya initialized the durable execution graph.",
                    "summary": graph.summary(),
                })
                self._record_graph_transitions(oid, [
                    {"task_id": task["id"], "from": "pending", "to": "ready"}
                    for task in graph.ready_tasks()
                ])
                running = self.store.transition_orchestration(oid, ("Planned",), "Running")
                if running is None:
                    return
                self.store.add_orchestration_event(oid, {
                    "event_type": "freya.analyzing", "status": "Running",
                    "message": "Freya is scheduling ready tasks from the execution graph.",
                })
            self._run_graph(oid, running, deadline, operational_prompt)
        except Exception as exc:
            self._abort_graph(oid, str(exc))

    def _run_legacy(self, oid):
        started = self.clock()
        deadline = started + float(self.config["max_wallclock_seconds"])
        results: list[dict] = []
        delegated = 0

        with self.lock:
            planning = self.store.transition_orchestration(oid, ("Queued",), "Planning")
            if planning is None:
                return
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.planning.started", "status": "Planning",
                "message": "Freya is creating a structured plan.",
            })
        planning_metrics: dict = {}
        try:
            analysis, analysis_metrics = self._analyze_prompt(oid, planning["prompt"])
            analysis = self._require_ready_analysis(oid, analysis)
            operational_prompt = str(analysis.get("operational_prompt") or "").strip()
            agents = self.store.list_agents()
            context = self._planning_context(agents)
            resource_summary = self._record_planning_resources(oid, context)
            context["task_analysis"] = analysis
            with self.planner_lock:
                try:
                    plan = self.planner.create_plan(operational_prompt, context)
                finally:
                    planning_metrics = dict(self.planner.metrics)
            planning_metrics["task_analysis"] = {
                key: value for key, value in analysis_metrics.items()
                if key in {"mode", "model_calls", "prompt_tokens", "generated_tokens",
                           "total_tokens", "duration_seconds"}
            }
            planning_metrics.update(resource_summary)
        except Exception as exc:
            self._fail_planning(oid, exc, planning_metrics)
            return

        with self.lock:
            planned = self.store.save_orchestration_plan(
                oid, plan, PLAN_SCHEMA_VERSION, planning_metrics,
            )
            if planned is None:
                return
            self._record_planner_normalization(oid, planning_metrics)
            workspace = self._workspace_for_run(planned)
            if workspace:
                self.project_state.initialize(oid, plan, workspace)
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.plan.created", "status": "Planned",
                "message": "Freya created and saved the structured plan.",
                "goal": plan["goal"], "complexity": plan["complexity"],
                "task_count": len(plan["tasks"]),
                "task_ids": [task["id"] for task in plan["tasks"]],
                "plan_schema_version": PLAN_SCHEMA_VERSION,
                "planning_metrics": planning_metrics,
                "selected_capabilities": sorted({
                    capability for task in plan["tasks"]
                    for capability in task.get("required_capabilities", [])}),
                "selected_skills": sorted({
                    skill for task in plan["tasks"]
                    for skill in task.get("preferred_skills", [])}),
                "selected_tools": sorted({
                    tool for task in plan["tasks"]
                    for tool in task.get("required_tools", [])}),
                "resource_catalog_version": resource_summary.get("resource_catalog_version"),
            })
            running = self.store.transition_orchestration(oid, ("Planned",), "Running")
            if running is None:
                return
            self.store.add_orchestration_event(oid, {
                "event_type": "freya.analyzing", "status": "Running",
                "message": "Freya is selecting how to handle the planned work.",
            })

        try:
            for _round in range(int(self.config["max_rounds"])):
                if self.clock() >= deadline:
                    self._timeout(oid)
                    return
                if self.decide:
                    decision = self._decision(operational_prompt, self.store.list_agents(), results)
                else:
                    # Compatibility path for explicitly injected legacy decision hooks.
                    selected_task = self._select_planned_task(
                        oid, running["plan"]["tasks"][0], running,
                    )
                    if selected_task is None:
                        return
                    decision = {"action": "delegate", "tasks": [selected_task]}
                action = decision.get("action") if isinstance(decision, dict) else None
                if action == "respond":
                    with self.lock:
                        message = str(decision.get("message", ""))
                        completed = self.store.transition_orchestration(
                            oid, ("Running",), "Success", response=message,
                            legacy_without_graph=True,
                        )
                        if completed is not None:
                            self.store.add_orchestration_event(oid, {
                                "event_type": "freya.completed", "status": "Success", "message": message,
                            })
                    return
                if action not in {"delegate", "continue"}:
                    raise ValueError("Freya returned an invalid action.")
                tasks = decision.get("tasks", [])
                if not isinstance(tasks, list) or delegated + len(tasks) > self.config["max_delegated_tasks"]:
                    raise ValueError("Delegation limit reached.")
                for item in tasks:
                    with self.lock:
                        if self.clock() >= deadline:
                            self._timeout(oid)
                            return
                        if self.store.get_orchestration(oid)["status"] != "Running":
                            return
                        agent_id = item.get("agent_id")
                        objective = str(item.get("objective", "")).strip()
                        agent = self.store.get_agent(agent_id)
                        if agent.get("enabled") is not True:
                            raise ValueError("Selected agent is disabled or unavailable.")
                        plan_snapshot = running.get("effective_plan") or running.get("plan") or {}
                        planned_context_task = next((candidate for candidate in plan_snapshot.get("tasks", [])
                                                     if candidate.get("id") == item.get("planned_task_id")), item)
                        runtime_context, project_context = self._project_context_for_dispatch(
                            oid, plan_snapshot, planned_context_task, running,
                        )
                        execution_prompt = self._execution_prompt(
                            operational_prompt, item, objective, project_context,
                            self._plan_context_for_dispatch(oid, plan_snapshot, planned_context_task),
                        )
                        task = self._submit_runtime_task(
                            agent_id, execution_prompt, self._workspace_for_run(running), runtime_context,
                        )
                        delegation_id = self.store.add_delegation(
                            oid, agent_id, execution_prompt, task["id"],
                        )
                        if delegation_id is None:
                            self.runtime.cancel(task["id"])
                            return
                        delegated += 1
                        self.store.add_orchestration_event(oid, {
                            "event_type": "freya.delegated", "status": "Queued", "agent_id": agent_id,
                            "task_id": task["id"], "planned_task_id": item.get("planned_task_id"),
                            "selection_id": item.get("selection_id"),
                            "message": "Delegated objective to agent.",
                        })
                        results.append({"delegation_id": delegation_id, "task_id": task["id"],
                                        "agent_id": agent_id})

                while True:
                    if self.store.get_orchestration(oid)["status"] == "Cancelled":
                        return
                    active = [item for item in results
                              if self.store.get_task(item["task_id"])["status"] in
                              ACTIVE_DELEGATED_TASK_STATUSES]
                    if not active:
                        break
                    if self.clock() >= deadline:
                        self._timeout(oid)
                        return
                    self.wait(min(.2, max(0, deadline - self.clock())))

                with self.lock:
                    if self.store.get_orchestration(oid)["status"] != "Running":
                        return
                    child_tasks = []
                    for item in results:
                        child = self.store.get_task(item["task_id"])
                        self._snapshot_delegation(item["delegation_id"], child)
                        item["status"] = child["status"]
                        item["result"] = child.get("result")
                        child_tasks.append(child)
                    failed_ids = [task["id"] for task in child_tasks if task["status"] == "Failed"]
                    cancelled_ids = [task["id"] for task in child_tasks if task["status"] == "Cancelled"]
                    if failed_ids or cancelled_ids:
                        detail = ("Delegated task failure: " + ", ".join(failed_ids) if failed_ids else
                                  "Delegated task cancellation: " + ", ".join(cancelled_ids))
                        self._fail_running(oid, detail)
                        return
                    if any(task["status"] != "Success" for task in child_tasks):
                        raise RuntimeError("A delegated task ended in an unknown non-success state.")
                    if action == "delegate" and results:
                        message = "Freya completed the delegated work.\n\n" + "\n\n".join(
                            str(item.get("result") or item.get("status")) for item in results
                        )
                        completed = self.store.transition_orchestration(
                            oid, ("Running",), "Success", response=message,
                            legacy_without_graph=True,
                        )
                        if completed is not None:
                            self.store.add_orchestration_event(oid, {
                                "event_type": "freya.completed", "status": "Success",
                                "message": "Freya integrated successful agent results.",
                            })
                        return
            raise RuntimeError("Maximum orchestration rounds reached.")
        except Exception as exc:
            self._fail_running(oid, str(exc))
