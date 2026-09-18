"""Deterministic, explainable selection of an existing agent for one planned task.

The selector is deliberately read-only.  It classifies and ranks candidates;
the capability policy remains the only authority that can permit execution.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Iterable

from .agent_context import build_effective_agent
from .capabilities import tool_for_capability
from .policy import PolicyEngine
from .skills import resolve_agent_skills


AGENT_SELECTOR_VERSION = 1

# Scoring is intentionally small, centralized and stable. Eligibility class is
# compared before score, so no amount of relevance can overcome a hard filter.
OPERATIONAL_PREFERRED_SKILL_POINTS = 20
NON_OPERATIONAL_PREFERRED_SKILL_POINTS = 4
ROLE_TOKEN_POINTS = 3
ROLE_TOPIC_POINTS = 5
MAX_ROLE_POINTS = 15
TASK_SKILL_RELEVANCE_POINTS = 2
MAX_TASK_SKILL_RELEVANCE_POINTS = 10
IDLE_POINTS = 10
APPROVAL_REQUIRED_PENALTY = 15
ACTIVE_TASK_PENALTY = 5

# ``enabled`` is the durable availability control. Agent ``status`` is an
# operational/ephemeral signal: temporary states affect ranking and warnings,
# but do not revoke eligibility or capability authorization.
TRANSIENT_STATUS_ADJUSTMENTS = {
    "running": 0,
    "waiting": -2,
    "paused": -8,
    "offline": -10,
    "error": -12,
}
TRANSIENT_STATUS_WARNINGS = {
    "waiting": "Agent is temporarily waiting.",
    "paused": "Agent is temporarily paused and may need to be resumed before submission.",
    "offline": "Agent is temporarily offline and may need runtime activation.",
    "error": "Agent currently reports an operational error state.",
}

ADMINISTRATIVE_UNUSABLE_STATUSES = {"disabled", "unavailable", "archived"}
CLASSIFICATION_ORDER = {"eligible": 0, "conditional": 1, "ineligible": 2}
STOP_WORDS = {
    "agent", "and", "con", "del", "for", "from", "las", "los", "para", "por",
    "the", "una", "uno", "use", "with", "work", "task", "request", "complete",
}
ROLE_TOPICS = {
    "backend": {"api", "auth", "authentication", "backend", "database", "django", "fastapi", "flask", "python", "server"},
    "frontend": {"browser", "css", "frontend", "html", "javascript", "react", "typescript", "ui", "web"},
    "network": {"dns", "firewall", "network", "routing", "tcp", "tls", "vpn"},
    "quality": {"debug", "debugging", "diagnose", "pytest", "quality", "test", "testing", "unittest", "verify"},
    "security": {"audit", "authorization", "credential", "permission", "policy", "security", "threat"},
    "data": {"analytics", "data", "dataset", "etl", "sql", "statistics"},
}


def _tokens(value: Any) -> set[str]:
    text = str(value or "").casefold().replace("_", " ").replace("-", " ")
    return {token for token in re.findall(r"[^\W_]{3,}", text, re.UNICODE)
            if token not in STOP_WORDS}


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")


def _workload(agent: dict[str, Any], context: dict[str, Any]) -> int:
    configured = context.get("workloads", {})
    value = configured.get(agent.get("id")) if isinstance(configured, dict) else None
    if value is None:
        value = agent.get("active_task_count")
    if value is None:
        value = 1 if agent.get("current_task") else 0
    return max(0, int(value)) if isinstance(value, int) and not isinstance(value, bool) else 0


def _ineligible(agent_id: str | None, warnings: list[str], *, workload: int = 0,
                capability_status: dict[str, list[str]] | None = None) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "eligible": False,
        "conditionally_eligible": False,
        "classification": "ineligible",
        "eligibility": "ineligible",
        "score": None,
        "workload": workload,
        "preferred_skill_matches": 0,
        "operational_preferred_skills": [],
        "non_operational_preferred_skills": [],
        "capability_status": capability_status or {
            "allowed": [], "approval_required": [], "denied": [], "runtime_unavailable": [],
        },
        "reasons": [],
        "warnings": warnings,
    }


class AgentSelector:
    """Classify and rank existing agents without mutating any input or policy."""

    version = AGENT_SELECTOR_VERSION

    def select_agent(self, task: dict[str, Any], agents: Iterable[dict[str, Any]],
                     context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(task, dict):
            raise ValueError("Planned task must be an object.")
        if isinstance(agents, (str, bytes)):
            raise ValueError("Agents must be an iterable of agent records.")
        task_copy = copy.deepcopy(task)
        agents_copy = copy.deepcopy(list(agents))
        context_copy = copy.deepcopy(context) if isinstance(context, dict) else {}
        seen_agent_ids: set[str] = set()
        for raw in agents_copy:
            raw_id = raw.get("id") if isinstance(raw, dict) else None
            if not isinstance(raw_id, str) or not raw_id.strip():
                continue
            agent_id = raw_id.strip()
            if agent_id in seen_agent_ids:
                raise ValueError("Duplicate agent id: " + agent_id)
            seen_agent_ids.add(agent_id)
        task_id = str(task_copy.get("id") or "").strip()
        required = task_copy.get("required_capabilities", [])
        preferred = task_copy.get("preferred_skills", [])
        if (not isinstance(required, list) or any(not isinstance(item, str) for item in required)
                or not isinstance(preferred, list) or any(not isinstance(item, str) for item in preferred)):
            raise ValueError("Planned task capability and Skill requirements must be lists of strings.")
        required = list(dict.fromkeys(item.strip() for item in required if item.strip()))
        preferred = list(dict.fromkeys(_slug(item) for item in preferred if _slug(item)))

        candidates = [self._candidate(task_copy, raw, context_copy, required, preferred)
                      for raw in agents_copy]
        candidates.sort(key=self._sort_key)
        selected = next((item for item in candidates if item["classification"] == "eligible"), None)
        if selected is None:
            selected = next((item for item in candidates
                             if item["classification"] == "conditional"), None)
        return {
            "task_id": task_id,
            "status": ("no_eligible_agent" if selected is None else
                       "approval_required" if selected["classification"] == "conditional" else
                       "selected"),
            "selected_agent_id": selected["agent_id"] if selected else None,
            "score": selected["score"] if selected else None,
            "classification": selected["classification"] if selected else "ineligible",
            "approval_required": bool(selected and selected["classification"] == "conditional"),
            "selector_version": self.version,
            "reasons": list(selected["reasons"]) if selected else [],
            "warnings": list(selected["warnings"]) if selected else ["No eligible or conditional agent is available."],
            "candidates": candidates,
        }

    @staticmethod
    def _sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
        score = candidate.get("score")
        return (
            CLASSIFICATION_ORDER[candidate["classification"]],
            -(score if isinstance(score, int) else -10**9),
            candidate.get("workload", 0),
            -candidate.get("preferred_skill_matches", 0),
            str(candidate.get("agent_id") or "").casefold(),
        )

    def _candidate(self, task: dict[str, Any], raw: Any, context: dict[str, Any],
                   required: list[str], preferred: list[str]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return _ineligible(None, ["Agent does not exist or is not a valid record."])
        agent = raw
        raw_agent_id = agent.get("id")
        agent_id = (raw_agent_id.strip() if isinstance(raw_agent_id, str)
                    and raw_agent_id.strip() else None)
        workload = _workload(agent, context)
        hard_warnings: list[str] = []
        if agent_id is None:
            hard_warnings.append("Agent does not have a valid id.")
        if agent.get("enabled") is not True:
            hard_warnings.append("Agent is disabled.")
        if agent.get("archived") is True or agent.get("deleted_at"):
            hard_warnings.append("Agent is archived.")
        status = str(agent.get("status", agent.get("availability", "Offline"))).strip()
        status_key = status.casefold()
        if status_key in ADMINISTRATIVE_UNUSABLE_STATUSES:
            hard_warnings.append(f"Agent status {status or 'Offline'} is not usable.")
        if agent.get("usable") is False:
            hard_warnings.append("Agent is explicitly marked as not usable.")
        compatibility = context.get("workspace_compatibility", {})
        if agent.get("workspace_compatible") is False or (
                isinstance(compatibility, dict) and agent_id in compatibility
                and compatibility[agent_id] is False):
            hard_warnings.append("Agent is incompatible with the selected workspace.")
        try:
            effective = build_effective_agent(agent)
        except (KeyError, TypeError, ValueError) as exc:
            hard_warnings.append("Agent configuration is invalid: " + str(exc))
            return _ineligible(agent_id, hard_warnings, workload=workload)

        workspace = context.get("workspace_path") or effective.get("config", {}).get("workspace_path") or "."
        engine = PolicyEngine(effective["capability_policy"], workspace)
        capability_status = {"allowed": [], "approval_required": [], "denied": [],
                             "runtime_unavailable": []}
        runtime_tools = context.get("runtime_tools")
        available_tools = (set(runtime_tools) if isinstance(runtime_tools, (list, tuple, set))
                           else set(effective.get("tools", [])))
        for capability in required:
            decision = engine.evaluate(capability)
            if decision.outcome == "allow":
                capability_status["allowed"].append(capability)
            elif decision.outcome == "approval_required":
                capability_status["approval_required"].append(capability)
            else:
                capability_status["denied"].append(capability)
                hard_warnings.append(f"Required capability {capability} is denied.")
                continue
            tool = tool_for_capability(capability)
            if tool and tool not in available_tools:
                capability_status["runtime_unavailable"].append(capability)
                hard_warnings.append(
                    f"Required runtime tool {tool} for {capability} is unavailable."
                )

        if capability_status["runtime_unavailable"]:
            # Runtime support is a hard constraint even when policy says allow/ask.
            hard_warnings = list(dict.fromkeys(hard_warnings))
        if hard_warnings or capability_status["denied"] or capability_status["runtime_unavailable"]:
            return _ineligible(agent_id, list(dict.fromkeys(hard_warnings)), workload=workload,
                               capability_status=capability_status)

        task_text = " ".join(str(task.get(key) or "") for key in
                             ("objective", "description", "preferred_role"))
        skills = resolve_agent_skills(
            agent, effective.get("skills", []), task_text,
            policy=effective["capability_policy"], available_tools=effective.get("tools", []),
        )
        skill_by_key: dict[str, list[dict[str, Any]]] = {}
        for skill in skills:
            for key in {_slug(skill.get("id")), _slug(skill.get("name"))}:
                if key:
                    skill_by_key.setdefault(key, []).append(skill)
        operational_matches: list[str] = []
        non_operational_matches: list[str] = []
        for preferred_id in preferred:
            matches = skill_by_key.get(preferred_id, [])
            if any(skill.get("enabled") and skill.get("operational") for skill in matches):
                operational_matches.append(preferred_id)
            elif matches:
                non_operational_matches.append(preferred_id)

        identity = effective["identity"]
        identity_text = " ".join([
            identity.get("role", ""), identity.get("purpose", ""),
            identity.get("description", ""), *identity.get("responsibilities", []),
        ])
        task_tokens, identity_tokens = _tokens(task_text), _tokens(identity_text)
        shared_tokens = task_tokens & identity_tokens
        shared_topics = [name for name, terms in ROLE_TOPICS.items()
                         if task_tokens & terms and identity_tokens & terms]
        role_points = min(MAX_ROLE_POINTS,
                          len(shared_tokens) * ROLE_TOKEN_POINTS + len(shared_topics) * ROLE_TOPIC_POINTS)

        operational_skill_tokens: set[str] = set()
        for skill in skills:
            if skill.get("operational") and skill.get("enabled"):
                operational_skill_tokens |= _tokens(" ".join([
                    skill.get("id", ""), skill.get("name", ""), skill.get("category", ""),
                    *skill.get("tags", []),
                ]))
        relevant_skill_tokens = task_tokens & operational_skill_tokens
        relevance_points = min(MAX_TASK_SKILL_RELEVANCE_POINTS,
                               len(relevant_skill_tokens) * TASK_SKILL_RELEVANCE_POINTS)

        classification = ("conditional" if capability_status["approval_required"] else "eligible")
        status_adjustment = TRANSIENT_STATUS_ADJUSTMENTS.get(status_key, 0)
        score = (
            len(operational_matches) * OPERATIONAL_PREFERRED_SKILL_POINTS
            + len(non_operational_matches) * NON_OPERATIONAL_PREFERRED_SKILL_POINTS
            + role_points + relevance_points
            + (IDLE_POINTS if status.casefold() == "idle" and workload == 0 else 0)
            + status_adjustment
            - len(capability_status["approval_required"]) * APPROVAL_REQUIRED_PENALTY
            - workload * ACTIVE_TASK_PENALTY
        )
        reasons: list[str] = []
        warnings: list[str] = []
        if required:
            reasons.append(
                f"Required capabilities: {len(capability_status['allowed'])} allowed, "
                f"{len(capability_status['approval_required'])} require approval, "
                f"{len(capability_status['denied'])} denied."
            )
        else:
            reasons.append("No required capabilities were declared.")
        if capability_status["approval_required"]:
            warnings.append("Approval is required for: " + ", ".join(
                capability_status["approval_required"]))
        if operational_matches:
            reasons.append(f"{len(operational_matches)} operational preferred Skills matched.")
        if non_operational_matches:
            warnings.append("Preferred Skills are assigned but non-operational: " +
                            ", ".join(non_operational_matches))
        if preferred and not operational_matches and not non_operational_matches:
            warnings.append("No preferred Skills matched.")
        if role_points:
            reasons.append("Agent role and identity are relevant to the task.")
        if relevance_points:
            reasons.append("Operational Skill metadata is relevant to the task.")
        if status_key == "idle" and workload == 0:
            reasons.append("Agent is idle.")
        elif workload:
            warnings.append(f"Agent has {workload} active task(s).")
        status_warning = TRANSIENT_STATUS_WARNINGS.get(status_key)
        if status_warning:
            warnings.append(status_warning)
        return {
            "agent_id": agent_id,
            "eligible": classification == "eligible",
            "conditionally_eligible": classification == "conditional",
            "classification": classification,
            "eligibility": classification,
            "score": score,
            "workload": workload,
            "preferred_skill_matches": len(operational_matches),
            "operational_preferred_skills": operational_matches,
            "non_operational_preferred_skills": non_operational_matches,
            "capability_status": capability_status,
            "reasons": reasons,
            "warnings": warnings,
        }


def select_agent(task: dict[str, Any], agents: Iterable[dict[str, Any]],
                 context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Convenience API for callers that do not need a reusable selector."""
    return AgentSelector().select_agent(task, agents, context)
