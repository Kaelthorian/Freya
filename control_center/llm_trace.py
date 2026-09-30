"""Bounded, redacted observability for local model calls."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
import os
import time
import uuid
from typing import Any, Callable, Iterator

from .security import sanitize


_emitter: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "freya_llm_trace_emitter", default=None)
_last_call_id: ContextVar[str] = ContextVar("freya_last_llm_call_id", default="")
_last_component: ContextVar[str] = ContextVar("freya_last_llm_component", default="")
_last_prompt_name: ContextVar[str] = ContextVar("freya_last_llm_prompt_name", default="")
PROMPT_VERSIONS = {
    "task_analyst": "task-spec-v1", "planner": "semantic-plan-v4",
    "worker": "worker-v1", "evaluator": "evaluator-v1",
    "failure_analyzer": "failure-analysis-v1", "recovery_replanner": "recovery-v1",
    "global_verifier": "global-verification-v1",
    "integration_replanner": "integration-replan-v1",
    "result_integrator": "result-integration-v1",
}


@contextmanager
def bind_llm_trace(emit: Callable[[dict[str, Any]], None]) -> Iterator[None]:
    token = _emitter.set(emit)
    call_token = _last_call_id.set("")
    component_token = _last_component.set("")
    prompt_token = _last_prompt_name.set("")
    try:
        yield
    finally:
        _last_prompt_name.reset(prompt_token)
        _last_component.reset(component_token)
        _last_call_id.reset(call_token)
        _emitter.reset(token)


def debug_enabled() -> bool:
    return os.getenv("FREYA_DEBUG_LLM_PROMPTS", "").strip().casefold() in {"true", "1", "yes", "on"}


def debug_max_chars() -> int:
    try:
        configured = int(os.getenv("FREYA_DEBUG_PROMPT_MAX_CHARS", "20000"))
    except ValueError:
        configured = 20000
    return max(1000, min(configured, 200000))


def _bounded(value: Any, limit: int) -> tuple[Any, dict[str, int | bool]]:
    """Preserve JSON structure while distributing one character budget over values."""
    source = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    def oversized_string(item: Any) -> bool:
        if isinstance(item, str):
            return len(item) > limit
        if isinstance(item, dict):
            return any(oversized_string(child) for child in item.values())
        if isinstance(item, (list, tuple)):
            return any(oversized_string(child) for child in item)
        return False
    clean = sanitize(value, max_string_chars=limit)
    serialized = json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)
    budget = [limit]
    def visit(item: Any) -> Any:
        if isinstance(item, str):
            kept = item[:max(0, budget[0])]
            budget[0] -= len(kept)
            return kept
        if isinstance(item, dict):
            return {key: visit(child) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item
    result = visit(clean)
    stored_chars = len(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return result, {"original_chars": len(source), "original_size": len(source),
                    "redacted_chars": len(serialized),
                    "retained_value_chars": limit - budget[0],
                    "stored_chars": stored_chars, "stored_size": stored_chars,
                    "truncated": oversized_string(value) or
                                 (budget[0] == 0 and len(serialized) > limit)}


def trace_model_call(component: str, body: dict[str, Any], operation: Callable[[], dict[str, Any]],
                     *, stage: str = "initial", call_id: str | None = None,
                     prompt_name: str | None = None,
                     structured_context: Any = None) -> dict[str, Any]:
    """Capture the exact request body and preparse response at the transport boundary."""
    emit = _emitter.get()
    if emit is None:
        return operation()
    previous_call_id = _last_call_id.get()
    identifier = call_id or uuid.uuid4().hex
    _last_call_id.set(identifier)
    _last_component.set(component)
    _last_prompt_name.set(prompt_name or component)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    event: dict[str, Any] = {
        "event_type": "llm.call", "level": "info", "llm_call_id": identifier,
        "component": component, "stage": stage,
        "prompt_name": prompt_name or component,
        "prompt_version": PROMPT_VERSIONS.get(prompt_name or component, "v1"),
        "model": str(body.get("model") or "")[:200],
        "prompt_sha256": sha256(encoded.encode("utf-8")).hexdigest(),
        "prompt_chars": len(encoded), "debug_prompts_enabled": debug_enabled(),
    }
    if stage == "repair" and previous_call_id:
        event["repair_of_llm_call_id"] = previous_call_id
    if debug_enabled():
        event["request_body"], event["request_truncation"] = _bounded(body, debug_max_chars())
        if structured_context is not None:
            event["structured_context"], event["context_truncation"] = _bounded(
                structured_context, debug_max_chars())
    started = time.monotonic()
    try:
        response = operation()
    except Exception as exc:
        event.update(status="Failed", error_type=type(exc).__name__,
                     duration_seconds=round(time.monotonic() - started, 4))
        try:
            emit(event)
        except Exception:
            pass
        raise
    event.update(status="Success", duration_seconds=round(time.monotonic() - started, 4),
                 prompt_tokens=response.get("prompt_eval_count"),
                 generated_tokens=response.get("eval_count"))
    message = response.get("message")
    raw = message.get("content") if isinstance(message, dict) else None
    if isinstance(raw, str):
        event["response_chars"] = len(raw)
        event["response_sha256"] = sha256(raw.encode("utf-8")).hexdigest()
        if debug_enabled():
            event["raw_response"], event["response_truncation"] = _bounded(raw, debug_max_chars())
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                event["parse_status"] = "invalid_json_or_non_json_action"
            else:
                event["parse_status"] = "json_parsed"
                event["parsed_response"], event["parsed_truncation"] = _bounded(
                    parsed, debug_max_chars())
    if isinstance(message, dict) and message.get("tool_calls") and debug_enabled():
        event["tool_calls"], event["tool_calls_truncation"] = _bounded(
            message["tool_calls"], debug_max_chars())
    try:
        emit(event)
    except Exception:
        pass
    response["_freya_llm_call_id"] = identifier
    return response


def record_validation(component: str, status: str, *, detail: str = "",
                      normalized_response: Any = None) -> None:
    emit = _emitter.get()
    if emit is None or component not in {_last_component.get(), _last_prompt_name.get()}:
        return
    event = {"event_type": "llm.validation", "level": "info", "component": component,
             "llm_call_id": _last_call_id.get(), "status": status,
             "debug_prompts_enabled": debug_enabled()}
    if debug_enabled():
        event["detail"] = sanitize(detail[:600])
        if normalized_response is not None:
            event["normalized_response"], event["normalization_truncation"] = _bounded(
                normalized_response, debug_max_chars())
    elif status == "rejected" and detail:
        event["error_type"] = detail.split(":", 1)[0][:80]
    try:
        emit(event)
    except Exception:
        pass
