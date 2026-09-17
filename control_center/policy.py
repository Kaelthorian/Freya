"""Validated capability policies and fail-closed deterministic evaluation."""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

from .capabilities import CAPABILITY_REGISTRY

MODES = {"allow", "deny", "ask"}


@dataclass(frozen=True)
class PolicyDecision:
    outcome: str
    reason: str
    action: str
    resource: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


def _path_ok(value: str) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        return False
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    return not path.is_absolute() and ".." not in path.parts and ":" not in normalized


def validate_policy(policy: Any) -> dict[str, Any]:
    """Return a normalized policy or raise ValueError for invalid input."""
    if not isinstance(policy, dict):
        raise ValueError("capability_policy must be an object")
    capabilities = policy.get("capabilities", policy)
    if not isinstance(capabilities, dict):
        raise ValueError("capabilities must be an object")
    normalized: dict[str, Any] = {"capabilities": {}}
    for category, actions in capabilities.items():
        if not isinstance(category, str) or not isinstance(actions, dict):
            raise ValueError("Each capability category must be an object")
        if category not in {item.category for item in CAPABILITY_REGISTRY.values()}:
            raise ValueError("Unknown capability category: " + str(category))
        for action, raw in actions.items():
            capability_id = action if "." in action else f"{category}.{action}"
            if capability_id not in CAPABILITY_REGISTRY:
                raise ValueError("Unknown capability: " + capability_id)
            if "." in action and not action.startswith(category + "."):
                raise ValueError("Capability action does not match its category")
            if not isinstance(raw, dict):
                raise ValueError(f"Policy for {capability_id} must be an object")
            unknown = set(raw) - {"mode", "paths", "extensions", "max_bytes"}
            if unknown:
                raise ValueError("Unknown policy fields: " + ", ".join(sorted(unknown)))
            mode = raw.get("mode", "deny")
            if mode not in MODES:
                raise ValueError("mode must be allow, deny, or ask")
            item: dict[str, Any] = {"mode": mode}
            for key in ("paths", "extensions"):
                if key in raw:
                    values = raw[key]
                    if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                        raise ValueError(f"{key} must be a list of strings")
                    if key == "paths" and any(not _path_ok(v) for v in values):
                        raise ValueError("Policy paths must be relative and cannot contain '..'")
                    item[key] = list(dict.fromkeys(v.replace("\\", "/").strip() for v in values))
            if "max_bytes" in raw:
                value = raw["max_bytes"]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("max_bytes must be a non-negative integer")
                item["max_bytes"] = value
            normalized_name = capability_id.split(".", 1)[1]
            normalized["capabilities"].setdefault(category, {})[normalized_name] = item
    return normalized


def _legacy_action_policy(mode: str, *, paths: list[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"mode": mode}
    if paths:
        result["paths"] = paths
    return result


def policy_from_legacy(config: dict[str, Any], tools: list[str]) -> dict[str, Any]:
    """Translate permission levels and selected tools into the new model."""
    permission = config.get("permissions", "read_only")
    selected = set(tools or [])
    all_actions = {cap.id: {"mode": "deny"} for cap in CAPABILITY_REGISTRY.values()}
    read_only = {"filesystem.list", "filesystem.read", "filesystem.search", "git.diff"}
    for capability in CAPABILITY_REGISTRY.values():
        if capability.tool not in selected:
            continue
        if capability.id in read_only:
            all_actions[capability.id] = {"mode": "allow"}
        elif capability.category == "filesystem" and permission in {"workspace", "execute"}:
            all_actions[capability.id] = {"mode": "allow"}
        elif capability.category in {"execution", "git"} and permission == "execute":
            all_actions[capability.id] = {"mode": "allow"}
    nested: dict[str, dict[str, Any]] = {}
    for action, rule in all_actions.items():
        category, name = action.split(".", 1)
        nested.setdefault(category, {})[name] = rule
    return validate_policy({"capabilities": nested})


class PolicyEngine:
    def __init__(self, policy: dict[str, Any], workspace: Path | str, hard_max_bytes: int | None = None):
        self.workspace = Path(workspace).resolve()
        self.hard_max_bytes = hard_max_bytes
        try:
            self.policy = validate_policy(policy)
            self.invalid_reason = ""
        except (TypeError, ValueError) as exc:
            self.policy = {"capabilities": {}}
            self.invalid_reason = str(exc)

    def evaluate(self, action: str, resource: str = "", context: dict[str, Any] | None = None) -> PolicyDecision:
        context = context or {}
        if action not in CAPABILITY_REGISTRY:
            return PolicyDecision("deny", "Unknown capability is denied.", action, resource)
        if self.invalid_reason:
            return PolicyDecision("deny", "Invalid capability policy: " + self.invalid_reason, action, resource)
        if resource:
            normalized = resource.replace("\\", "/")
            if not _path_ok(normalized):
                return PolicyDecision("deny", "Resource path is outside the workspace.", action, resource)
            candidate = (self.workspace / normalized).resolve()
            if candidate != self.workspace and self.workspace not in candidate.parents:
                return PolicyDecision("deny", "Resource path is outside the workspace.", action, resource)
            resource = PurePosixPath(normalized).as_posix()
        category, name = action.split(".", 1)
        rule = self.policy.get("capabilities", {}).get(category, {}).get(name)
        if not isinstance(rule, dict):
            return PolicyDecision("deny", f"{action} is not configured for this agent.", action, resource)
        mode = rule.get("mode")
        if mode not in MODES:
            return PolicyDecision("deny", "Invalid policy mode is denied.", action, resource)
        paths = rule.get("paths") or []
        if paths and resource and not any(self._match(resource, pattern) for pattern in paths):
            return PolicyDecision("deny", "Path is not permitted by policy.", action, resource)
        extensions = rule.get("extensions") or []
        extension = str(context.get("extension") or Path(resource).suffix).lower()
        if extensions and extension not in {str(item).lower() for item in extensions}:
            return PolicyDecision("deny", "File extension is not permitted by policy.", action, resource)
        size = context.get("size_bytes")
        maximum = rule.get("max_bytes")
        if isinstance(size, int) and maximum is not None and size > maximum:
            return PolicyDecision("deny", f"File exceeds policy max_bytes ({maximum}).", action, resource)
        if isinstance(size, int) and self.hard_max_bytes is not None and size > self.hard_max_bytes:
            return PolicyDecision("deny", "File exceeds the global write limit.", action, resource)
        return PolicyDecision("approval_required" if mode == "ask" else mode,
                              f"{action} policy mode is {mode} for {resource or 'this request'}.", action, resource)

    @staticmethod
    def _match(resource: str, pattern: str) -> bool:
        pattern = pattern.replace("\\", "/")
        if pattern in {"**", "**/*", "./**", "./**/*"}:
            return True
        if pattern.endswith("/**") and resource == pattern[:-3].rstrip("/"):
            return True
        return fnmatchcase(resource, pattern) or fnmatchcase("/" + resource, pattern)
