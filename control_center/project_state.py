"""Deterministic, orchestration-scoped project context.

Workers receive bounded snapshots and can query them through a read-only tool.
Only the scheduler persists changes, and task candidates become canonical after
the Evaluator accepts the corresponding execution.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from .cross_task import CrossTaskRequestError, normalize_owned_path, owned_path_key
from .security import sanitize
from .storage import utcnow


PROJECT_STATE_SCHEMA_VERSION = 1
IGNORED_DIRECTORIES = {".git", ".venv", "__pycache__", "node_modules", ".mypy_cache"}
MAX_HASH_BYTES = 1_000_000
MAX_TEXT_BYTES = 1_000_000
MAX_QUERY_RESULTS = 30
MAX_QUERY_CHARS = 8_000
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _is_reparse_or_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
    return stat.S_ISLNK(info.st_mode) or reparse


def _file_metadata(path: Path) -> tuple[str | None, int | None]:
    """Hash a bounded regular file without following links or storing contents."""
    if _is_reparse_or_link(path):
        return None, None
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_HASH_BYTES:
            return None, info.st_size
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_HASH_BYTES:
                    return None, info.st_size
                digest.update(chunk)
        return digest.hexdigest(), info.st_size
    except OSError:
        return None, None


def _plan_owners(plan: dict[str, Any]) -> dict[str, str]:
    owners = plan.get("write_owners")
    if isinstance(owners, dict):
        result: dict[str, str] = {}
        for path, task_id in owners.items():
            try:
                result[owned_path_key(path)] = str(task_id)
            except (TypeError, ValueError, CrossTaskRequestError):
                continue
        return result
    result = {}
    for task in plan.get("tasks", []):
        if not isinstance(task, dict):
            continue
        for path in task.get("owned_paths", []):
            try:
                key = owned_path_key(path)
            except (TypeError, ValueError, CrossTaskRequestError):
                continue
            result[key] = str(task.get("id") or "")
    return result


def _artifact_key(path: str) -> str:
    return owned_path_key(path)


def _normalize_graph_status(value: Any) -> str:
    raw = str(value or "pending").casefold()
    mapping = {
        "pending": "pending", "ready": "ready", "selected": "ready",
        "running": "running", "evaluating": "evaluating",
        "recovery_pending": "waiting", "waiting": "waiting",
        "succeeded": "accepted", "success": "accepted",
        "failed": "failed", "blocked": "blocked", "cancelled": "cancelled",
    }
    return mapping.get(raw, "pending")


def _safe_text(value: Any, limit: int = 800) -> str:
    return sanitize(str(value or "").strip())[:limit]


class ProjectStateManager:
    """Owns deterministic project state updates and worker-facing snapshots."""

    def __init__(self, store: Any, *, max_manifest_files: int = 500,
                 max_manifest_depth: int = 8, max_snapshot_artifacts: int = 500,
                 max_prompt_chars: int = 12_000) -> None:
        for name, value, low, high in (
            ("max_manifest_files", max_manifest_files, 1, 5_000),
            ("max_manifest_depth", max_manifest_depth, 1, 32),
            ("max_snapshot_artifacts", max_snapshot_artifacts, 1, 5_000),
            ("max_prompt_chars", max_prompt_chars, 1_000, 64_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}.")
        self.store = store
        self.max_manifest_files = max_manifest_files
        self.max_manifest_depth = max_manifest_depth
        self.max_snapshot_artifacts = max_snapshot_artifacts
        self.max_prompt_chars = max_prompt_chars

    @staticmethod
    def _root(workspace: str | Path) -> Path:
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Project workspace must be an existing directory.")
        return root

    def _manifest(self, root: Path) -> tuple[dict[str, dict[str, Any]], bool]:
        artifacts: dict[str, dict[str, Any]] = {}
        queue: deque[tuple[Path, int]] = deque([(root, 0)])
        truncated = False
        while queue:
            directory, depth = queue.popleft()
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
            except OSError:
                continue
            for entry_index, entry in enumerate(entries):
                path = Path(entry.path)
                if _is_reparse_or_link(path):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if depth < self.max_manifest_depth and entry.name not in IGNORED_DIRECTORIES:
                            queue.append((path, depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                try:
                    relative = normalize_owned_path(path.relative_to(root).as_posix())
                except (ValueError, CrossTaskRequestError):
                    continue
                digest, size = _file_metadata(path)
                key = _artifact_key(relative)
                artifacts[key] = {
                    "path": relative, "status": "present" if digest else "unverified",
                    "sha256": digest, "size_bytes": size, "revision": 1,
                    "owner_plan_task_id": None, "owner_agent_id": None,
                    "purpose": "", "verification": {
                        "status": "manifested" if digest else "unverified",
                        "verified_hash": digest, "verified_at": utcnow() if digest else "",
                    }, "symbols": [], "dependencies": [],
                    "last_modified_by_task": "", "last_modified_by_agent": "",
                }
                if len(artifacts) >= self.max_manifest_files:
                    truncated = bool(queue) or entry_index < len(entries) - 1
                    return artifacts, truncated
        return artifacts, truncated

    @staticmethod
    def _initial_tasks(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for task in plan.get("tasks", []):
            if not isinstance(task, dict) or not isinstance(task.get("id"), str):
                continue
            result[task["id"]] = {
                "task_id": task["id"],
                "objective": _safe_text(task.get("objective"), 600),
                "description": _safe_text(task.get("description"), 800),
                "depends_on": [str(item)[:100] for item in task.get("depends_on", [])[:30]
                               if isinstance(item, str)],
                "status": "pending", "artifact_paths": [], "updated_revision": 0,
            }
        return result

    def initialize(self, orchestration_id: str, plan: dict[str, Any],
                    workspace: str | Path) -> dict[str, Any]:
        existing = self.store.get_project_state(orchestration_id)
        if existing is not None:
            return existing
        root = self._root(workspace)
        artifacts, truncated = self._manifest(root)
        owners = _plan_owners(plan)
        owner_paths: dict[str, str] = {}
        for planned in plan.get("tasks", []):
            if not isinstance(planned, dict):
                continue
            for path in planned.get("owned_paths", []):
                try:
                    owner_paths[_artifact_key(path)] = normalize_owned_path(path)
                except (TypeError, ValueError, CrossTaskRequestError):
                    continue
        manifest_count = len(artifacts)
        for key, task_id in owners.items():
            try:
                relative = owner_paths.get(key) or normalize_owned_path(key)
            except (ValueError, CrossTaskRequestError):
                continue
            artifact = artifacts.get(key)
            if artifact is None:
                target = self._safe_artifact_path(root, relative)
                digest, size = _file_metadata(target) if target is not None and target.is_file() else (None, None)
                artifacts[key] = {
                    "path": relative, "status": "present" if digest else "planned", "sha256": digest,
                    "size_bytes": size, "revision": 1 if digest else 0,
                    "owner_plan_task_id": task_id, "owner_agent_id": None,
                    "purpose": "", "verification": {
                        "status": "manifested" if digest else "not_created",
                        "verified_hash": digest, "verified_at": utcnow() if digest else "",
                    }, "symbols": [], "dependencies": [],
                    "last_modified_by_task": "", "last_modified_by_agent": "",
                }
            else:
                artifact["owner_plan_task_id"] = task_id
        state = {
            "schema_version": PROJECT_STATE_SCHEMA_VERSION,
            "orchestration_id": orchestration_id,
            "revision": 0,
            "workspace_identity": hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest(),
            "manifest": {"file_count": manifest_count, "truncated": truncated,
                         "max_files": self.max_manifest_files,
                         "max_depth": self.max_manifest_depth},
            "artifacts": artifacts,
            "tasks": self._initial_tasks(plan),
            "created_at": utcnow(), "updated_at": utcnow(),
        }
        event = {
            "event_type": "project_context.initialized", "status": "Success",
            "phase": "project_context", "actor_type": "runtime",
            "actor_role": "orchestrator", "workspace": str(root),
            "artifact_count": len(artifacts), "manifest_truncated": truncated,
            "message": "Initialized the bounded per-orchestration project manifest.",
        }
        saved = self.store.commit_project_state(
            orchestration_id, expected_revision=None, state=state,
            events=[event], update_state=True,
        )
        return saved or self.store.get_project_state(orchestration_id) or state

    @staticmethod
    def _safe_artifact_path(root: Path, relative: str) -> Path | None:
        try:
            normalized = normalize_owned_path(relative)
        except (ValueError, CrossTaskRequestError):
            return None
        target = root.joinpath(*normalized.split("/"))
        current = root
        for part in normalized.split("/"):
            current = current / part
            if _is_reparse_or_link(current):
                return None
        try:
            resolved = target.resolve(strict=False)
        except OSError:
            return None
        if resolved != root and root not in resolved.parents:
            return None
        return target

    def _refresh_state(self, state: dict[str, Any], root: Path) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for artifact in state.get("artifacts", {}).values():
            path = self._safe_artifact_path(root, str(artifact.get("path") or ""))
            if path is None or not path.is_file():
                if artifact.get("status") not in {"missing", "planned"}:
                    artifact["revision"] = int(artifact.get("revision", 0)) + 1
                    artifact["status"] = "missing"
                    artifact["verification"] = {
                        "status": "stale", "verified_hash": artifact.get("sha256"),
                        "verified_at": "", "reason": "artifact_missing",
                    }
                    for symbol in artifact.get("symbols", []):
                        symbol["verified"] = False
                        symbol["status"] = "reported_unverified"
                    events.append({
                        "event_type": "artifact.stale", "status": "Warning",
                        "phase": "project_context", "actor_type": "runtime",
                        "path": artifact.get("path"), "stale_reason": "artifact_missing",
                        "revision": artifact["revision"],
                        "message": "A tracked artifact is no longer present in the workspace.",
                    })
                continue
            previous_status = artifact.get("status")
            digest, size = _file_metadata(path)
            if digest is None:
                if artifact.get("status") != "unverified":
                    had_verified_hash = bool(artifact.get("sha256"))
                    if had_verified_hash:
                        artifact["revision"] = int(artifact.get("revision", 0)) + 1
                    artifact["status"] = "unverified"
                    artifact["verification"] = {
                        "status": "unverified", "verified_hash": None,
                        "verified_at": "", "reason": "hash_limit_or_read_error",
                    }
                    artifact["size_bytes"] = size
                    for symbol in artifact.get("symbols", []):
                        symbol["verified"] = False
                        symbol["status"] = "reported_unverified"
                    if had_verified_hash:
                        events.append({
                            "event_type": "artifact.stale", "status": "Warning",
                            "phase": "project_context", "actor_type": "runtime",
                            "path": artifact.get("path"), "revision": artifact["revision"],
                            "stale_reason": "artifact_hash_unavailable",
                            "message": "A tracked artifact could not be rehashed within the verification limit.",
                        })
                continue
            if artifact.get("sha256") and artifact.get("sha256") != digest:
                previous_hash = artifact.get("sha256")
                artifact["revision"] = int(artifact.get("revision", 0)) + 1
                artifact["sha256"] = digest
                artifact["size_bytes"] = size
                artifact["status"] = "stale"
                artifact["verification"] = {
                    "status": "stale", "verified_hash": previous_hash,
                    "verified_at": "", "reason": "content_changed_since_verification",
                }
                for symbol in artifact.get("symbols", []):
                    if symbol.get("verified_hash") != digest:
                        symbol["verified"] = False
                        symbol["status"] = "reported_unverified"
                events.append({
                    "event_type": "artifact.stale", "status": "Warning",
                    "phase": "project_context", "actor_type": "runtime",
                    "path": artifact.get("path"), "revision": artifact["revision"],
                    "message": "A tracked artifact changed after its last accepted verification.",
                })
            elif artifact.get("sha256") is None:
                artifact["sha256"] = digest
                artifact["size_bytes"] = size
                artifact["revision"] = max(1, int(artifact.get("revision", 0)))
                if previous_status in {"planned", "unverified"}:
                    artifact["status"] = "present_unverified"
                    artifact["verification"] = {
                        "status": "unverified", "verified_hash": None,
                        "verified_at": "", "reason": "hash_available_without_semantic_acceptance",
                    }
            elif previous_status == "missing":
                artifact["revision"] = int(artifact.get("revision", 0)) + 1
                artifact["status"] = "stale"
                events.append({
                    "event_type": "artifact.stale", "status": "Warning",
                    "phase": "project_context", "actor_type": "runtime",
                    "path": artifact.get("path"), "revision": artifact["revision"],
                    "stale_reason": "artifact_restored_after_missing",
                    "message": "A previously missing tracked artifact reappeared and needs acceptance.",
                })
            elif (previous_status == "unverified"
                  and (artifact.get("verification") or {}).get("status") != "verified"):
                artifact["status"] = "present_unverified"
                artifact["verification"] = {
                    "status": "unverified", "verified_hash": None,
                    "verified_at": "", "reason": "hash_available_without_semantic_acceptance",
                }
            artifact["size_bytes"] = size
        return events

    @staticmethod
    def _sync_task_statuses(state: dict[str, Any], plan: dict[str, Any],
                            graph: list[dict[str, Any]] | None) -> bool:
        changed = False
        graph_by_id = {
            str(item.get("plan_task_id") or item.get("task_id") or item.get("id") or ""): item
            for item in (graph or []) if isinstance(item, dict)
        }
        tasks = state.setdefault("tasks", {})
        for planned in plan.get("tasks", []):
            if not isinstance(planned, dict) or not isinstance(planned.get("id"), str):
                continue
            key = planned["id"]
            record = tasks.setdefault(key, {
                "task_id": key, "objective": _safe_text(planned.get("objective"), 600),
                "description": _safe_text(planned.get("description"), 800),
                "depends_on": list(planned.get("depends_on") or []),
                "status": "pending", "artifact_paths": [], "updated_revision": 0,
            })
            node = graph_by_id.get(key)
            if node is None:
                continue
            candidate = _normalize_graph_status(node.get("state") or node.get("status"))
            if record.get("status") != candidate:
                record["status"] = candidate
                changed = True
        return changed

    @staticmethod
    def _task_context_score(task: dict[str, Any], artifact: dict[str, Any]) -> int:
        path_key = _artifact_key(str(artifact.get("path") or ""))
        own = {_artifact_key(path) for path in task.get("owned_paths", [])
               if isinstance(path, str)}
        foreign_targets: set[str] = set()
        for target in task.get("foreign_write_targets", []):
            if not isinstance(target, dict) or not isinstance(target.get("path"), str):
                continue
            try:
                foreign_targets.add(_artifact_key(target["path"]))
            except (TypeError, ValueError, CrossTaskRequestError):
                continue
        if path_key in foreign_targets:
            return 110
        if path_key in own:
            return 100
        owner = str(artifact.get("owner_plan_task_id") or "")
        deps = {str(value) for value in task.get("depends_on", [])}
        if owner and owner in deps:
            return 80
        terms = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", " ".join([
            str(task.get("objective") or ""), str(task.get("description") or ""),
            *[str(item) for item in task.get("success_criteria", [])],
        ]).casefold()))
        labels = " ".join([str(artifact.get("path") or ""), str(artifact.get("purpose") or "")]
                          + [str(symbol.get("name") or "") for symbol in artifact.get("symbols", [])]).casefold()
        if any(term in labels for term in terms):
            return 60
        if artifact.get("status") in {"stale", "missing", "present_unverified"}:
            return 40
        return 10

    def snapshot_for_dispatch(self, orchestration_id: str, plan: dict[str, Any],
                              task: dict[str, Any], workspace: str | Path,
                              graph: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], str]:
        root = self._root(workspace)
        if self.store.get_project_state(orchestration_id) is None:
            self.initialize(orchestration_id, plan, root)
        task_id = str(task.get("id") or task.get("plan_task_id") or "")
        for _ in range(5):
            current = self.store.get_project_state(orchestration_id)
            if current is None:
                self.initialize(orchestration_id, plan, root)
                current = self.store.get_project_state(orchestration_id)
            if current is None:
                raise RuntimeError("Project state could not be initialized.")
            original = json.dumps(current, sort_keys=True, ensure_ascii=False)
            state = json.loads(original)
            refresh_events = self._refresh_state(state, root)
            task_changed = self._sync_task_statuses(state, plan, graph)
            before_revision = int(current.get("revision", 0))
            state["updated_at"] = utcnow()
            changed = task_changed or bool(refresh_events)
            if not changed:
                state["updated_at"] = current.get("updated_at") or state["updated_at"]
                state["revision"] = before_revision
            else:
                state["revision"] = before_revision + 1
            event = {
                "event_type": "project_context.snapshot_generated", "status": "Success",
                "phase": "project_context", "actor_type": "runtime",
                "actor_role": "orchestrator", "workspace": str(root),
                "plan_task_id": task_id, "state_revision": state["revision"],
                "message": "Generated a bounded current project-context snapshot for dispatch.",
            }
            saved = self.store.commit_project_state(
                orchestration_id, expected_revision=before_revision,
                state=state, events=[*refresh_events, event], update_state=changed,
            )
            if saved is None:
                continue
            snapshot = self._bounded_snapshot(saved, task, plan)
            rendered = self._render_prompt_context(snapshot)
            return snapshot, rendered
        raise RuntimeError("Project state changed repeatedly while a dispatch snapshot was prepared.")

    def _bounded_snapshot(self, state: dict[str, Any], task: dict[str, Any],
                          plan: dict[str, Any]) -> dict[str, Any]:
        artifacts = list((state.get("artifacts") or {}).values())
        artifacts.sort(key=lambda item: (
            -self._task_context_score(task, item),
            str(item.get("path") or "").casefold(),
        ))
        selected = artifacts[:self.max_snapshot_artifacts]
        selected_keys = {_artifact_key(str(item.get("path") or "")) for item in selected}
        task_id = str(task.get("id") or task.get("plan_task_id") or "")
        plan_tasks = {item.get("id"): item for item in plan.get("tasks", [])
                      if isinstance(item, dict)}
        related_ids = {task_id}
        related_ids.update(str(item) for item in (task.get("depends_on") or []) if isinstance(item, str))
        task_rows = []
        for related_id in sorted(related_ids):
            planned = plan_tasks.get(related_id, {})
            state_task = (state.get("tasks") or {}).get(related_id, {})
            if planned or state_task:
                task_rows.append({
                    "task_id": related_id,
                    "objective": _safe_text(planned.get("objective") or state_task.get("objective"), 500),
                    "status": str(state_task.get("status") or "pending"),
                    "depends_on": list(state_task.get("depends_on") or planned.get("depends_on") or [])[:20],
                })
        return sanitize({
            "schema_version": PROJECT_STATE_SCHEMA_VERSION,
            "orchestration_id": state.get("orchestration_id"),
            "revision": int(state.get("revision", 0)),
            "plan_task_id": task_id,
            "manifest": state.get("manifest") or {},
            "artifacts": [item for item in selected if _artifact_key(str(item.get("path") or "")) in selected_keys],
            "total_artifact_count": len(artifacts),
            "snapshot_truncated": len(artifacts) > len(selected),
            "tasks": task_rows,
            "generated_at": utcnow(),
        })

    def _render_prompt_context(self, snapshot: dict[str, Any]) -> str:
        lines = [
            "PROJECT CONTEXT (accepted Freya state; metadata only):",
            f"Project revision: {snapshot.get('revision', 0)}; artifacts shown: "
            f"{len(snapshot.get('artifacts', []))}/{snapshot.get('total_artifact_count', 0)}.",
            "Use project_context to query artifact, symbol, task, or summary metadata.",
            "Before relying on a file, read its current contents. Read an existing owned file "
            "again immediately before changing it; a stale read is blocked until refreshed.",
        ]
        for task in snapshot.get("tasks", []):
            lines.append("Task {task_id}: status={status}; depends_on={depends_on}; objective={objective}".format(
                task_id=task.get("task_id"), status=task.get("status"),
                depends_on=", ".join(task.get("depends_on", [])) or "none",
                objective=task.get("objective") or "(no objective recorded)",
            ))
        for artifact in snapshot.get("artifacts", []):
            verification = artifact.get("verification") or {}
            lines.append(
                "Artifact {path}: status={status}; revision={revision}; owner_task={owner}; "
                "verification={verification}; purpose={purpose}".format(
                    path=artifact.get("path"), status=artifact.get("status"),
                    revision=artifact.get("revision", 0),
                    owner=artifact.get("owner_plan_task_id") or "external/unassigned",
                    verification=verification.get("status", "unknown"),
                    purpose=_safe_text(artifact.get("purpose"), 240) or "(purpose not recorded)",
                )
            )
            for symbol in artifact.get("symbols", [])[:12]:
                lines.append("  Symbol {name}{signature}: {status}; {purpose}".format(
                    name=symbol.get("name"),
                    signature=(" " + str(symbol.get("signature"))) if symbol.get("signature") else "",
                    status="verified" if symbol.get("verified") else "reported, unverified",
                    purpose=_safe_text(symbol.get("purpose"), 180),
                ))
        if snapshot.get("snapshot_truncated"):
            lines.append("Additional artifacts are available through project_context search.")
        rendered = "\n".join(lines)
        if len(rendered) > self.max_prompt_chars:
            rendered = rendered[:self.max_prompt_chars - 32] + "\n[project context clipped]"
        return rendered

    def accept_task_update(self, orchestration_id: str, plan: dict[str, Any],
                           planned_task: dict[str, Any], runtime_task: dict[str, Any],
                           selected_agent_id: str | None = None,
                           mark_task_accepted: bool = True) -> dict[str, Any] | None:
        """Commit only runtime-grounded candidates after semantic acceptance."""
        workspace = runtime_task.get("workspace")
        root = self._root(workspace)
        task_id = str(planned_task.get("id") or "")
        result = runtime_task.get("result")
        result = result if isinstance(result, dict) else {}
        candidate = result.get("project_context_update")
        candidate = candidate if isinstance(candidate, dict) else {}
        writes = result.get("artifacts")
        writes = writes if isinstance(writes, list) else []
        owner_index = _plan_owners(plan)
        actual: dict[str, dict[str, Any]] = {}
        for item in writes[:100]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            try:
                path = normalize_owned_path(item["path"])
                key = _artifact_key(path)
            except (TypeError, ValueError, CrossTaskRequestError):
                continue
            if owner_index.get(key) != task_id:
                continue
            target = self._safe_artifact_path(root, path)
            if target is None or not target.is_file():
                continue
            digest, size = _file_metadata(target)
            if digest is None:
                continue
            actual[key] = {"path": path, "change_type": str(item.get("change_type") or "modified"),
                           "sha256": digest, "size_bytes": size}
        candidate_artifacts: dict[str, dict[str, Any]] = {}
        for item in candidate.get("artifacts", [])[:100] if isinstance(candidate.get("artifacts"), list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            try:
                candidate_artifacts[_artifact_key(item["path"])] = item
            except (TypeError, ValueError, CrossTaskRequestError):
                continue
        invalid_claims = sorted(set(candidate_artifacts) - set(actual))
        symbols = candidate.get("symbols", []) if isinstance(candidate.get("symbols"), list) else []
        dependencies = candidate.get("dependencies", []) if isinstance(candidate.get("dependencies"), list) else []
        for _attempt in range(5):
            current = self.store.get_project_state(orchestration_id)
            if current is None:
                self.initialize(orchestration_id, plan, root)
                current = self.store.get_project_state(orchestration_id)
            if current is None:
                raise RuntimeError("Project state is missing for accepted task update.")
            before_revision = int(current.get("revision", 0))
            state = json.loads(json.dumps(current, ensure_ascii=False))
            events: list[dict[str, Any]] = []
            changed = False
            task_row = state.setdefault("tasks", {}).setdefault(task_id, {
                "task_id": task_id, "objective": _safe_text(planned_task.get("objective"), 600),
                "description": _safe_text(planned_task.get("description"), 800),
                "depends_on": list(planned_task.get("depends_on") or []),
                "status": "pending", "artifact_paths": [], "updated_revision": 0,
            })
            if mark_task_accepted:
                old_task_status = task_row.get("status")
                old_runtime_task_id = task_row.get("last_runtime_task_id")
                task_row.update(status="accepted", last_runtime_task_id=runtime_task.get("id"),
                                last_agent_id=selected_agent_id or runtime_task.get("agent_id"),
                                accepted_at=utcnow())
                changed = (old_task_status != "accepted"
                           or old_runtime_task_id != runtime_task.get("id"))
            else:
                owner_actions = task_row.setdefault("accepted_owner_actions", [])
                action_id = str(runtime_task.get("id") or "")
                if action_id not in owner_actions:
                    owner_actions.append(action_id)
                changed = True
            for key, evidence in actual.items():
                previous = state.setdefault("artifacts", {}).get(key)
                is_new = previous is None or previous.get("status") == "planned"
                if previous is None:
                    previous = {
                        "path": evidence["path"], "revision": 0,
                        "owner_plan_task_id": task_id, "owner_agent_id": None,
                        "purpose": "", "symbols": [], "dependencies": [],
                    }
                if previous.get("owner_plan_task_id") != task_id:
                    # A runtime action cannot transfer permanent plan ownership.
                    continue
                prior_hash = previous.get("sha256")
                artifact_candidate = candidate_artifacts.get(key, {})
                previous.update({
                    "path": evidence["path"], "status": "verified",
                    "sha256": evidence["sha256"], "size_bytes": evidence["size_bytes"],
                    "revision": max(0, int(previous.get("revision", 0))) +
                    (1 if prior_hash != evidence["sha256"] else 0),
                    "owner_plan_task_id": task_id,
                    "owner_agent_id": selected_agent_id or runtime_task.get("agent_id"),
                    "purpose": _safe_text(artifact_candidate.get("purpose"), 800)
                    or _safe_text(previous.get("purpose"), 800),
                    "verification": {"status": "verified", "verified_hash": evidence["sha256"],
                                     "verified_at": utcnow(), "source": "accepted_runtime_artifact"},
                    "last_modified_by_task": task_id,
                    "last_modified_by_agent": selected_agent_id or runtime_task.get("agent_id") or "",
                })
                for symbol in previous.get("symbols", []):
                    if symbol.get("verified_hash") != evidence["sha256"]:
                        symbol["verified"] = False
                        symbol["status"] = "reported_unverified"
                state["artifacts"][key] = previous
                if evidence["path"] not in task_row.setdefault("artifact_paths", []):
                    task_row["artifact_paths"].append(evidence["path"])
                task_row["updated_revision"] = before_revision + 1
                events.append({
                    "event_type": "artifact.created" if is_new else "artifact.updated",
                    "status": "Success", "phase": "project_context", "actor_type": "runtime",
                    "plan_task_id": task_id, "runtime_task_id": runtime_task.get("id"),
                    "agent_id": selected_agent_id or runtime_task.get("agent_id"),
                    "path": evidence["path"], "artifact_revision": previous["revision"],
                    "content_hash": evidence["sha256"],
                    "message": "Accepted runtime evidence updated project artifact metadata.",
                })
                changed = True
            for raw in symbols[:100]:
                if not isinstance(raw, dict):
                    continue
                try:
                    path = normalize_owned_path(raw.get("artifact_path", raw.get("path")))
                    key = _artifact_key(path)
                except (TypeError, ValueError, CrossTaskRequestError):
                    continue
                artifact = state.get("artifacts", {}).get(key)
                if (artifact is None or artifact.get("owner_plan_task_id") != task_id
                        or artifact.get("status") != "verified"):
                    continue
                name = _safe_text(raw.get("name"), 128)
                if not _IDENTIFIER.fullmatch(name):
                    continue
                signature = _safe_text(raw.get("signature"), 300)
                purpose = _safe_text(raw.get("purpose"), 500)
                verified = self._verify_symbol(root, path, name, signature, artifact["sha256"])
                symbol_record = {
                    "name": name, "kind": _safe_text(raw.get("kind"), 64),
                    "signature": signature, "purpose": purpose,
                    "artifact_path": path, "verified": verified,
                    "status": "verified" if verified else "reported_unverified",
                    "verified_hash": artifact["sha256"] if verified else None,
                    "reported_by_task": task_id,
                }
                symbols_list = artifact.setdefault("symbols", [])
                symbols_list[:] = [item for item in symbols_list
                                   if str(item.get("name", "")).casefold() != name.casefold()]
                symbols_list.append(symbol_record)
                events.append({
                    "event_type": "symbol.verified" if verified else "symbol.reported",
                    "status": "Success" if verified else "Warning",
                    "phase": "project_context", "actor_type": "runtime",
                    "plan_task_id": task_id, "runtime_task_id": runtime_task.get("id"),
                    "agent_id": selected_agent_id or runtime_task.get("agent_id"),
                    "path": path, "symbol_name": name,
                    "message": "Reported symbol was checked against the current artifact bytes."
                    if verified else "Reported symbol was retained without claiming verification.",
                })
                events.append({
                    "event_type": ("project_context.symbol_verified" if verified
                                   else "project_context.symbol_reported"),
                    "status": "Success" if verified else "Warning",
                    "phase": "project_context", "actor_type": "runtime",
                    "plan_task_id": task_id, "path": path, "symbol_name": name,
                })
                changed = True
            for raw in dependencies[:100]:
                if not isinstance(raw, dict):
                    continue
                try:
                    source_path = normalize_owned_path(raw.get("path"))
                    dependency_path = normalize_owned_path(raw.get("depends_on"))
                    source_key = _artifact_key(source_path)
                    dependency_key = _artifact_key(dependency_path)
                except (TypeError, ValueError, CrossTaskRequestError):
                    continue
                source = state.get("artifacts", {}).get(source_key)
                target = state.get("artifacts", {}).get(dependency_key)
                if (source is None or target is None
                        or source.get("status") not in {"verified", "present", "stale"}
                        or target.get("status") not in {"verified", "present"}):
                    continue
                relation = _safe_text(raw.get("relationship"), 300)
                dependency = {
                    "path": dependency_path,
                    "relationship": relation,
                    "artifact_revision": source.get("revision", 0),
                    "depends_on_revision": target.get("revision", 0),
                    "status": "observed",
                }
                rows = source.setdefault("dependencies", [])
                rows[:] = [item for item in rows if item.get("path", "").casefold() != dependency_path.casefold()]
                rows.append(dependency)
                changed = True
            if invalid_claims:
                events.append({
                    "event_type": "project_context.update_rejected", "status": "Warning",
                    "phase": "project_context", "actor_type": "runtime",
                    "plan_task_id": task_id, "runtime_task_id": runtime_task.get("id"),
                    "rejected_paths": invalid_claims[:20],
                    "message": "Worker context claims did not match accepted runtime write evidence.",
                })
            if changed:
                state["updated_at"] = utcnow()
                state["revision"] = before_revision + 1
                events.append({
                    "event_type": "project_context.task_updated", "status": "Success",
                    "phase": "project_context", "actor_type": "runtime",
                    "plan_task_id": task_id, "runtime_task_id": runtime_task.get("id"),
                    "agent_id": selected_agent_id or runtime_task.get("agent_id"),
                    "state_revision": state["revision"],
                    "message": "Evaluator acceptance committed the worker's grounded project-context candidate.",
                })
            saved = self.store.commit_project_state(
                orchestration_id, expected_revision=before_revision,
                state=state, events=events, update_state=changed,
            )
            if saved is not None:
                return saved
        raise RuntimeError("Project state changed repeatedly while an accepted update was committed.")

    @staticmethod
    def _verify_symbol(root: Path, relative: str, name: str,
                       signature: str, expected_hash: str) -> bool:
        target = ProjectStateManager._safe_artifact_path(root, relative)
        if target is None:
            return False
        digest, _size = _file_metadata(target)
        if digest != expected_hash:
            return False
        try:
            content = target.read_bytes()
            if len(content) > MAX_TEXT_BYTES:
                return False
            text = content.decode("utf-8")
        except (OSError, UnicodeError):
            return False
        identifier = re.escape(name)
        declaration = re.search(
            rf"^\s*(?:(?:export|default|async|public|private|static)\s+)*"
            rf"(?:def|class|function|interface|type|enum|const|let|var)\s+{identifier}\b[^\r\n]*|"
            rf"^\s*(?:export\s+)?(?:async\s+)?{identifier}\s*\([^\r\n]*",
            text, flags=re.MULTILINE,
        )
        if not declaration:
            return False
        if signature:
            candidate_parameters = re.search(
                rf"\b{identifier}\s*\(([^)]*)\)", signature,
            )
            declaration_parameters = re.search(
                rf"\b{identifier}\s*(?:=\s*(?:async\s*)?)?\(([^)]*)\)",
                declaration.group(0),
            )
            if candidate_parameters:
                if not declaration_parameters:
                    return False
                candidate_args = re.sub(r"\s+", "", candidate_parameters.group(1))
                declaration_args = re.sub(r"\s+", "", declaration_parameters.group(1))
                return candidate_args == declaration_args
            normalized_signature = re.sub(r"\s+", "", signature)
            normalized_declaration = re.sub(r"\s+", "", declaration.group(0))
            return normalized_signature in normalized_declaration
        return True


def query_project_snapshot(snapshot: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Run a bounded metadata-only query over the worker's dispatch snapshot."""
    allowed = {"operation", "path", "query", "task_id", "limit"}
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise ValueError("project_context accepts only operation, path, query, task_id and limit.")
    operation = arguments.get("operation")
    if operation not in {"summary", "artifact", "symbol", "task", "search"}:
        raise ValueError("project_context operation must be summary, artifact, symbol, task or search.")
    limit = arguments.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_RESULTS:
        raise ValueError(f"project_context limit must be between 1 and {MAX_QUERY_RESULTS}.")
    artifacts = [item for item in snapshot.get("artifacts", []) if isinstance(item, dict)]
    tasks = [item for item in snapshot.get("tasks", []) if isinstance(item, dict)]
    query = arguments.get("query", "")
    path = arguments.get("path", "")
    task_id = arguments.get("task_id", "")
    if any(not isinstance(value, str) or len(value) > 512 for value in (query, path, task_id)):
        raise ValueError("project_context text arguments must be strings up to 512 characters.")
    if operation == "summary":
        value = {
            "revision": snapshot.get("revision", 0),
            "artifact_count": snapshot.get("total_artifact_count", len(artifacts)),
            "visible_artifact_count": len(artifacts),
            "manifest": snapshot.get("manifest", {}),
            "task_count": len(tasks),
            "task_statuses": {str(item.get("task_id")): item.get("status") for item in tasks},
            "snapshot_truncated": bool(snapshot.get("snapshot_truncated")),
        }
    elif operation == "artifact":
        if not path:
            raise ValueError("artifact query requires path.")
        try:
            key = _artifact_key(path)
        except (ValueError, CrossTaskRequestError) as exc:
            raise ValueError("artifact path must be a workspace-relative file path.") from exc
        value = next((item for item in artifacts if _artifact_key(item.get("path", "")) == key), None)
        value = value or {"path": path, "status": "not_in_snapshot"}
    elif operation == "symbol":
        if not query:
            raise ValueError("symbol query requires query.")
        found = []
        for artifact in artifacts:
            for symbol in artifact.get("symbols", []):
                if str(symbol.get("name", "")).casefold() == query.casefold():
                    found.append({**symbol, "artifact_status": artifact.get("status"),
                                  "artifact_revision": artifact.get("revision")})
        value = {"query": query, "matches": found[:limit]}
    elif operation == "task":
        if not task_id:
            raise ValueError("task query requires task_id.")
        value = next((item for item in tasks if item.get("task_id") == task_id), None)
        value = value or {"task_id": task_id, "status": "not_in_snapshot"}
    else:
        if not query:
            raise ValueError("search query requires query.")
        needle = query.casefold()
        found_artifacts = []
        found_symbols = []
        found_tasks = []
        for artifact in artifacts:
            metadata = " ".join([str(artifact.get("path", "")), str(artifact.get("purpose", ""))]).casefold()
            if needle in metadata:
                found_artifacts.append(artifact)
            for symbol in artifact.get("symbols", []):
                text = " ".join(str(symbol.get(key, "")) for key in ("name", "signature", "purpose")).casefold()
                if needle in text:
                    found_symbols.append({**symbol, "artifact_status": artifact.get("status"),
                                          "artifact_revision": artifact.get("revision")})
        for task in tasks:
            text = " ".join(str(task.get(key, "")) for key in ("task_id", "objective", "status")).casefold()
            if needle in text:
                found_tasks.append(task)
        value = {"query": query, "artifacts": found_artifacts[:limit],
                 "symbols": found_symbols[:limit], "tasks": found_tasks[:limit],
                 "snapshot_truncated": bool(snapshot.get("snapshot_truncated"))}
    result = sanitize({"operation": operation, "revision": snapshot.get("revision", 0), "result": value})
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > MAX_QUERY_CHARS:
        result = sanitize({
            "operation": operation, "revision": snapshot.get("revision", 0),
            "result": {"truncated": True,
                       "message": "Query metadata exceeded the response limit; narrow the query."},
        })
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return {"json": encoded, "operation": operation,
            "result_count": len(value.get("matches", [])) if isinstance(value, dict) and isinstance(value.get("matches"), list)
            else sum(len(value.get(key, [])) for key in ("artifacts", "symbols", "tasks") if isinstance(value.get(key), list))
            if isinstance(value, dict) else 1,
            "revision": snapshot.get("revision", 0)}
