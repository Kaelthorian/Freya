"""Bounded, non-redirecting HTTP transport for the local Ollama API."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class TransportError(RuntimeError):
    """A provider request failed without exposing its body or credentials."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def request_json(method: str, url: str, body: dict[str, Any] | None = None,
                 *, timeout: float = 10, token: str = "") -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, data=json.dumps(body).encode("utf-8") if body is not None else None,
                      headers=headers, method=method)
    try:
        # Neither proxy configuration nor redirects may forward provider credentials.
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=max(.05, timeout)) as response:
            data = response.read(4_000_001)
            if len(data) > 4_000_000:
                raise TransportError("Ollama response exceeds the 4 MB limit.")
    except HTTPError as exc:
        raise TransportError("Ollama returned HTTP {}.".format(exc.code)) from None
    except (URLError, TimeoutError, OSError) as exc:
        raise TransportError("Could not reach Ollama ({}).".format(type(exc).__name__)) from None
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise TransportError("Ollama returned invalid JSON.") from None
    if not isinstance(result, dict):
        raise TransportError("Ollama returned an invalid response object.")
    return result


def list_models(endpoint: str = "http://127.0.0.1:11434", *, token: str = "",
                timeout: float = 5) -> list[dict[str, Any]]:
    result = request_json("GET", endpoint.rstrip("/") + "/api/tags", token=token, timeout=timeout)
    models = result.get("models")
    if not isinstance(models, list):
        raise TransportError("Ollama returned an invalid model list.")
    return [item for item in models if isinstance(item, dict)]
