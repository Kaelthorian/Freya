"""Bounded local Ollama HTTP transport with streamed chat and safe telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import http.client
import json
import logging
import socket
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_RESPONSE_BYTES = 4_000_000
PROVIDER_COOLDOWN_SECONDS = 5.0
_logger = logging.getLogger("freya.ollama")
_health_lock = threading.Lock()
_unhealthy_until: dict[str, float] = {}


@dataclass(frozen=True)
class ModelProfile:
    connect_timeout: float
    inactivity_timeout: float
    hard_timeout: float
    max_output_tokens: int
    repair_output_tokens: int


# Existing caller timeout overrides inactivity; it no longer bounds the whole
# generation. A worker also supplies its remaining wall-clock as hard_timeout.
MODEL_PROFILES = {
    "task_analyst": ModelProfile(5, 90, 600, 4096, 3072),
    "planner": ModelProfile(5, 120, 600, 4096, 2048),
    "worker": ModelProfile(5, 90, 600, 2048, 768),
    "evaluator": ModelProfile(5, 90, 360, 1024, 512),
    "global_verifier": ModelProfile(5, 120, 600, 1024, 512),
    "integration_replanner": ModelProfile(5, 120, 600, 1024, 512),
    "result_integrator": ModelProfile(5, 120, 600, 1024, 512),
    "failure_analyzer": ModelProfile(5, 120, 360, 768, 512),
    "recovery_replanner": ModelProfile(5, 120, 600, 768, 512),
}


def model_profile(component: str) -> ModelProfile:
    return MODEL_PROFILES[component]


class TransportError(RuntimeError):
    """Provider failure with a stable category and body-free call metadata."""

    def __init__(self, message: str, *, code: str = "OLLAMA_HTTP_ERROR",
                 metrics: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.metrics = dict(metrics or {})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _log_call(metrics: dict[str, Any]) -> None:
    _logger.info(json.dumps(metrics, sort_keys=True, separators=(",", ":")))


def _cooldown(origin: str) -> None:
    with _health_lock:
        _unhealthy_until[origin] = time.monotonic() + PROVIDER_COOLDOWN_SECONDS


def provider_health(origin: str) -> dict[str, Any]:
    """Read-only provider circuit state; slow models never open this circuit."""
    with _health_lock:
        remaining = max(0.0, _unhealthy_until.get(origin, 0.0) - time.monotonic())
    return {"provider": "ollama", "reachable": remaining == 0,
            "retry_after_seconds": round(remaining, 3)}


def _chat_request(url: str, body: dict[str, Any], *, token: str,
                  component: str, timeout: float | None, hard_timeout: float | None,
                  max_output_tokens: int | None) -> dict[str, Any]:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    profile = MODEL_PROFILES.get(component, MODEL_PROFILES["worker"])
    inactivity = float(timeout) if timeout is not None else profile.inactivity_timeout
    hard = min(profile.hard_timeout, float(hard_timeout)) if hard_timeout is not None else profile.hard_timeout
    if inactivity <= 0 or hard <= 0:
        raise ValueError("Ollama timeouts must be positive.")
    started = time.monotonic()
    metrics: dict[str, Any] = {
        "provider": "ollama", "model": str(body.get("model") or "")[:200],
        "component": component, "request_started_at": datetime.now(timezone.utc).isoformat(),
        "connection_established": False, "first_token_received": False,
        "time_to_connect": None, "first_token_latency": None,
        "generation_duration": None, "total_duration": 0.0,
        "prompt_tokens": 0, "generated_tokens": 0, "token_count_estimated": True,
        "stream_chunks": 0, "tokens_per_second": None,
        "stop_reason": "", "timeout_type": None, "http_status": None, "streaming": True,
    }

    def finish() -> None:
        metrics["total_duration"] = round(time.monotonic() - started, 4)
        if metrics["first_token_latency"] is not None:
            metrics["generation_duration"] = round(
                max(0.0, metrics["total_duration"] - metrics["first_token_latency"]), 4)
        duration = metrics["generation_duration"]
        if duration and metrics["generated_tokens"]:
            metrics["tokens_per_second"] = round(metrics["generated_tokens"] / duration, 3)
        _log_call(metrics)

    def fail(message: str, code: str, *, timeout_type: str | None = None) -> TransportError:
        metrics["stop_reason"] = code
        metrics["timeout_type"] = timeout_type
        return TransportError(message, code=code, metrics=metrics)

    if not provider_health(origin)["reachable"]:
        error = fail("Could not reach Ollama (provider circuit open).", "OLLAMA_UNREACHABLE")
        finish()
        error.metrics = dict(metrics)
        raise error

    payload = dict(body)
    # Legacy adapters and injected test transports retain their request shape;
    # production chat always streams here and reconstructs that same shape.
    payload["stream"] = True
    options = dict(payload.get("options") or {})
    if max_output_tokens is not None:
        options["num_predict"] = max_output_tokens
    elif not isinstance(options.get("num_predict"), int) or options["num_predict"] < 0:
        options["num_predict"] = profile.max_output_tokens
    payload["options"] = options
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port,
                                 timeout=max(.05, min(profile.connect_timeout, hard)))
    chunks: list[str] = []
    tool_calls: list[Any] = []
    final: dict[str, Any] = {}
    buffered = b""
    seen_lines = 0
    saw_done = False
    saw_stream_marker = False
    received_bytes = 0
    last_activity = started
    response: http.client.HTTPResponse | None = None

    def consume(line: bytes) -> None:
        nonlocal seen_lines, saw_done, saw_stream_marker
        if not line.strip():
            return
        seen_lines += 1
        try:
            item = json.loads(line)
        except (ValueError, UnicodeError):
            raise fail("Ollama returned invalid streamed JSON.", "OLLAMA_INVALID_RESPONSE")
        if not isinstance(item, dict):
            raise fail("Ollama returned an invalid response object.", "OLLAMA_INVALID_RESPONSE")
        if isinstance(item.get("error"), str):
            raise fail("Ollama returned a generation error.", "OLLAMA_HTTP_ERROR")
        message = item.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content:
                chunks.append(content)
                metrics["stream_chunks"] += 1
                metrics["generated_tokens"] += 1
                if not metrics["first_token_received"]:
                    metrics["first_token_received"] = True
                    metrics["first_token_latency"] = round(time.monotonic() - started, 4)
            calls = message.get("tool_calls")
            if isinstance(calls, list) and calls:
                tool_calls.extend(calls)
                if not metrics["first_token_received"]:
                    metrics["first_token_received"] = True
                    metrics["first_token_latency"] = round(time.monotonic() - started, 4)
        final.update({key: value for key, value in item.items() if key != "message"})
        if item.get("done") is True:
            saw_done = True
        if "done" in item:
            saw_stream_marker = True

    try:
        try:
            connection.connect()
        except (socket.timeout, TimeoutError) as exc:
            raise fail("Ollama connection timed out.", "OLLAMA_REQUEST_TIMEOUT",
                       timeout_type="connect") from exc
        except OSError as exc:
            _cooldown(origin)
            raise fail("Could not reach Ollama ({}).".format(type(exc).__name__),
                       "OLLAMA_UNREACHABLE") from exc
        metrics["connection_established"] = True
        metrics["time_to_connect"] = round(time.monotonic() - started, 4)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            connection.request("POST", path, body=encoded, headers=headers)
            if connection.sock is not None:
                connection.sock.settimeout(max(.05, min(inactivity, hard - (time.monotonic() - started))))
            response = connection.getresponse()
        except (socket.timeout, TimeoutError) as exc:
            kind = "hard" if time.monotonic() - started >= hard else "inactivity"
            raise fail("Ollama did not begin responding before the timeout.",
                       "OLLAMA_REQUEST_TIMEOUT", timeout_type=kind) from exc
        metrics["http_status"] = response.status
        if response.status != 200:
            raise fail(f"Ollama returned HTTP {response.status}.", "OLLAMA_HTTP_ERROR")
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= hard:
                raise fail(f"Ollama generation timed out after {round(hard, 3)} seconds.",
                           "OLLAMA_GENERATION_TIMEOUT", timeout_type="hard")
            idle = time.monotonic() - last_activity
            if idle >= inactivity:
                raise fail("Ollama generation stopped producing data.",
                           "OLLAMA_GENERATION_TIMEOUT" if metrics["first_token_received"]
                           else "OLLAMA_REQUEST_TIMEOUT", timeout_type="inactivity")
            if connection.sock is not None:
                connection.sock.settimeout(max(.05, min(hard - elapsed, inactivity - idle)))
            try:
                data = response.read1(4096)
            except (socket.timeout, TimeoutError) as exc:
                elapsed = time.monotonic() - started
                kind = "hard" if elapsed >= hard else "inactivity"
                code = ("OLLAMA_GENERATION_TIMEOUT" if metrics["first_token_received"]
                        or kind == "hard" else "OLLAMA_REQUEST_TIMEOUT")
                raise fail("Ollama generation timed out." if code == "OLLAMA_GENERATION_TIMEOUT"
                           else "Ollama responded but sent no generation data.",
                           code, timeout_type=kind) from exc
            if not data:
                break
            last_activity = time.monotonic()
            received_bytes += len(data)
            if received_bytes > MAX_RESPONSE_BYTES:
                raise fail("Ollama response exceeds the 4 MB limit.", "OLLAMA_INVALID_RESPONSE")
            buffered += data
            while b"\n" in buffered:
                line, buffered = buffered.split(b"\n", 1)
                consume(line)
            if saw_done:
                break
        if buffered.strip():
            consume(buffered)
        if not seen_lines or ((seen_lines > 1 or saw_stream_marker) and not saw_done):
            raise fail("Ollama streamed response ended before completion.",
                       "OLLAMA_INVALID_RESPONSE")
        for source, target in (("prompt_eval_count", "prompt_tokens"),
                               ("eval_count", "generated_tokens")):
            count = final.get(source)
            if count is None:
                continue
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise fail("Ollama returned invalid token metrics.", "OLLAMA_INVALID_RESPONSE")
            metrics[target] = count
        metrics["token_count_estimated"] = "eval_count" not in final
        metrics["stop_reason"] = str(final.get("done_reason") or "completed")[:32]
        result = {**final, "message": {"role": "assistant", "content": "".join(chunks)}}
        if tool_calls:
            result["message"]["tool_calls"] = tool_calls
        finish()
        result["_freya_transport"] = dict(metrics)
        return result
    except TransportError as exc:
        finish()
        exc.metrics = dict(metrics)
        raise
    except (OSError, http.client.HTTPException) as exc:
        code = "OLLAMA_INVALID_RESPONSE" if metrics["connection_established"] else "OLLAMA_UNREACHABLE"
        error = fail("Ollama stream ended unexpectedly." if metrics["connection_established"]
                     else "Could not reach Ollama.", code)
        if code == "OLLAMA_UNREACHABLE":
            _cooldown(origin)
        finish()
        error.metrics = dict(metrics)
        raise error from exc
    finally:
        if response is not None:
            response.close()
        connection.close()


def model_request(request: Callable[..., dict[str, Any]], component: str,
                  method: str, url: str, body: dict[str, Any], *, timeout: float,
                  token: str = "", hard_timeout: float | None = None,
                  max_output_tokens: int | None = None,
                  telemetry: dict[str, Any] | None = None) -> dict[str, Any]:
    """Preserve injected transports while routing production calls by profile."""
    if request is request_json:
        try:
            response = request_json(method, url, body, timeout=timeout, token=token,
                                    component=component, hard_timeout=hard_timeout,
                                    max_output_tokens=max_output_tokens)
        except TransportError as exc:
            if telemetry is not None:
                telemetry["transport"] = exc.metrics
            raise
        if telemetry is not None:
            telemetry["transport"] = response.get("_freya_transport", {})
        return response
    kwargs: dict[str, Any] = {"timeout": timeout}
    if token:
        kwargs["token"] = token
    return request(method, url, body, **kwargs)


def request_json(method: str, url: str, body: dict[str, Any] | None = None,
                 *, timeout: float = 10, token: str = "", component: str = "worker",
                 hard_timeout: float | None = None,
                 max_output_tokens: int | None = None) -> dict[str, Any]:
    if method == "POST" and urlsplit(url).path == "/api/chat" and isinstance(body, dict):
        return _chat_request(url, body, token=token, component=component,
                             timeout=timeout, hard_timeout=hard_timeout,
                             max_output_tokens=max_output_tokens)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, data=json.dumps(body).encode("utf-8") if body is not None else None,
                      headers=headers, method=method)
    try:
        # Neither proxy configuration nor redirects may forward provider credentials.
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=max(.05, timeout)) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise TransportError("Ollama response exceeds the 4 MB limit.",
                                     code="OLLAMA_INVALID_RESPONSE")
    except HTTPError as exc:
        raise TransportError("Ollama returned HTTP {}.".format(exc.code),
                             code="OLLAMA_HTTP_ERROR") from None
    except (URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise TransportError("Ollama request timed out.", code="OLLAMA_REQUEST_TIMEOUT") from None
        raise TransportError("Could not reach Ollama ({}).".format(type(reason).__name__),
                             code="OLLAMA_UNREACHABLE") from None
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise TransportError("Ollama returned invalid JSON.", code="OLLAMA_INVALID_RESPONSE") from None
    if not isinstance(result, dict):
        raise TransportError("Ollama returned an invalid response object.",
                             code="OLLAMA_INVALID_RESPONSE")
    return result


def list_models(endpoint: str = "http://127.0.0.1:11434", *, token: str = "",
                timeout: float = 5) -> list[dict[str, Any]]:
    result = request_json("GET", endpoint.rstrip("/") + "/api/tags", token=token, timeout=timeout)
    models = result.get("models")
    if not isinstance(models, list):
        raise TransportError("Ollama returned an invalid model list.", code="OLLAMA_INVALID_RESPONSE")
    return [item for item in models if isinstance(item, dict)]
