"""Parent-owned, bounded observation of the current evaluation state.

This module reads files only. It never executes commands or changes policy.
Execution facts come from completed Runtime records, superseded by stable
identity and execution order, never by a timestamp or an aggregate pass flag.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .security import sanitize
from .tools import Toolbox, MAX_FILE_BYTES
from .python_execution import INFRASTRUCTURE_ERRORS

SNAPSHOT_VERSION = 1
MAX_FILES = 100
MAX_CONTENT_CHARS = 12_000
MAX_SNAPSHOT_CHARS = 48_000


def runtime_ledger_rows(runtime: dict[str, Any]) -> list[dict[str, Any]]:
    """Recover completed command observations from the parent-owned event ledger.

    Events enrich their action by step ID. Diffs/readbacks only supply current
    observation targets; historical content is never accepted as final content.
    """
    starts: dict[str, dict[str, Any]] = {}
    rows: dict[str, dict[str, Any]] = {}
    for event in runtime.get("events", []):
        if not isinstance(event, dict):
            continue
        kind = event.get("event_type")
        step = str(event.get("step_id") or event.get("event_id") or "")
        if kind == "step.started" and step:
            starts[step] = event
        elif kind in {"step.finished", "verification.case_completed"}:
            prior = rows.get(step, {})
            row = {**starts.get(step, {}), **prior, **event}
            if kind == "step.finished" and prior.get("case_id"):
                # Real Runtime publishes case_completed before step.finished.
                # The latter's input is argv metadata, not the case's stdin.
                row.update({key: prior[key] for key in ("case_id", "input", "success", "status",
                    "exit_code", "stdout", "stderr", "supports_acceptance_criterion_ids") if key in prior})
            if kind == "verification.case_completed":
                row.update(type="command_execution", tool="run_command")
            if row.get("tool") != "run_command":
                continue
            arguments = row.get("arguments") or starts.get(step, {}).get("input") or {}
            if isinstance(arguments, dict):
                row.setdefault("command", arguments.get("argv", []))
            row["event_id"] = step
            if kind == "step.finished" and not prior.get("case_id"):
                row["success"] = event.get("status") == "Success"
                row["status"] = "passed" if row["success"] else "failed"
            rows[step or str(event.get("id") or len(rows))] = row
    return list(rows.values())


def evidence_fingerprint(snapshot: dict[str, Any], criterion_ids: list[str] | None = None) -> str:
    """Hash material current observations, excluding delivery/provenance IDs.

    Equivalent re-execution must not manufacture progress by changing a runtime
    ID, event ID, timestamp or evidence ordering.
    """
    rows = []
    for kind in ("files", "verification_facts"):
        for fact in snapshot.get(kind, []):
            supported = fact.get("supports_acceptance_criterion_ids", [])
            if kind == "verification_facts" and criterion_ids and supported and not set(supported) & set(criterion_ids):
                continue
            row = {key: fact[key] for key in (
                "path", "exists", "readable", "content_sha256", "content", "case_id", "input",
                "source_task_id", "command", "stdin_sha256", "kind", "check", "status", "exit_code",
                "stdout", "stderr", "output", "program_started", "environment_available",
                "error_class", "content_truncated", "supports_acceptance_criterion_ids") if key in fact}
            rows.append(json.dumps(row, sort_keys=True, ensure_ascii=False))
    return hashlib.sha256(json.dumps(sorted(set(rows)), ensure_ascii=False).encode()).hexdigest()


def criterion_paths(text: str) -> list[str]:
    """Extract named relative file targets, without interpreting their content."""
    return re.findall(r"(?<![\w./\\-])[\w./\\-]+\.[A-Za-z0-9]+(?![\w./\\-])", text)


def verification_kind(record: dict[str, Any]) -> str | None:
    descriptor = " ".join(str(record.get(key) or "") for key in
                          ("type", "check", "capability", "tool"))
    descriptor += " " + " ".join(record.get("command") or [])
    descriptor = descriptor.casefold()
    if re.search(r"pytest|unittest|test_result|\btests?\b", descriptor):
        return "test"
    if re.search(r"ruff|eslint|flake8|pylint|\blint\b", descriptor):
        return "lint"
    if re.search(r"py_compile|compilation|compile|\bbuild\b|\btsc\b", descriptor):
        return "compilation"
    if record.get("tool") == "run_command" or record.get("type") == "command_execution":
        return "command"
    if record.get("type") in {"visual_result", "external_state"}:
        return record["type"]
    return None


def _test_summary(output: str) -> dict[str, Any]:
    """Extract runner-reported counts and failed case IDs, never criterion truth."""
    summary: dict[str, Any] = {}
    for count, status in re.findall(r"\b(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed)\b", output):
        summary[status.rstrip("s") if status.startswith("error") else status] = int(count)
    ran = re.search(r"\bRan (\d+) tests?\b", output)
    if ran:
        summary["total"] = int(ran[1])
        failures = re.search(r"FAILED\s*\(([^)]+)\)", output)
        failed = dict(re.findall(r"(failures|errors)=(\d+)", failures[1])) if failures else {}
        if failures or re.search(r"^OK\s*$", output, re.M):
            summary["failed"] = int(failed.get("failures", 0))
            summary["error"] = int(failed.get("errors", 0))
            summary["passed"] = max(0, summary["total"] - summary["failed"] - summary["error"])
    elif "passed" in summary or "failed" in summary:
        summary["total"] = sum(summary.get(key, 0) for key in ("passed", "failed", "skipped", "error", "xfailed", "xpassed"))
    cases = re.findall(r"^FAILED\s+(\S+)(?:\s+-\s+(.*))?$", output, re.M)
    cases += re.findall(r"^(?:FAIL|ERROR):\s+([^\n]+)", output, re.M)
    summary["failed_tests"] = [
        {"id": value[0] if isinstance(value, tuple) else value,
         "detail": value[1] if isinstance(value, tuple) else "", "status": "failed"}
        for value in cases[:MAX_FILES]]
    return summary


def final_verification_facts(runtime_tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Last completed observation wins for a verification signature.

    A command signature includes argv and bounded stdin digest: distinct QA
    cases must not supersede each other. Explicit check/test IDs distinguish
    individual cases. Runtime event IDs are provenance, not check identities.
    """
    latest: dict[str, dict[str, Any]] = {}
    for runtime in runtime_tasks:
        result = runtime.get("result") if isinstance(runtime.get("result"), dict) else {}
        verification = runtime.get("verification") or result.get("verification") or {}
        evidence = verification.get("evidence", [])
        actions = result.get("actions", [])
        # Reusing prior independent cases is safe across read-only observations,
        # never across a subsequent mutation of the executable workspace. A
        # dependency module can change without appearing in command argv.
        mutated = bool(result.get("workspace_diffs")) or any(
            isinstance(action, dict) and action.get("success") is True and action.get("changed") is True
            and action.get("tool") in {"write_file", "edit_file"} for action in actions)
        mutated |= any(isinstance(event, dict) and event.get("event_type") == "workspace.diff"
                       for event in runtime.get("events", []))
        if mutated:
            latest = {identifier: fact for identifier, fact in latest.items()
                      if fact.get("kind") in {"visual_result", "external_state"}}
        # Verification rows enrich the same action event rather than replay it.
        enriched = {item.get("event_id"): item for item in evidence
                    if isinstance(item, dict) and item.get("event_id")}
        consumed = set()
        rows = []
        for action in actions:
            if not isinstance(action, dict) or action.get("tool") != "run_command":
                continue
            arguments = action.get("arguments") or {}
            row = {**action, "command": arguments.get("argv", []),
                   "stdin_sha256": action.get("stdin_sha256")}
            if arguments.get("stdin") is not None and not row.get("stdin_sha256"):
                row["stdin_identity_unavailable"] = True
            if action.get("event_id") in enriched:
                row.update(enriched[action["event_id"]])
                consumed.add(action["event_id"])
            rows.append(row)
        rows.extend(item for item in evidence if isinstance(item, dict)
                    and (not item.get("event_id") or item["event_id"] not in consumed))
        # Merge event metadata into the same observation, never create a second
        # proof for the same step. Case events are authoritative Runtime facts.
        by_event = {row.get("event_id"): index for index, row in enumerate(rows) if row.get("event_id")}
        for event_row in runtime_ledger_rows(runtime):
            index = by_event.get(event_row.get("event_id"))
            if index is None:
                rows.append(event_row)
            else:
                rows[index] = {**event_row, **rows[index], **{key: event_row[key] for key in
                    ("case_id", "input", "supports_acceptance_criterion_ids") if key in event_row}}
        for row in rows:
            kind = verification_kind(row)
            if not kind:
                continue
            command = row.get("command") or []
            explicit = row.get("case_id") or row.get("verification_id") or row.get("test_id") or row.get("check_id")
            identity = ([kind, "case", runtime.get("source_task_id"), explicit, command] if row.get("case_id") else
                        [kind, "id", explicit] if explicit else
                        [kind, "command", command, row.get("stdin_sha256")]
                        if command else [kind, "check", row.get("check") or row.get("id")])
            if row.get("stdin_identity_unavailable") and not explicit:
                identity.append([runtime.get("id"), row.get("event_id") or len(latest)])
            # Unidentified observations cannot safely supersede another check.
            if not command and not explicit and not (row.get("check") or row.get("id")):
                identity.append([runtime.get("id"), len(latest)])
            identifier = "F-" + hashlib.sha256(json.dumps(
                identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
            exit_code = row.get("exit_code")
            status = row.get("status") or (
                "passed" if row.get("success") is True else
                "failed" if row.get("success") is False else "unknown")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool) and status not in {"failed", "unavailable"}:
                status = "passed" if exit_code == 0 else "failed"
            if row.get("success") is False:
                status = "failed"
            if row.get("policy_decision") in {"deny", "denied"} or row.get("error_class") in {
                    *INFRASTRUCTURE_ERRORS, "policy_denied", "approval_denied", "unknown_tool"} or (
                    row.get("program_started") is False or row.get("environment_available") is False
                    or row.get("status") == "unavailable"):
                status = "unavailable"
            fact = {key: row[key] for key in (
                "check", "tool", "capability", "exit_code", "command", "path",
                "program_started", "environment_available",
                "test_id", "case_id", "input", "check_id", "verification_id", "event_id", "error_class",
                "stdin_sha256", "stdin_identity_unavailable", "supports_acceptance_criteria", "criterion_id",
                "supports_acceptance_criterion_ids", "source_task_id",
                "source_runtime_task_id", "worker_id", "passed", "failed", "total", "issues",
            ) if key in row}
            fact.update(id=identifier, kind=kind, status=status,
                        type={"test": "test_result", "lint": "lint_result",
                              "compilation": "compilation_result"}.get(kind, "command_execution" if kind == "command" else kind),
                        source="final_verification", source_runtime_task_id=runtime.get("id"),
                        source_task_id=runtime.get("source_task_id") or row.get("source_task_id"),
                        worker_id=runtime.get("config", {}).get(
                            "worker_assignment", {}).get("worker_id") or row.get("worker_id"))
            if isinstance(fact.get("input"), str) and not fact.get("stdin_sha256"):
                fact["stdin_sha256"] = hashlib.sha256(fact["input"].encode()).hexdigest()
            if status == "unavailable":
                fact.update(kind="execution_environment", type="execution_environment",
                            program_started=False, environment_available=False, exit_code=None,
                            verification_kind=kind)
            for field in ("stdout", "stderr", "output"):
                if isinstance(row.get(field), str):
                    value = sanitize(row[field])
                    fact[field] = value[:MAX_CONTENT_CHARS]
                    if len(value) > MAX_CONTENT_CHARS:
                        fact["content_truncated"] = True
            # Legacy records have merged output; do not invent separate streams.
            if "stdout" not in fact:
                fact["streams_unavailable"] = True
            if kind == "test":
                fact["test_summary"] = _test_summary(str(row.get("output") or
                                                        (row.get("stdout") or "") + (row.get("stderr") or "")))
            fact["content_truncated"] = bool(fact.get("content_truncated") or row.get("content_truncated"))
            latest[identifier] = sanitize(fact)
    return list(latest.values())


def build_final_state(planned_task: dict[str, Any], runtime_tasks: list[dict[str, Any]],
                      workspace: str | None) -> dict[str, Any]:
    """Capture relevant current files before calling the tool-free Evaluator."""
    targets = [*(planned_task.get("write_targets") or []),
               *(planned_task.get("owned_paths") or []), *(planned_task.get("read_targets") or [])]
    for criterion in planned_task.get("success_criteria", []):
        targets.extend(criterion_paths(criterion))
    # Historical paths are candidates only; their old contents/status are never used.
    for runtime in runtime_tasks:
        result = runtime.get("result") if isinstance(runtime.get("result"), dict) else {}
        for action in result.get("actions", []):
            if isinstance(action, dict) and isinstance(action.get("arguments"), dict):
                path = action["arguments"].get("path")
                if isinstance(path, str):
                    targets.append(path)
                argv = action['arguments'].get('argv')
                if action.get('tool') == 'run_command' and isinstance(argv, list) and argv:
                    executable = Path(str(argv[0]).replace('\\', '/')).name.lower().removesuffix('.exe')
                    if executable in {'python', 'python3'}:
                        script = next((arg for arg in argv[1:] if isinstance(arg, str)
                                       and not arg.startswith('-') and arg.lower().endswith('.py')), None)
                        if script:
                            targets.append(script)
        for item in [*(result.get("artifacts") or []), *(result.get("workspace_diffs") or []),
                     *((runtime.get("verification") or result.get("verification") or {}).get("evidence") or [])]:
            if isinstance(item, dict) and item.get("path"):
                targets.append(item["path"])
        for event in runtime.get("events", []):
            if isinstance(event, dict) and event.get("event_type") in {"workspace.diff", "step.finished"}:
                payload = event.get("output") if isinstance(event.get("output"), dict) else event
                if payload.get("path"):
                    targets.append(payload["path"])
                arguments = event.get("input") if isinstance(event.get("input"), dict) else {}
                if arguments.get("path"):
                    targets.append(arguments["path"])
    targets = list(dict.fromkeys(path for path in targets if isinstance(path, str)
                                and "runtime_envs" not in Path(path.replace("\\", "/")).parts))
    snapshot: dict[str, Any] = {"snapshot_version": SNAPSHOT_VERSION, "files": [],
                              "verification_facts": final_verification_facts(runtime_tasks),
                              "task_outputs": [], "context_truncated": len(targets) > MAX_FILES}
    for runtime in runtime_tasks:
        result = runtime.get("result")
        output = result.get("summary", "") if isinstance(result, dict) else result
        if isinstance(output, str) and output:
            snapshot["task_outputs"].append({"id": "final_output:" + str(runtime.get("id")),
                                             "source_runtime_task_id": runtime.get("id"),
                                             "source_task_id": runtime.get("source_task_id"),
                                             "content": sanitize(output)[:MAX_CONTENT_CHARS],
                                             "source": "agent_claim", "content_truncated": len(output) > MAX_CONTENT_CHARS})
    root = Path(workspace).resolve() if workspace else None
    # Reuse the tool boundary's path resolver without invoking tools or making folders.
    resolver = object.__new__(Toolbox)
    resolver.workspace = root
    budget = MAX_SNAPSHOT_CHARS
    for path in targets[:MAX_FILES]:
        record: dict[str, Any] = {"path": path, "exists": None, "readable": None,
                                  "source": "current_workspace"}
        try:
            if root is None or not root.is_dir():
                raise ValueError("Workspace unavailable for final observation.")
            target = resolver.safe_path(path)
            record["exists"] = target.is_file()
            record["readable"] = False
            if record["exists"]:
                with target.open("rb") as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                record["readable"] = True
                if len(data) > MAX_FILE_BYTES:
                    record["content_truncated"] = True
                else:
                    record["content_sha256"] = hashlib.sha256(data).hexdigest()
                try:
                    content = sanitize(data[:MAX_FILE_BYTES].decode("utf-8"))
                    limit = min(MAX_CONTENT_CHARS, budget)
                    record["content"] = content[:limit]
                    budget -= len(record["content"])
                    record["content_truncated"] = record.get("content_truncated", False) or len(content) > limit
                except UnicodeDecodeError:
                    record["content_unavailable"] = "binary_or_non_utf8"
        except (OSError, ValueError) as exc:
            record["observation_error"] = sanitize(str(exc))[:500]
        record["id"] = "final_file:" + path
        snapshot["files"].append(record)
        snapshot["context_truncated"] |= bool(record.get("content_truncated"))
    # Bound the durable snapshot too; omission must remain explicit.
    budget = MAX_SNAPSHOT_CHARS
    for key in ("files", "verification_facts", "task_outputs"):
        rows = snapshot[key]
        snapshot[key] = []
        for row in rows:
            for field in ("content", "stdout", "stderr", "output"):
                if isinstance(row.get(field), str):
                    length = len(row[field])
                    row[field] = row[field][:max(0, budget)]
                    budget = max(0, budget - len(row[field]))
                    if len(row[field]) < length:
                        row["content_truncated"] = True
                        snapshot["context_truncated"] = True
            if len(snapshot[key]) == MAX_FILES:
                snapshot["context_truncated"] = True
                break
            snapshot[key].append(row)
    snapshot["context_truncated"] |= any(row.get("content_truncated", False)
                                         for key in ("verification_facts", "task_outputs") for row in snapshot[key])
    return sanitize(snapshot)
