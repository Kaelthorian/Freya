"""Normalize mechanical semantic prerequisites before runtime identities exist."""
from __future__ import annotations

import copy
import re
from typing import Any

from .cross_task import normalize_owned_paths, owned_path_key
from .capabilities import CAPABILITY_REGISTRY
from .plan_evidence import verification_mode
from .runtime_resources import SEMANTIC_OPERATION_CAPABILITIES


EXPECTED_TASK_RANGES = {"simple": (1, 2), "multi_step": (2, 5)}
GRANULARITY_FIELDS = {"logical_outcome", "independent_value", "preserve_boundary"}
IMPLEMENTATION_KINDS = {"file_creation", "program_creation", "code_change", "general"}
FILE_OPERATIONS = {"create_file", "modify_file", "overwrite_file", "read_file",
                   "list_workspace", "search_workspace"}
# These are boundary declarations, never permission grants. Existing runtime
# identities/policies must not be rewritten by a semantic normalization pass.
FROZEN_FIELDS = {"id", "worker_id", "worker_assignment", "worker_assignments",
                 "capability_policy", "policy", "approval", "approval_boundary",
                 "security_boundary", "recovery_links", "rollback", "recovery_boundary",
                 "criterion_links", "foreign_write_targets", "task_characteristics"}
_BOUNDARY = re.compile(
    r"\b(?:approval|approv\w*|human review|user phase|separate phases?|"
    r"security boundary|policy boundary|rollback|independent recovery|"
    r"aprobaci\w*|aprobado|fases? separad\w*|etapas? separad\w*|"
    r"recuperaci\w* independiente|resultado intermedio requerido)\b", re.I)
_SCAFFOLD = re.compile(
    r"\b(?:empty|vac[ií][oa])\s+(?:files?|director(?:y|ies)|folders?|artifacts?|"
    r"archivos?|directorios?|carpetas?|artefactos?)\b|"
    r"\b(?:files?|archivos?|directorios?|carpetas?)\s+(?:(?:is|are|est[aá])\s+)?(?:empty|vac[ií][oa])\b|"
    r"\b(?:placeholder|scaffold|boilerplate|esqueleto|marcador de posici[oó]n)\b", re.I)
_DIRECTORY = re.compile(r"\b(?:mkdir|directory|folder|directorio|carpeta)\b", re.I)
_READ_ONLY = re.compile(r"\b(?:read|open|inspect|leer|abrir|inspeccionar)\b", re.I)


def normalize_granularity(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) - GRANULARITY_FIELDS:
        raise ValueError("granularity must contain logical_outcome, independent_value or preserve_boundary only.")
    result = {}
    for key, text in value.items():
        if not isinstance(text, str) or len(text) > 1000:
            raise ValueError("granularity values must be bounded text, never permissions.")
        result[key] = text.strip()
    return result


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")[:64]


def _text(task: dict[str, Any]) -> str:
    return " ".join([str(task.get("objective") or ""), str(task.get("description") or ""),
                     *task.get("semantic_needs", [])])


def _paths(task: dict[str, Any]) -> list[str]:
    return normalize_owned_paths([*task.get("owned_paths", []), *task.get("write_targets", [])])


def _presence(criterion: str) -> bool:
    # Use the existing strict bare-presence grammar, not its broad semantic
    # classification: "file exists and contains logic" has independent value.
    if verification_mode(criterion) in {"file_exists", "file_readable"}:
        return True
    return bool(re.fullmatch(
        r"(?:(?:the|a|el|la)\s+)?(?:directory|folder|directorio|carpeta)\s+"
        r"[\w./\\-]+\s+(?:exists?|is created|existe|est[aá] creado)[.!]?",
        criterion.strip(), re.I))


def _mechanical(task: dict[str, Any]) -> str | None:
    operations = set(task.get("operations", []))
    criteria = task.get("success_criteria", [])
    if any(not _presence(criterion) and not _scaffold_only(criterion) for criterion in criteria):
        return None
    if not operations and _DIRECTORY.search(_text(task)) and _paths(task):
        return "directory_setup"
    if operations and operations <= {"read_file", "list_workspace", "search_workspace"}:
        if _READ_ONLY.search(_text(task)):
            return "prerequisite_inspection"
    if operations & {"create_file", "modify_file", "overwrite_file"}:
        if operations == {"create_file"} and criteria and all(_presence(item) for item in criteria):
            return "scaffold"
        if (re.search(r"\b(?:implement|implementation|logic|public api|conversion|"
                      r"implementar|implementaci[oó]n|l[oó]gica|conversi[oó]n)\b", _text(task), re.I)
                and not _SCAFFOLD.search(_text(task))):
            return None
        if (all(_presence(criterion) for criterion in criteria) or _SCAFFOLD.search(_text(task))
                or any(_scaffold_only(criterion) for criterion in criteria)):
            return "scaffold"
    return None


def _scaffold_only(criterion: str) -> bool:
    return bool(_SCAFFOLD.search(criterion) and not re.search(
        r"\b(?:logic|functions?|handlers?|behavior|conversion|converts?|calculates?|"
        r"handles?|input|output|implements?|api|without|no|not|sin)\b", criterion, re.I))


def _canonical_boundary(spec: dict[str, Any]) -> str:
    texts = [str(spec.get("user_intent") or "")]
    for field in ("constraints", "requirements", "deliverables"):
        texts.extend(entry["description"] for entry in spec.get(field, [])
                     if entry.get("source") in {"explicit", "clarified"})
    texts.extend(str(value) for value in spec.get("user_decisions", {}).values())
    # An expressly requested empty/intermediate artifact is a deliverable,
    # rather than an inferred implementation prerequisite.
    affirmative = [re.sub(r"\b(?:do not|don't|never|avoid|no|sin|evitar|nunca)\s+(?:\w+\s+){0,4}"
                          r"(?:empty (?:files?|director(?:y|ies)|folders?|artifacts?)|"
                          r"placeholders?|scaffolds?|archivos? vac[ií]os?|"
                          r"directorios? vac[ií]os?|carpetas? vac[ií]as?|"
                          r"empty [\w./\\-]+\.[a-z0-9]+)\b",
                          "", text, flags=re.I) for text in texts]
    if any(_BOUNDARY.search(text) or _SCAFFOLD.search(text) or
           re.search(r"\bempty [\w./\\-]+\.[a-z0-9]+\b", text, re.I) for text in affirmative):
        return "canonical_intermediate_or_phase_boundary"
    return ""


def _boundary(task: dict[str, Any], *, source: bool) -> str:
    if task.get("task_kind") not in IMPLEMENTATION_KINDS:
        return "independent_role"
    if set(task.get("operations", [])) - FILE_OPERATIONS:
        return "execution_or_non_filesystem_boundary"
    if any(CAPABILITY_REGISTRY[SEMANTIC_OPERATION_CAPABILITIES[operation]].dangerous
           for operation in task.get("operations", [])):
        return "approval_capability_boundary"
    if any(field in task for field in FROZEN_FIELDS):
        return "identity_policy_or_recovery_boundary"
    metadata = task.get("granularity", {})
    if metadata.get("preserve_boundary"):
        return "explicit_task_boundary"
    if source and metadata.get("independent_value"):
        return "declared_independent_value"
    if _BOUNDARY.search(_text(task)):
        return "task_phase_boundary"
    return ""


def has_concrete_granularity_reason(reason: Any, tasks: list[dict[str, Any]],
                                    summary: dict[str, Any]) -> bool:
    """Require an explanation and graph-grounded independent value/boundaries.

    Counts and Worker strategy are not evidence of meaningful Task granularity.
    This metadata explains decomposition; it never grants a permission.
    """
    if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 1000:
        return False
    if not re.search(r"\b(?:independent|separately recoverable|public interfaces?|"
                     r"approval|security|policy|rollback|recovery|consumers?|dependencies|"
                     r"subsystems?|qa|audit|review|independiente[s]?|aprobaci[oó]n|"
                     r"recuperaci[oó]n|fases)\b", reason, re.I):
        return False
    protected = {item[field] for item in summary.get("preserved_boundaries", [])
                 if item["reason"] != "distinct_or_parallel_workers"
                 for field in ("source_task_key", "target_task_key")}
    return all(_mechanical(task) is None or task.get("key") in protected or
               bool(_boundary(task, source=True)) for task in tasks)


def _validate_graph(tasks: list[dict[str, Any]], keys: list[str]) -> None:
    by_key = dict(zip(keys, tasks))
    visited, active = set(), set()

    def visit(key):
        if key in active:
            raise ValueError("Semantic dependencies contain a cycle; granularity cannot repair it.")
        if key in visited:
            return
        active.add(key)
        for dependency in by_key[key].get("depends_on", []):
            target = _key(dependency)
            if target not in by_key:
                raise ValueError(f"Unknown semantic dependency: {dependency}.")
            visit(target)
        active.remove(key)
        visited.add(key)

    for key in keys:
        visit(key)


def normalize_task_granularity(plan: dict[str, Any], tasks: list[dict[str, Any]],
                               keys: list[str], spec: dict[str, Any]
                               ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Merge only a mechanical, single-consumer prerequisite into its successor.

    Preserve the earliest semantic key. IDs, criterion links, write owners and
    Worker assignments are created by Compiler afterwards, never patched in a
    live/persisted execution graph. Original source IDs in diagnostics refer to
    the proposed plan's positions, not existing Runtime tasks.
    """
    tasks = copy.deepcopy(tasks)
    keys = list(keys)
    original_keys = list(keys)
    for task, key in zip(tasks, keys):
        task["key"] = key
        if "granularity" in task:
            task["granularity"] = normalize_granularity(task["granularity"])
        criteria = task.get("success_criteria", [])
        if not isinstance(criteria, list) or any(not isinstance(item, str) or not item.strip() for item in criteria):
            raise ValueError("Semantic task checks must be non-empty text.")
        task["success_criteria"] = list(dict.fromkeys(item.strip() for item in criteria))
        task["depends_on"] = list(dict.fromkeys(_key(item) for item in task.get("depends_on", [])))
    _validate_graph(tasks, keys)
    sources = {key: [f"task-{index}"] for index, key in enumerate(keys, 1)}
    auxiliary = {key: [] for key in keys}
    aliases = {key: key for key in keys}
    component_roots: dict[str, list[str]] = {}
    merges, preserved = [], []
    canonical_boundary = _canonical_boundary(spec)
    strategy = plan.get("execution_strategy", "single_worker")

    while True:
        children = {key: [] for key in keys}
        for target_key, task in zip(keys, tasks):
            for dependency in task["depends_on"]:
                children[dependency].append(target_key)
        merged = False
        for index, (source_key, source) in enumerate(zip(keys, tasks)):
            mechanical = _mechanical(source)
            if mechanical is None or not children[source_key]:
                continue
            reason = canonical_boundary or (
                "distinct_or_parallel_workers" if strategy != "single_worker" else "")
            reason = reason or _boundary(source, source=True)
            if len(children[source_key]) != 1:
                reason = reason or "multiple_consumers"
            target_key = children[source_key][0]
            target_index = keys.index(target_key)
            target = tasks[target_index]
            reason = reason or _boundary(target, source=False)
            if target["depends_on"] != [source_key]:
                reason = reason or "additional_dependencies"
            if reason:
                preserved.append({"source_task_key": source_key, "target_task_key": target_key, "reason": reason})
                continue
            source_paths, target_paths = _paths(source), _paths(target)
            source_set = {owned_path_key(path) for path in source_paths}
            target_set = {owned_path_key(path) for path in target_paths}
            # Read prerequisites name their input in their objective/description;
            # do not turn a metadata/path-free inspection into an arbitrary merge.
            if mechanical == "prerequisite_inspection" and not source_set:
                source_set = {owned_path_key(path) for path in target_paths if re.search(
                    r"(?<![\w./\\-])" + re.escape(path) + r"(?![\w/\\-]|\.[\w])",
                    _text(source), re.I)}
            same_artifact = bool(source_set) and source_set == target_set
            roots = component_roots.get(source_key, source_paths if mechanical == "directory_setup" else [])
            component = bool(roots and target_paths and all(
                any(owned_path_key(path).startswith(owned_path_key(root).rstrip("/") + "/") for root in roots)
                for path in target_paths))
            outcome = source.get("granularity", {}).get("logical_outcome")
            same_outcome = bool(outcome and outcome == target.get("granularity", {}).get("logical_outcome"))
            if not (same_artifact or component or same_outcome):
                continue
            source_ops, target_ops = source.get("operations", []), target.get("operations", [])
            if not set(target_ops) & {"create_file", "modify_file", "overwrite_file"}:
                continue
            # A merge must never hide two creators claiming one concrete file.
            if ("create_file" in source_ops and "create_file" in target_ops and source_set & target_set):
                continue
            if (mechanical != "directory_setup" and source_set and not source_set <= target_set
                    and not (component or same_outcome)):
                continue
            final_criteria = [criterion for criterion in target.get("success_criteria", [])
                              if not _presence(criterion)]
            # Keep a mechanical tail recognizable until the entire chain is
            # normalized. Only the final survivor gets an outcome criterion.
            if not final_criteria:
                final_criteria = list(target.get("success_criteria", []))
            combined = copy.deepcopy(target)
            combined.update(key=source_key, depends_on=list(source["depends_on"]),
                            operations=list(dict.fromkeys([*source_ops, *target_ops])),
                            semantic_needs=list(dict.fromkeys([
                                *source.get("semantic_needs", []), *target.get("semantic_needs", [])])),
                            success_criteria=list(dict.fromkeys(final_criteria)))
            combined["description"] = (
                str(target.get("description") or target["objective"]) +
                "\nIncluded mechanical prerequisite: " + str(source["objective"]) +
                "\n" + str(source.get("description") or source["objective"]))
            combined["owned_paths"] = normalize_owned_paths([
                *(source.get("owned_paths", []) if mechanical != "directory_setup" else []),
                *target.get("owned_paths", [])])
            combined["write_targets"] = normalize_owned_paths([
                *(source.get("write_targets", []) if mechanical != "directory_setup" else []),
                *target.get("write_targets", [])])
            combined["granularity"] = {**source.get("granularity", {}), **target.get("granularity", {})}
            if not combined["granularity"]:
                combined.pop("granularity")
            if mechanical == "directory_setup" or component:
                component_roots[source_key] = roots
            sources[source_key] += sources.pop(target_key)
            auxiliary[source_key] += [*auxiliary.pop(target_key), *source.get("success_criteria", [])]
            for key, representative in aliases.items():
                if representative == target_key:
                    aliases[key] = source_key
            merge_reason = ("same_artifact_create_then_modify_without_independent_value" if same_artifact
                            else "same_logical_component_mechanical_prerequisite")
            merges.append({"source_task_ids": list(sources[source_key]),
                           "source_task_keys": [source_key, target_key],
                           "result_task_key": source_key, "reason": merge_reason,
                           "operations": list(combined["operations"]),
                           "absorbed_prerequisite_criteria": list(source.get("success_criteria", []))})
            tasks[index] = combined
            tasks.pop(target_index)
            keys.pop(target_index)
            for task in tasks:
                task["depends_on"] = list(dict.fromkeys(
                    source_key if dependency == target_key else dependency for dependency in task["depends_on"]))
            _validate_graph(tasks, keys)
            merged = True
            break
        if not merged:
            break

    global_checks = {item.strip() for item in [*plan.get("success_criteria", []),
                                              *spec.get("validation_expectations", [])]
                     if isinstance(item, str) and item.strip()}
    for task, key in zip(tasks, keys):
        if len(sources[key]) == 1:
            continue
        criteria = task.get("success_criteria", [])
        final_criteria = [criterion for criterion in criteria
                          if not _presence(criterion) and not _scaffold_only(criterion)]
        if not final_criteria:
            final_criteria = ["The resulting source contains the requested implementation content and structure."]
        # Explicit global presence obligations remain auxiliary local checks.
        # Regenerating links later therefore cannot orphan a canonical criterion.
        task["success_criteria"] = list(dict.fromkeys([
            *final_criteria, *[criterion for criterion in [*auxiliary[key], *criteria]
                              if criterion in global_checks]]))
    compiled_ids = {key: f"task-{index}" for index, key in enumerate(keys, 1)}
    for merge in merges:
        merge["result_task_id"] = compiled_ids[aliases[merge["result_task_key"]]]
    unique_preserved = {tuple(item.values()): item for item in preserved}
    summary = {"task_count_before": len(original_keys), "task_count_after": len(tasks),
               "merges": merges, "preserved_boundaries": list(unique_preserved.values()),
               "task_complexity": plan.get("task_complexity"), "execution_strategy": strategy,
               "task_id_map": {f"task-{index}": compiled_ids[aliases[key]]
                               for index, key in enumerate(original_keys, 1)},
               "operations_grouped": [merge["operations"] for merge in merges]}
    return tasks, keys, summary
