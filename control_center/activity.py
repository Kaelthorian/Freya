"""Backend-derived orchestration activity and wall-clock performance read model."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from .security import sanitize


_DETAIL_FIELDS = (
    "phase", "actor_type", "actor_name", "actor_role", "agent_id", "agent_name",
    "task_id", "runtime_task_id", "plan_task_id", "planned_task_id", "attempt",
    "model", "mode", "model_calls", "prompt_tokens", "generated_tokens", "total_tokens",
    "tokens_per_second", "first_token_latency", "first_token_latency_seconds",
    "streaming", "fallback_used", "fallback_reason", "repair_attempted",
    "initial_validation_error", "repair_validation_error", "error", "error_path",
    "error_type", "expected", "received", "validation_message", "raw_response_excerpt",
    "normalization_attempted", "normalization_changes", "resource_catalog_version",
    "available_capability_ids", "available_tool_ids", "available_skill_ids",
    "selected_capabilities", "selected_tools", "selected_skills", "required_capabilities",
    "required_tools", "preferred_skills", "semantic_need", "integration_status",
    "evaluation_status", "duration_seconds", "model_call_details", "semantic_compiler",
    "task_ids", "global_criteria",
)


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds") if value else None


def _seconds(start: Any, end: Any) -> float | None:
    first, last = _date(start), _date(end)
    if first is None or last is None:
        return None
    return round(max(0.0, (last - first).total_seconds()), 4)


def _union_seconds(intervals: Iterable[tuple[datetime, datetime]]) -> float:
    ordered = sorted((left, right) for left, right in intervals if right > left)
    if not ordered:
        return 0.0
    total = 0.0
    left, right = ordered[0]
    for next_left, next_right in ordered[1:]:
        if next_left <= right:
            right = max(right, next_right)
        else:
            total += (right - left).total_seconds()
            left, right = next_left, next_right
    total += (right - left).total_seconds()
    return round(max(0.0, total), 4)


def _subtract_covered_seconds(intervals: list[tuple[datetime, datetime]],
                              removed: list[tuple[datetime, datetime]]) -> float:
    """Return the union of intervals after excluding any overlapping wait spans."""
    pieces: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        remaining = [(start, end)]
        for cut_start, cut_end in removed:
            next_remaining = []
            for left, right in remaining:
                if cut_end <= left or cut_start >= right:
                    next_remaining.append((left, right))
                    continue
                if cut_start > left:
                    next_remaining.append((left, cut_start))
                if cut_end < right:
                    next_remaining.append((cut_end, right))
            remaining = next_remaining
        pieces.extend(remaining)
    return _union_seconds(pieces)


def _component(event: dict[str, Any]) -> tuple[str, str]:
    event_type = str(event.get("event_type") or "event").casefold()
    role = str(event.get("agent_role") or event.get("actor_role") or "").casefold()
    name = str(event.get("agent_name") or event.get("actor_name") or "").strip()
    if name.casefold() in {"—", "-", "none"}:
        name = ""
    if event_type.startswith("task_analysis.clarification"):
        return "clarification", "Clarification"
    if event_type.startswith("task_analysis."):
        return "task_analyst", name or "Task Analyst"
    if event_type.startswith("freya.planning.") or event_type == "freya.plan.created":
        return "planner", "Planner"
    if event_type.startswith("freya.plan_compiler."):
        return "plan_compiler", "Plan Compiler / Runtime preparation"
    if event_type.startswith("freya.agent_factory.") or event_type in {
        "freya.agent_created", "freya.agent_policy.validated", "freya.dynamic_agent.archived",
    }:
        return "agent_factory", name or "Dynamic Agent creation"
    if event_type.startswith("freya.evaluation."):
        return "evaluator", "Evaluator"
    if event_type.startswith("freya.graph."):
        return "graph", "Graph completion"
    if event_type.startswith("freya.integration."):
        return "global_integration", "Global Integration"
    if event_type.startswith("freya.final_response."):
        return "result_integrator", "Result Integrator / Final Response"
    if event_type in {"freya.completed", "freya.failed", "freya.cancelled", "freya.interrupted",
                      "freya.orchestration.started"}:
        return "orchestration", "Orchestration"
    if event_type.startswith("approval."):
        return "approval", "Approval"
    if event_type.startswith("task.") or event_type.startswith("model.") or event_type.startswith("step."):
        if "qa" in role or "quality" in role or "qa" in name.casefold():
            return "qa", name or "QA"
        return "worker", name or role or "Worker execution"
    if event.get("actor_type"):
        return str(event["actor_type"]), name or str(event["actor_type"]).replace("_", " ").title()
    return "system", name or "Orchestrator"


def _metrics(event: dict[str, Any]) -> dict[str, Any]:
    candidates = [event.get("metrics"), event.get("planning_metrics"), event.get("activity_metrics")]
    output = event.get("output")
    if isinstance(output, dict):
        candidates.append(output.get("metrics"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
    return {}


def _safe_event(event: dict[str, Any]) -> dict[str, Any]:
    kind, component = _component(event)
    details: dict[str, Any] = {}
    for key in _DETAIL_FIELDS:
        value = event.get(key)
        if value is not None and value != "" and value != [] and value != {}:
            details[key] = value
    metrics = _metrics(event)
    for key in _DETAIL_FIELDS:
        value = metrics.get(key)
        if value is not None and value != "" and value != [] and value != {}:
            details.setdefault(key, value)
    error = event.get("error")
    message = event.get("message") or event.get("what") or event.get("action") or ""
    row = {
        "id": event.get("id"),
        "timestamp": event.get("timestamp") or event.get("when"),
        "component": component,
        "actor_type": event.get("actor_type") or kind,
        "actor_name": event.get("actor_name") or event.get("agent_name"),
        "event_type": event.get("event_type") or "event",
        "event": str(event.get("event_type") or "event").replace("_", " ").replace(".", " · "),
        "status": event.get("status") or event.get("level") or "Info",
        "duration_seconds": event.get("duration_seconds"),
        "message": sanitize(str(message))[:1200],
        "details": sanitize(details),
    }
    if error:
        row["error"] = sanitize(str(error))[:1000]
    return {key: value for key, value in row.items() if value is not None and value != ""}


def _same_instance(start: dict[str, Any], end: dict[str, Any]) -> bool:
    for key in ("runtime_task_id", "task_id", "plan_task_id", "planned_task_id",
                "evaluation_id", "integration_id", "round", "attempt"):
        left, right = start.get(key), end.get(key)
        if left is not None and right is not None and str(left) != str(right):
            return False
    return True


def _pair(events: list[dict[str, Any]], start_type: str, end_types: set[str],
          phase: str, label: str, *, metric_key: str | None = None,
          qa_from_role: bool = False, now: datetime | None = None) -> list[dict[str, Any]]:
    ends_used: set[int] = set()
    result: list[dict[str, Any]] = []
    for index, start in enumerate(events):
        if start.get("event_type") != start_type:
            continue
        ending = None
        end_index = None
        for candidate_index in range(index + 1, len(events)):
            candidate = events[candidate_index]
            if candidate_index in ends_used or candidate.get("event_type") not in end_types:
                continue
            if _same_instance(start, candidate):
                ending, end_index = candidate, candidate_index
                break
        if end_index is not None:
            ends_used.add(end_index)
        status = _phase_status(ending or start)
        component_label = label
        item_phase = phase
        actor = start.get("agent_name") or start.get("actor_name")
        if qa_from_role:
            role = str(start.get("agent_role") or start.get("actor_role") or "").casefold()
            if "qa" in role or "quality" in role or "qa" in str(actor or "").casefold():
                component_label = str(actor or "QA")
                item_phase = "qa"
            else:
                component_label = str(actor or label)
        finish_time = (ending or {}).get("timestamp") or (ending or {}).get("when")
        start_time = start.get("timestamp") or start.get("when")
        metrics = _metrics(ending or start)
        item = {
            "name": item_phase,
            "label": component_label,
            "actor_type": start.get("actor_type") or _component(start)[0],
            "actor_name": actor,
            "started_at": start_time,
            "completed_at": finish_time,
            "duration_seconds": _seconds(start_time, finish_time or _iso(now)) if not ending else _seconds(start_time, finish_time),
            "status": status if ending else "Running",
            "event_type": (ending or start).get("event_type"),
        }
        for key in ("task_id", "runtime_task_id", "plan_task_id", "planned_task_id",
                    "attempt", "model", "model_calls", "prompt_tokens", "generated_tokens",
                    "total_tokens", "fallback_used", "fallback_reason", "error_path",
                    "error_type", "first_token_latency", "streaming", "tokens_per_second",
                    "resource_catalog_version", "selected_capabilities", "selected_tools",
                    "selected_skills", "semantic_compiler", "model_call_details"):
            value = (ending or {}).get(key, metrics.get(key))
            if value is not None and value != [] and value != {}:
                item[key] = value
        if metric_key:
            value = metrics.get(metric_key)
            if value is not None:
                item[metric_key] = value
        if ending and (ending.get("message") or ending.get("what")):
            item["message"] = sanitize(str(ending.get("message") or ending.get("what")))[:1200]
        if ending and ending.get("error"):
            item["error"] = sanitize(str(ending["error"]))[:1000]
        result.append({key: value for key, value in item.items() if value is not None and value != ""})
    return result


def _phase_status(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type") or "").casefold()
    status = str(event.get("status") or "").casefold()
    if status in {"failed", "denied", "error"} or event_type.endswith((".failed", ".blocked")):
        return "Failed"
    if status == "cancelled" or "cancel" in event_type or "interrupted" in event_type:
        return "Cancelled"
    if status in {"needsclarification", "needs_clarification"}:
        return "Needs clarification"
    if event_type in {"task_analysis.updated", "freya.plan.created", "freya.agent_created",
                      "task.success", "freya.evaluation.completed", "freya.graph.completed",
                      "freya.integration.completed", "freya.final_response.created", "freya.completed"}:
        return "Success"
    return str(event.get("status") or "Running")


def _wait_spans(events: list[dict[str, Any]], start_type: str, end_type: str,
                now: datetime, run_end: datetime) -> list[tuple[datetime, datetime]]:
    open_starts: dict[str, list[datetime]] = {}
    spans: list[tuple[datetime, datetime]] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        key = str(event.get("approval_id") or event.get("task_id") or event.get("plan_task_id") or "default")
        timestamp = _date(event.get("timestamp") or event.get("when"))
        if timestamp is None:
            continue
        if event_type == start_type:
            open_starts.setdefault(key, []).append(timestamp)
        elif event_type == end_type and open_starts.get(key):
            left = open_starts[key].pop(0)
            right = min(timestamp, run_end)
            if right > left:
                spans.append((left, right))
    for starts in open_starts.values():
        for left in starts:
            right = min(now, run_end)
            if right > left:
                spans.append((left, right))
    return spans


def _metric_sources(run: dict[str, Any], events: list[dict[str, Any]],
                    evaluations: list[dict[str, Any]],
                    integrations: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    sources: list[tuple[str, dict[str, Any]]] = []
    analyst_events = [item for item in events if item.get("event_type") == "task_analysis.updated"]
    sources.extend(("task_analyst", _metrics(item)) for item in analyst_events)
    plan_events = [item for item in events if item.get("event_type") in {
        "freya.plan.created", "freya.planning.failed",
    }]
    if plan_events:
        sources.extend(("planner", _metrics(item)) for item in plan_events[-1:])
    elif isinstance(run.get("planning_metrics"), dict):
        sources.append(("planner", run["planning_metrics"]))
    for item in evaluations:
        metrics = item.get("metrics")
        if isinstance(metrics, dict):
            sources.append(("evaluator", metrics))
    for item in integrations:
        metrics = item.get("metrics")
        if isinstance(metrics, dict):
            sources.append(("global_integration", metrics))
    for item in events:
        if item.get("event_type") == "freya.final_response.created":
            metrics = _metrics(item)
            if metrics:
                sources.append(("result_integrator", metrics))
    return sources


def _llm_summary(run: dict[str, Any], events: list[dict[str, Any]],
                 evaluations: list[dict[str, Any]],
                 integrations: list[dict[str, Any]]) -> dict[str, Any]:
    calls = prompt_tokens = generated_tokens = 0
    duration_seconds = 0.0
    first_tokens: list[float] = []
    streaming_values: list[bool] = []
    for _, metrics in _metric_sources(run, events, evaluations, integrations):
        details = metrics.get("model_call_details")
        details = details if isinstance(details, list) else []
        count = int(metrics.get("model_calls") or 0)
        calls += max(count, len(details))
        if details:
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                prompt_tokens += int(detail.get("prompt_tokens") or 0)
                generated_tokens += int(detail.get("generated_tokens") or 0)
                duration_seconds += float(detail.get("total_duration") or detail.get("duration_seconds") or 0)
                latency = detail.get("first_token_latency")
                if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                    first_tokens.append(float(latency))
                if isinstance(detail.get("streaming"), bool):
                    streaming_values.append(detail["streaming"])
        else:
            prompt_tokens += int(metrics.get("prompt_tokens") or 0)
            generated_tokens += int(metrics.get("generated_tokens") or 0)
            if count:
                duration_seconds += float(metrics.get("llm_duration_seconds") or metrics.get("duration_seconds") or 0)
    # Runtime model.finished events are individual, non-overlapping LLM calls.
    for event in events:
        if event.get("event_type") != "model.finished":
            continue
        calls += 1
        details = _metrics(event)
        prompt_tokens += int(event.get("prompt_tokens") or details.get("prompt_tokens") or 0)
        generated_tokens += int(event.get("generated_tokens") or details.get("generated_tokens") or 0)
        duration_seconds += float(event.get("duration_seconds") or details.get("duration_seconds") or 0)
        latency = event.get("first_token_latency") or details.get("first_token_latency")
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            first_tokens.append(float(latency))
        if isinstance(event.get("streaming"), bool):
            streaming_values.append(event["streaming"])
    summary = {
        "calls": calls,
        "prompt_tokens": prompt_tokens,
        "generated_tokens": generated_tokens,
        "total_tokens": prompt_tokens + generated_tokens,
        "duration_seconds": round(duration_seconds, 4),
    }
    if first_tokens:
        summary["first_token_latency_seconds"] = round(sum(first_tokens) / len(first_tokens), 4)
    if generated_tokens and duration_seconds > 0:
        summary["tokens_per_second"] = round(generated_tokens / duration_seconds, 3)
    if streaming_values:
        summary["streaming_calls"] = sum(streaming_values)
    return summary


def build_orchestration_activity(run: dict[str, Any], events: list[dict[str, Any]], *,
                                 evaluations: list[dict[str, Any]] | None = None,
                                 integrations: list[dict[str, Any]] | None = None,
                                 now: datetime | str | None = None) -> dict[str, Any]:
    """Build chronologically safe event, phase and non-additive time summaries."""
    now_dt = now if isinstance(now, datetime) else _date(now) if now else datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_dt = now_dt.astimezone(timezone.utc)
    started_at = run.get("created_at")
    safe_events = [dict(item) for item in events if isinstance(item, dict)]
    integration_metrics = {
        str(item.get("id")): item.get("metrics")
        for item in (integrations or []) if item.get("id") and isinstance(item.get("metrics"), dict)
    }
    for event in safe_events:
        integration_id = event.get("integration_id")
        if integration_id and str(integration_id) in integration_metrics:
            event["activity_metrics"] = integration_metrics[str(integration_id)]
    safe_events.sort(key=lambda item: (_date(item.get("timestamp") or item.get("when")) or now_dt,
                                       str(item.get("id") or "")))
    terminal_events = [item for item in safe_events if item.get("event_type") in {
        "freya.completed", "freya.failed", "freya.cancelled", "freya.interrupted",
    }]
    status = str(run.get("status") or "Queued")
    folded_status = status.casefold()
    terminal = folded_status in {"success", "completed", "failed", "cancelled", "interrupted"}
    end_dt = (_date(terminal_events[-1].get("timestamp")) if terminal_events else None)
    if terminal and end_dt is None:
        end_dt = _date(run.get("updated_at"))
    if end_dt is None:
        end_dt = now_dt
    start_dt = _date(started_at) or end_dt

    rows = [_safe_event(item) for item in safe_events]
    if not any(item.get("event_type") == "freya.orchestration.started" for item in safe_events):
        rows.append({
            "id": "derived:orchestration-start", "timestamp": _iso(start_dt),
            "component": "Orchestration", "actor_type": "orchestrator",
            "event_type": "freya.orchestration.started", "event": "Orchestration · started",
            "status": "Running", "message": "Freya accepted the orchestration request.",
            "details": {}, "derived": True,
        })
    if terminal and not terminal_events:
        terminal_event = {
            "failed": ("freya.failed", "Failed", "failed"),
            "cancelled": ("freya.cancelled", "Cancelled", "cancelled"),
            "interrupted": ("freya.interrupted", "Failed", "interrupted"),
        }.get(folded_status, ("freya.completed", "Success", "completed"))
        event_type, terminal_event_status, terminal_label = terminal_event
        rows.append({
            "id": "derived:orchestration-terminal", "timestamp": _iso(end_dt),
            "component": "Orchestration", "actor_type": "orchestrator",
            "event_type": event_type, "event": "Orchestration · " + terminal_label,
            "status": terminal_event_status,
            "message": sanitize(str(run.get("error") or run.get("response") or "Orchestration reached a terminal state."))[:1200],
            "details": {}, "derived": True,
        })
    rows.sort(key=lambda item: (_date(item.get("timestamp")) or now_dt, str(item.get("id") or "")))

    phases: list[dict[str, Any]] = [{
        "name": "orchestration", "label": "Orchestration", "actor_type": "orchestrator",
        "started_at": _iso(start_dt), "completed_at": _iso(end_dt) if terminal else None,
        "duration_seconds": _seconds(_iso(start_dt), _iso(end_dt)),
        "status": {
            "failed": "Failed", "cancelled": "Cancelled", "interrupted": "Failed",
            "success": "Success", "completed": "Success",
        }.get(folded_status, status),
    }]
    phases.extend(_pair(safe_events, "task_analysis.started", {"task_analysis.updated"},
                        "task_analysis", "Task Analyst", now=now_dt))
    phases.extend(_pair(safe_events, "freya.planning.started",
                        {"freya.plan.created", "freya.planning.failed"}, "planning", "Planner", now=now_dt))
    phases.extend(_pair(safe_events, "freya.plan_compiler.started",
                        {"freya.plan_compiler.completed", "freya.plan_compiler.failed"},
                        "plan_compiler", "Plan Compiler / Runtime preparation", now=now_dt))
    phases.extend(_pair(safe_events, "freya.agent_factory.started",
                        {"freya.agent_created", "freya.agent_factory.failed"},
                        "dynamic_agent_creation", "Dynamic Agent creation", now=now_dt))
    phases.extend(_pair(safe_events, "task.started",
                        {"task.success", "task.failed", "task.cancelled", "task.interrupted"},
                        "worker_execution", "Worker execution", qa_from_role=True, now=now_dt))
    phases.extend(_pair(safe_events, "freya.evaluation.started",
                        {"freya.evaluation.completed", "freya.evaluation.failed"},
                        "evaluation", "Evaluator", now=now_dt))
    phases.extend(_pair(safe_events, "freya.graph.initialized", {"freya.graph.completed"},
                        "graph", "Graph completion", now=now_dt))
    phases.extend(_pair(safe_events, "freya.integration.started",
                        {"freya.integration.completed", "freya.integration.failed"},
                        "global_integration", "Global Integration", now=now_dt))
    phases.extend(_pair(safe_events, "freya.final_response.started",
                        {"freya.final_response.created", "freya.final_response.failed"},
                        "result_integrator", "Result Integrator / Final Response", now=now_dt))

    user_wait = _wait_spans(safe_events, "task_analysis.clarification_required",
                            "task_analysis.clarification_received", now_dt, end_dt)
    approval_wait = _wait_spans(safe_events, "approval.requested", "approval.resolved", now_dt, end_dt)
    for name, label, spans, finish_label in (
        ("clarification_wait", "Clarification waiting", user_wait, "Received"),
        ("approval_wait", "Approval waiting", approval_wait, "Resolved"),
    ):
        for left, right in spans:
            phases.append({
                "name": name, "label": label, "actor_type": "human",
                "started_at": _iso(left), "completed_at": _iso(right),
                "duration_seconds": round((right - left).total_seconds(), 4),
                "status": finish_label if right < now_dt or terminal else "Waiting",
            })

    orchestration_interval = [(start_dt, end_dt)] if end_dt > start_dt else []
    total_elapsed = _union_seconds(orchestration_interval)
    waiting_user = _union_seconds(user_wait)
    waiting_approval = _union_seconds(approval_wait)
    all_wait = _union_seconds([*user_wait, *approval_wait])
    worker_intervals: list[tuple[datetime, datetime]] = []
    open_workers: dict[str, datetime] = {}
    worker_terminals = {"task.success", "task.failed", "task.cancelled", "task.interrupted"}
    for event in safe_events:
        kind = event.get("event_type")
        key = str(event.get("task_id") or event.get("runtime_task_id") or "")
        timestamp = _date(event.get("timestamp") or event.get("when"))
        if not timestamp or not key:
            continue
        if kind == "task.started":
            open_workers[key] = timestamp
        elif kind in worker_terminals and key in open_workers:
            left = open_workers.pop(key)
            if timestamp > left:
                worker_intervals.append((left, timestamp))
    for left in open_workers.values():
        if end_dt > left:
            worker_intervals.append((left, end_dt))
    processing = max(0.0, total_elapsed - all_wait)
    execution = _subtract_covered_seconds(worker_intervals, [*user_wait, *approval_wait])
    llm = _llm_summary(run, safe_events, evaluations or [], integrations or [])
    phases.sort(key=lambda item: (_date(item.get("started_at")) or now_dt, item.get("name", "")))
    return {
        "orchestration_id": run.get("id"), "status": status,
        "started_at": _iso(start_dt), "completed_at": _iso(end_dt) if terminal else None,
        "total_elapsed_seconds": total_elapsed,
        "processing_seconds": round(processing, 4),
        "execution_seconds": execution,
        "waiting_for_user_seconds": waiting_user,
        "clarification_waiting_seconds": waiting_user,
        "waiting_for_approval_seconds": waiting_approval,
        "llm": llm, "phases": phases, "events": rows,
        "duration_semantics": {
            "total_elapsed": "Wall clock from orchestration creation to terminal event, or current time while active.",
            "processing": "Total elapsed excluding the union of clarification and approval wait intervals.",
            "execution": "Union of worker task intervals excluding approval wait; parallel workers are not double-counted.",
            "llm": "Sum of recorded model-call durations. It is contained within processing and may overlap worker phases.",
            "phases": "Individual wall-clock spans can overlap or nest; do not add them to form a total.",
        },
    }
