"""Bounded, redacted observability for local model calls."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from hashlib import sha256
import json
import time
import uuid
from typing import Any, Callable, Iterator

from .security import sanitize
from .settings import get_settings


_emitter: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "freya_llm_trace_emitter", default=None)
_last_call_id: ContextVar[str] = ContextVar("freya_last_llm_call_id", default="")
_last_component: ContextVar[str] = ContextVar("freya_last_llm_component", default="")
_last_prompt_name: ContextVar[str] = ContextVar("freya_last_llm_prompt_name", default="")
_last_call_debug: ContextVar[dict[str, Any]] = ContextVar("freya_last_llm_debug", default={})
PROMPT_VERSIONS = {
    "task_analyst": "task-spec-v1", "planner": "semantic-plan-v5",
    "worker": "worker-v1", "evaluator": "evaluator-v1",
    "worker_forced_finalization": "worker-finalization-v1",
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
    debug_token = _last_call_debug.set({})
    try:
        yield
    finally:
        _last_call_debug.reset(debug_token)
        _last_prompt_name.reset(prompt_token)
        _last_component.reset(component_token)
        _last_call_id.reset(call_token)
        _emitter.reset(token)


def debug_enabled() -> bool:
    return get_settings().debug_llm_prompts


def debug_max_chars() -> int:
    return get_settings().debug_prompt_max_chars


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
    clipped = [False]
    def visit(item: Any) -> Any:
        if isinstance(item, str):
            kept = item[:max(0, budget[0])]
            clipped[0] |= len(kept) < len(item)
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
                    "truncated": oversized_string(value) or clipped[0]}


def trace_model_call(component: str, body: dict[str, Any], operation: Callable[[], dict[str, Any]],
                     *, stage: str = "initial", call_id: str | None = None,
                     prompt_name: str | None = None,
                     structured_context: Any = None) -> dict[str, Any]:
    """Capture the exact request body and preparse response at the transport boundary."""
    emit = _emitter.get()
    if emit is None:
        return operation()
    previous_call_id = _last_call_id.get()
    previous_component = _last_component.get()
    previous_prompt_name = _last_prompt_name.get()
    identifier = call_id or uuid.uuid4().hex
    _last_call_id.set(identifier)
    _last_component.set(component)
    _last_prompt_name.set(prompt_name or component)
    settings = get_settings()
    enabled, limit = settings.debug_llm_prompts, settings.debug_prompt_max_chars
    _last_call_debug.set({"stage": stage, "enabled": enabled, "limit": limit})
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    event: dict[str, Any] = {
        "event_type": "llm.call", "level": "info", "llm_call_id": identifier,
        "component": component, "stage": stage,
        "prompt_name": prompt_name or component,
        "prompt_version": PROMPT_VERSIONS.get(prompt_name or component, "v1"),
        "model": str(body.get("model") or "")[:200],
        "prompt_sha256": sha256(encoded.encode("utf-8")).hexdigest(),
        "prompt_chars": len(encoded), "debug_prompts_enabled": enabled,
    }
    if (stage == "repair" and previous_call_id and previous_component == component
            and previous_prompt_name == (prompt_name or component)):
        event["repair_of_llm_call_id"] = previous_call_id
    if enabled:
        event["request_body"], event["request_truncation"] = _bounded(body, limit)
        if structured_context is not None:
            event["structured_context"], event["context_truncation"] = _bounded(
                structured_context, limit)
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
        if enabled:
            event["raw_response"], event["response_truncation"] = _bounded(raw, limit)
            _last_call_debug.set({"stage": stage, "enabled": enabled, "limit": limit,
                                  "raw_response": event["raw_response"],
                                  "response_truncation": event["response_truncation"]})
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                event["parse_status"] = "invalid_json_or_non_json_action"
            else:
                event["parse_status"] = "json_parsed"
                event["parsed_response"], event["parsed_truncation"] = _bounded(
                    parsed, limit)
    if isinstance(message, dict) and message.get("tool_calls") and enabled:
        event["tool_calls"], event["tool_calls_truncation"] = _bounded(
            message["tool_calls"], limit)
    try:
        emit(event)
    except Exception:
        pass
    response["_freya_llm_call_id"] = identifier
    return response


def record_validation(component: str, status: str, *, detail: str = "",
                      normalized_response: Any = None,
                      validation_stage: str = "output_contract") -> None:
    emit = _emitter.get()
    if emit is None or component not in {_last_component.get(), _last_prompt_name.get()}:
        return
    call = _last_call_debug.get()
    enabled = call.get("enabled", False)
    limit = call.get("limit", debug_max_chars())
    event = {"event_type": "llm.validation", "level": "info", "component": component,
             "llm_call_id": _last_call_id.get(), "status": status,
             "debug_prompts_enabled": enabled}
    if status == "rejected" and detail:
        event["error_type"] = sanitize(detail.split(":", 1)[0][:80])
    if enabled:
        name = _last_prompt_name.get()
        event.update(contract_name=name, contract_version=PROMPT_VERSIONS.get(name, "v1"),
                     validation_stage=validation_stage, call_stage=call.get("stage", "initial"))
        event["detail"], event["detail_truncation"] = _bounded(detail, limit)
        if status == "rejected":
            event["validation_message"] = event["detail"]
            if "raw_response" in call:
                event["raw_response"] = call["raw_response"]
                event["response_truncation"] = call["response_truncation"]
        if normalized_response is not None:
            event["normalized_response"], event["normalization_truncation"] = _bounded(
                normalized_response, limit)
    try:
        emit(event)
    except Exception:
        pass


def observe_validation(component: str, *, validation_stage: str):
    """Add rejection diagnostics to a validator without changing results or exceptions."""
    def decorate(validate):
        @wraps(validate)
        def observed(*args, **kwargs):
            try:
                return validate(*args, **kwargs)
            except Exception as exc:
                try:
                    record_validation(component, "rejected", detail=f"{type(exc).__name__}: {exc}",
                                      validation_stage=validation_stage)
                except Exception:
                    pass
                raise
        return observed
    return decorate
