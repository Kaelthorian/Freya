"""Compile a semantic model plan into the existing durable execution contract."""
from __future__ import annotations

import re
import copy
from typing import Any

from .planner import MAX_PLAN_TASKS, TASK_KIND_VALUES, PlanValidationError, validate_plan
from .plan_scope import reconcile_plan_scope, semantic_categories
from .runtime_resources import RuntimeResourceCatalog, UnsupportedResourceRequirement
from .task_spec import validate_task_spec, _action_matches, _scope_tokens
from .cross_task import CrossTaskRequestError, normalize_owned_paths, owned_path_key
from .plan_evidence import (
    COMPILATION_RESULT, RUNTIME_BEHAVIOR, TEST_RESULT, classify_criterion,
    evidence_is_supported, verification_mode,
)
from .security import sanitize
from .verification_cases import normalize_cases, explicit_cases, group_case_tasks, MODES


WRITE_CAPABILITIES = {"filesystem.create", "filesystem.modify", "filesystem.overwrite"}


class OwnershipAmbiguous(PlanValidationError):
    """More than one modifier claims a path without a unique creator."""


class WriteScopeOverlap(PlanValidationError):
    """Unordered tasks can write the same artifact and race or duplicate work."""


class CriterionEvidenceMismatch(PlanValidationError):
    """A task cannot produce the minimum evidence required by its criterion."""


class OverfragmentedPlan(PlanValidationError):
    """Delegation cost is not justified by the semantic task graph."""


def _validate_decomposition(plan: dict[str, Any], tasks: list[dict[str, Any]],
                            dependencies: list[set[int]], catalog: RuntimeResourceCatalog) -> None:
    """Apply the new strategy contract without reinterpreting older saved plans."""
    fields = {"task_complexity", "execution_strategy", "decomposition_reason"}
    if not fields.intersection(plan):
        return
    if not fields <= plan.keys():
        raise PlanValidationError("Semantic plan must include all decomposition decision fields.")
    complexity = plan["task_complexity"]
    strategy = plan["execution_strategy"]
    reason = plan["decomposition_reason"]
    if complexity not in {"simple", "multi_step", "complex"}:
        raise PlanValidationError("Invalid task_complexity.")
    if strategy not in {"single_worker", "multi_worker"}:
        raise PlanValidationError("Invalid execution_strategy.")
    if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 1000:
        raise PlanValidationError("decomposition_reason must explain the worker decision.")
    count = len(tasks)
    # Semantic tasks are checkpoints, not worker allocations. A cohesive chain
    # can contain many tasks and still occupy one execution slot.
    if strategy == "single_worker" or count == 1:
        _compiler_event(catalog, "plan_compiler.decomposition_validated", task_count=count,
                        task_complexity=complexity, execution_strategy=strategy)
        return
    review_or_qa = any(task.get("task_kind") in {"review", "testing"} for task in tasks)
    implementation_only = all(task.get("task_kind") in {
        "file_creation", "program_creation", "code_change", "general"
    } for task in tasks)
    linear = all(dependencies[index] == {index - 1} for index in range(1, count))
    same_targets = any(set(left.get("write_targets", [])) & set(right.get("write_targets", []))
                       for left, right in zip(tasks, tasks[1:]))
    stems = [{str(path).replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
              for path in task.get("write_targets", [])} for task in tasks]
    coupled_pair = (count == 2 and linear and complexity != "complex" and
                    (bool(stems[0] & stems[1]) or
                     _responsibility_similarity(tasks[0], tasks[1]) >= 0.6))
    vague = re.search(r"(?i)(multiple? files?|several files?|many steps?|different operations?|"
                      r"html.?css.?js|acceptance criteria|more than one file)", reason)
    concrete = re.search(r"(?i)(independent (?:audit|review|verification)|controlled qa|"
                         r"separate subsystems?|distinct (?:interface|dependency|speciali[sz])|"
                         r"parallel (?:implementation|development))", reason)
    overfragmented = bool((vague and not concrete) or
                          (implementation_only and not review_or_qa and (
                              complexity == "simple" or same_targets or
                              coupled_pair or
                              (count >= 3 and linear and complexity != "complex"))))
    if overfragmented:
        _compiler_event(catalog, "plan_compiler.overfragmented", task_count=count,
                        task_complexity=complexity, execution_strategy=strategy,
                        reason="delegation_cost_exceeds_benefit")
        raise OverfragmentedPlan(
            "OverfragmentedPlan: cohesive implementation tasks should be consolidated; "
            "reserve separate workers for a concrete dependency, specialist, QA, audit or subsystem boundary.")
    _compiler_event(catalog, "plan_compiler.decomposition_validated", task_count=count,
                    task_complexity=complexity, execution_strategy=strategy)


def _dependency_indexes(tasks: list[dict[str, Any]], keys: list[str]) -> list[set[int]]:
    """Resolve semantic dependency keys once for structural compiler checks."""
    result: list[set[int]] = []
    for task in tasks:
        dependencies = task.get("depends_on", [])
        if not isinstance(dependencies, list):
            raise PlanValidationError("Semantic dependencies must be a list.")
        resolved: set[int] = set()
        for dependency in dependencies:
            key = _slug(str(dependency))
            if key not in keys:
                raise PlanValidationError(f"Unknown semantic dependency: {dependency}.")
            resolved.add(keys.index(key))
        result.append(resolved)
    return result


def _dependency_ancestors(dependencies: list[set[int]]) -> list[set[int]]:
    ancestors = [set(items) for items in dependencies]
    changed = True
    while changed:
        changed = False
        for index, direct in enumerate(list(ancestors)):
            expanded = set(direct)
            for dependency in direct:
                expanded.update(ancestors[dependency])
            if expanded != ancestors[index]:
                ancestors[index] = expanded
                changed = True
    return ancestors


def _responsibility_similarity(first: dict[str, Any], second: dict[str, Any]) -> float:
    def tokens(task: dict[str, Any]) -> set[str]:
        fields = [task.get("objective", ""), task.get("description", "")]
        for name in ("semantic_needs", "success_criteria"):
            values = task.get(name, [])
            if isinstance(values, list):
                fields.extend(values)
        return set(_scope_tokens(" ".join(str(item) for item in fields)))
    left, right = tokens(first), tokens(second)
    return round(len(left & right) / len(left | right), 3) if left or right else 0.0


def _validate_write_scope_overlaps(tasks: list[dict[str, Any]], keys: list[str],
                                   resources: list[dict[str, Any]],
                                   dependencies: list[set[int]],
                                   catalog: RuntimeResourceCatalog) -> None:
    """Reject shared writable paths unless task dependencies impose an order."""
    ancestors = _dependency_ancestors(dependencies)
    writers: dict[str, list[tuple[int, str]]] = {}
    for index, runtime in enumerate(resources):
        for path in normalize_owned_paths([
                *runtime.get("owned_paths", []), *runtime.get("write_targets", [])]):
            writers.setdefault(owned_path_key(path), []).append((index, path))
    for entries in writers.values():
        for position, (left, path) in enumerate(entries):
            for right, _ in entries[position + 1:]:
                left_operations = set(resources[left].get("semantic_operations", []))
                right_operations = set(resources[right].get("semantic_operations", []))
                creators = [index for index, operations in (
                    (left, left_operations), (right, right_operations)
                ) if "create_file" in operations]
                if len(creators) == 1:
                    creator = creators[0]
                    modifier = right if creator == left else left
                    ordered = creator in ancestors[modifier]
                else:
                    ordered = left in ancestors[right] or right in ancestors[left]
                similarity = _responsibility_similarity(tasks[left], tasks[right])
                details = {
                    "path": path,
                    "task_keys": [keys[left], keys[right]],
                    "task_ids": [f"task-{left + 1}", f"task-{right + 1}"],
                    "dependency_ordered": ordered,
                    "responsibility_similarity": similarity,
                }
                if len(creators) == 2:
                    _compiler_event(catalog, "plan_compiler.overlap_detected",
                                    reason="multiple_creators_share_write_target", **details)
                    raise PlanValidationError(
                        f"Two creators claim {path}: task-{left + 1}, task-{right + 1}")
                if ordered:
                    _compiler_event(catalog, "plan_compiler.overlap_validated",
                                    reason="dependency_ordered_write_handoff", **details)
                    continue
                _compiler_event(catalog, "plan_compiler.overlap_detected",
                                reason="unordered_tasks_share_write_target", **details)
                raise WriteScopeOverlap(
                    "WriteScopeOverlap: {} and {} both write {} without a dependency order; "
                    "narrow their responsibilities/write targets or add the real semantic dependency."
                    .format(details["task_ids"][0], details["task_ids"][1], path)
                )


def _default_task_criterion(task: dict[str, Any], runtime: dict[str, Any]) -> str:
    capabilities = set(runtime.get("required_capabilities", []))
    if capabilities & WRITE_CAPABILITIES:
        return "The declared workspace artifacts exist and their resulting content can be inspected."
    if any(item.startswith("execution.") for item in capabilities):
        return "The planned command completes with recorded output and exit status."
    if capabilities & {"filesystem.read", "filesystem.list", "filesystem.search", "git.diff", "git.status"}:
        return "The requested workspace content is inspected and recorded."
    return "The task result is reported."


def _reconcile_criterion_evidence(tasks: list[dict[str, Any]], keys: list[str],
                                  resources: list[dict[str, Any]],
                                  dependencies: list[set[int]],
                                  catalog: RuntimeResourceCatalog) -> None:
    """Move a criterion to one clear verifier or reject it before execution."""
    ancestors = _dependency_ancestors(dependencies)
    original_criteria: list[list[str]] = []
    for task in tasks:
        criteria = task.get("success_criteria") or []
        if not isinstance(criteria, list) or any(
                not isinstance(item, str) or not item.strip() for item in criteria):
            raise PlanValidationError("Semantic task checks must be text.")
        original_criteria.append(list(dict.fromkeys(item.strip() for item in criteria)))
    retained: list[list[str]] = [[] for _ in tasks]
    reassigned: list[list[str]] = [[] for _ in tasks]
    execution_evidence = {RUNTIME_BEHAVIOR, TEST_RESULT, COMPILATION_RESULT}
    for index, criteria in enumerate(original_criteria):
        capabilities = resources[index]["required_capabilities"]
        task_kind = str(resources[index].get("task_kind") or "")
        for criterion in criteria:
            evidence_type = classify_criterion(criterion)
            supported, reason = evidence_is_supported(
                evidence_type, capabilities, task_kind=task_kind)
            _compiler_event(
                catalog, "plan_compiler.criterion_classified",
                task_key=keys[index], task_id=f"task-{index + 1}",
                criterion=criterion[:300], evidence_type=evidence_type,
                verification_mode=verification_mode(criterion),
                required_capabilities=list(capabilities), verifiable=supported,
                reason=reason,
            )
            if supported:
                retained[index].append(criterion)
                continue
            candidates = []
            for candidate, runtime in enumerate(resources):
                if index not in ancestors[candidate]:
                    continue
                candidate_kind = str(runtime.get("task_kind") or "")
                if evidence_type in execution_evidence and candidate_kind != "testing":
                    continue
                can_verify, _ = evidence_is_supported(
                    evidence_type, runtime["required_capabilities"], task_kind=candidate_kind)
                if can_verify:
                    candidates.append(candidate)
            if len(candidates) == 1:
                target = candidates[0]
                reassigned[target].append(criterion)
                _compiler_event(
                    catalog, "plan_compiler.criterion_reassigned",
                    criterion=criterion[:300], evidence_type=evidence_type,
                    verification_mode=verification_mode(criterion),
                    source_task_key=keys[index], source_task_id=f"task-{index + 1}",
                    target_task_key=keys[target], target_task_id=f"task-{target + 1}",
                    reason="one compatible dependent verifier exists",
                )
                continue
            _compiler_event(
                catalog, "plan_compiler.plan_repair_required",
                task_key=keys[index], task_id=f"task-{index + 1}",
                criterion=criterion[:300], evidence_type=evidence_type,
                verification_mode=verification_mode(criterion),
                required_capabilities=list(capabilities), compatible_verifiers=len(candidates),
                reason=reason,
            )
            raise CriterionEvidenceMismatch(
                "CriterionEvidenceMismatch for task-{}: criterion {!r} requires {} evidence, "
                "but the task capabilities cannot produce it and no unique dependent verifier exists."
                .format(index + 1, criterion[:200], evidence_type)
            )
    for index, task in enumerate(tasks):
        criteria = list(dict.fromkeys([*retained[index], *reassigned[index]]))
        if not criteria:
            replacement = _default_task_criterion(task, resources[index])
            criteria = [replacement]
            _compiler_event(
                catalog, "plan_compiler.criterion_rewritten",
                task_key=keys[index], task_id=f"task-{index + 1}",
                criterion=replacement, evidence_type=classify_criterion(replacement),
                reason="all original criteria were reassigned to their verifier",
            )
        task["success_criteria"] = criteria


def _assign_write_owners(tasks: list[dict[str, Any]],
                         catalog: RuntimeResourceCatalog) -> dict[str, str]:
    """Resolve permanent plan-task ownership without changing the task graph."""
    claims: dict[str, dict[str, Any]] = {}
    for index, task in enumerate(tasks):
        try:
            owned = normalize_owned_paths(task.get("owned_paths", []))
            targets = normalize_owned_paths(task.get("write_targets", []))
        except CrossTaskRequestError as exc:
            raise PlanValidationError(f"Invalid write path in task-{index + 1}: {exc}") from exc
        writes = set(task.get("operations", [])) & {"create_file", "modify_file", "overwrite_file"}
        if writes and not (owned or targets):
            raise PlanValidationError(f"task-{index + 1} has a write operation without a concrete write target.")
        if not writes and targets:
            raise PlanValidationError(f"task-{index + 1} declares write_targets without a write operation.")
        task["owned_paths"] = owned
        task["write_targets"] = normalize_owned_paths([*owned, *targets]) if writes else targets
        for path in task["write_targets"]:
            key = owned_path_key(path)
            claim = claims.setdefault(key, {"path": path, "writers": set(), "owners": set(), "creators": set()})
            claim["writers"].add(index)
            if path in owned:
                claim["owners"].add(index)
            if "create_file" in writes:
                claim["creators"].add(index)
        for path in owned:
            key = owned_path_key(path)
            claim = claims.setdefault(key, {"path": path, "writers": set(), "owners": set(), "creators": set()})
            claim["owners"].add(index)
    owners: dict[str, str] = {}
    for key, claim in claims.items():
        creators, explicit, writers = claim["creators"], claim["owners"], claim["writers"]
        if len(creators) > 1:
            raise PlanValidationError(
                f"Two creators claim {claim['path']}: " + ", ".join(f"task-{i + 1}" for i in sorted(creators)))
        if creators:
            owner_index = next(iter(creators))
        elif len(explicit) == 1:
            owner_index = next(iter(explicit))
        elif len(explicit) > 1:
            _compiler_event(catalog, "plan_compiler.ownership_ambiguous", path=claim["path"],
                            task_ids=[f"task-{i + 1}" for i in sorted(explicit)])
            raise OwnershipAmbiguous(
                f"OwnershipAmbiguous for {claim['path']}: " + ", ".join(
                    f"task-{i + 1}" for i in sorted(explicit)))
        elif len(writers) == 1:
            owner_index = next(iter(writers))
        elif len(writers) > 1:
            _compiler_event(catalog, "plan_compiler.ownership_ambiguous", path=claim["path"],
                            task_ids=[f"task-{i + 1}" for i in sorted(writers)])
            raise OwnershipAmbiguous(
                f"OwnershipAmbiguous for {claim['path']}: " + ", ".join(
                    f"task-{i + 1}" for i in sorted(writers)))
        else:
            owner_index = next(iter(explicit))
        owner_id = f"task-{owner_index + 1}"
        owners[key] = owner_id
        for index, task in enumerate(tasks):
            task["owned_paths"] = [path for path in task["owned_paths"]
                                   if owned_path_key(path) != key or index == owner_index]
        owner = tasks[owner_index]
        if not any(owned_path_key(path) == key for path in owner["owned_paths"]):
            owner["owned_paths"].append(claim["path"])
        _compiler_event(catalog, "plan_compiler.owner_assigned", path=claim["path"],
                        owner_plan_task_id=owner_id, writer_plan_task_ids=[
                            f"task-{i + 1}" for i in sorted(writers)])
        for index in sorted(writers - {owner_index}):
            _compiler_event(catalog, "plan_compiler.foreign_write_routed", path=claim["path"],
                            owner_plan_task_id=owner_id, requester_plan_task_id=f"task-{index + 1}")
    return owners


def extend_compiled_write_ownership(plan: dict[str, Any],
                                    prior_plan: dict[str, Any]) -> dict[str, Any]:
    """Assign new paths in a plan revision while freezing every previous owner."""
    result = copy.deepcopy(plan)
    prior_owners = dict(prior_plan.get("write_owners", {}))
    prior_ids = {task["id"] for task in prior_plan["tasks"]}
    tasks_by_id = {task["id"]: task for task in result["tasks"]}
    for path, owner_id in prior_owners.items():
        owner = tasks_by_id.get(owner_id)
        if owner is None or path not in {owned_path_key(item)
                                         for item in owner.get("owned_paths", [])}:
            raise PlanValidationError(f"Plan revision cannot transfer ownership of {path}.")
    claims: dict[str, dict[str, Any]] = {}
    new_tasks = [task for task in result["tasks"] if task["id"] not in prior_ids]
    for task in new_tasks:
        owned = normalize_owned_paths(task.get("owned_paths", []))
        targets = normalize_owned_paths(task.get("write_targets", []))
        writes = bool(set(task.get("required_capabilities", [])) & WRITE_CAPABILITIES)
        if writes and not (owned or targets):
            raise PlanValidationError(f"New task {task['id']} lacks a concrete write target.")
        if targets and not writes:
            raise PlanValidationError(f"New task {task['id']} has write_targets without a write capability.")
        task["write_targets"] = normalize_owned_paths([*owned, *targets]) if writes else []
        task["owned_paths"] = [path for path in owned
                               if owned_path_key(path) not in prior_owners]
        for path in task["write_targets"]:
            key = owned_path_key(path)
            if key in prior_owners:
                continue
            claim = claims.setdefault(key, {"path": path, "writers": set(),
                                            "owners": set(), "creators": set()})
            claim["writers"].add(task["id"])
            if path in task["owned_paths"]:
                claim["owners"].add(task["id"])
            if "create_file" in task.get("semantic_operations", []):
                claim["creators"].add(task["id"])
        for path in task["owned_paths"]:
            key = owned_path_key(path)
            claim = claims.setdefault(key, {"path": path, "writers": set(),
                                            "owners": set(), "creators": set()})
            claim["owners"].add(task["id"])
    owners = dict(prior_owners)
    for key, claim in claims.items():
        creators, explicit, writers = claim["creators"], claim["owners"], claim["writers"]
        if len(creators) > 1:
            raise PlanValidationError(f"Two creators claim {claim['path']}.")
        if creators:
            owner_id = next(iter(creators))
        elif len(explicit) == 1:
            owner_id = next(iter(explicit))
        elif len(explicit) > 1 or len(writers) > 1:
            raise OwnershipAmbiguous(f"OwnershipAmbiguous for {claim['path']}.")
        elif writers:
            owner_id = next(iter(writers))
        else:
            raise PlanValidationError(f"No writer owns {claim['path']}.")
        owners[key] = owner_id
        for task in new_tasks:
            task["owned_paths"] = [path for path in task["owned_paths"]
                                   if owned_path_key(path) != key or task["id"] == owner_id]
        owner = tasks_by_id[owner_id]
        if key not in {owned_path_key(path) for path in owner["owned_paths"]}:
            owner["owned_paths"].append(claim["path"])
    for task in new_tasks:
        task["foreign_write_targets"] = [
            {"path": path, "owner_plan_task_id": owners[owned_path_key(path)]}
            for path in task["write_targets"]
            if owners[owned_path_key(path)] != task["id"]]
    result["write_owners"] = owners
    return result


def planned_write_target_grants(plan: dict[str, Any], task_id: str) -> list[dict[str, str]]:
    """Derive exact foreign-write grants for targets owned by dependencies."""
    if not isinstance(plan, dict) or not isinstance(task_id, str):
        return []
    tasks = plan.get("tasks")
    owners = plan.get("write_owners")
    if not isinstance(tasks, list) or not isinstance(owners, dict):
        return []
    tasks_by_id = {
        item.get("id"): item for item in tasks
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    task = tasks_by_id.get(task_id)
    if task is None:
        return []

    ancestors: set[str] = set()
    pending = list(task.get("depends_on", [])) if isinstance(task.get("depends_on"), list) else []
    while pending:
        dependency = pending.pop()
        if not isinstance(dependency, str) or dependency in ancestors:
            continue
        dependency_task = tasks_by_id.get(dependency)
        if dependency_task is None:
            continue
        ancestors.add(dependency)
        dependencies = dependency_task.get("depends_on", [])
        if isinstance(dependencies, list):
            pending.extend(dependencies)

    try:
        targets = normalize_owned_paths(task.get("write_targets", []))
    except CrossTaskRequestError:
        return []
    foreign = {}
    for item in task.get("foreign_write_targets", []):
        if not isinstance(item, dict) or set(item) != {"path", "owner_plan_task_id"}:
            continue
        try:
            foreign[owned_path_key(item["path"])] = item["owner_plan_task_id"]
        except (CrossTaskRequestError, TypeError):
            continue

    grants: list[dict[str, str]] = []
    for path in targets:
        key = owned_path_key(path)
        owner_id = owners.get(key)
        owner_task = tasks_by_id.get(owner_id)
        if (not isinstance(owner_id, str) or owner_id == task_id
                or owner_id not in ancestors or foreign.get(key) != owner_id
                or owner_task is None):
            continue
        try:
            owner_paths = {owned_path_key(value) for value in owner_task.get("owned_paths", [])}
        except (CrossTaskRequestError, TypeError):
            continue
        if key in owner_paths:
            grants.append({"path": path, "owner_plan_task_id": owner_id})
    return grants


def _compiler_event(catalog: RuntimeResourceCatalog, name: str, **details: Any) -> None:
    catalog.compiler_events.append(sanitize({"event_type": name, **details}))


def _verify_unsupported_claims(plan: dict[str, Any], spec: dict[str, Any],
                               catalog: RuntimeResourceCatalog) -> None:
    claims = plan.get("unsupported_requirements", [])
    if not isinstance(claims, list):
        raise PlanValidationError("unsupported_requirements must be a list.")
    evidence = " ".join([spec["user_intent"], *(
        item["description"] for field in ("requirements", "constraints", "deliverables")
        for item in spec[field] if item["source"] in {"explicit", "clarified"})])
    requested_actions = {action for _, _, action in _action_matches(evidence)}
    requested_categories = semantic_categories(evidence)
    requested_tokens = set(_scope_tokens(evidence))
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("semantic_need"), str):
            raise PlanValidationError("unsupported_requirements entries need semantic_need text.")
        need = claim["semantic_need"].strip()
        if not need:
            raise PlanValidationError("unsupported_requirements semantic_need is empty.")
        actions = {action for _, _, action in _action_matches(need)}
        categories = semantic_categories(need)
        external = categories & {"deployment", "publication", "network_action"}
        grounded = bool(actions & requested_actions or external & requested_categories)
        if not grounded:
            meaningful = set(_scope_tokens(need)) - {
                "unsupported", "unavailable", "resource", "runtime", "cannot", "possible"}
            grounded = bool(meaningful and meaningful <= requested_tokens)
        if not grounded:
            _compiler_event(catalog, "plan_compiler.unsupported_requirement_verified",
                            semantic_need=need[:200], verification_result="out_of_scope")
            raise PlanValidationError("Planner unsupported claim is not grounded in the Task Spec: " + need[:200])
        if not external:
            # Requested product behavior can be implemented by registered file
            # operations; it is not itself a missing runtime operation.
            artifact_request = "artifact_creation" in requested_categories
            if actions & requested_actions or artifact_request:
                _compiler_event(catalog, "plan_compiler.unsupported_requirement_verified",
                                semantic_need=need[:200], verification_result="supported_product_behavior")
                raise PlanValidationError("Planner unsupported claim describes implementable product behavior: " + need[:200])
            need_tokens = set(_scope_tokens(need))
            for operation in catalog.semantic_operations:
                operation_tokens = set(_scope_tokens(str(operation.get("id") or "").replace("_", " ")
                                                      + " " + str(operation.get("description") or "")))
                if need_tokens and len(need_tokens & operation_tokens) >= max(1, len(need_tokens) - 1):
                    _compiler_event(catalog, "plan_compiler.unsupported_requirement_verified",
                                    semantic_need=need[:200], verification_result="registered_operation_available")
                    raise PlanValidationError("Planner unsupported claim names a registered runtime action: " + need[:200])
        _compiler_event(catalog, "plan_compiler.unsupported_requirement_verified",
                        semantic_need=need[:200], category=sorted(external),
                        verification_result="genuinely_unsupported")
        raise UnsupportedResourceRequirement(
            "semantic_operation", semantic_need=need,
            reason="No registered local runtime operation can perform this requested action.")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")[:64]


def compile_semantic_task_resources(task: dict[str, Any], resource_catalog=None, *,
                                   require_task_kind: bool = False) -> dict[str, Any]:
    """Derive runtime resources from a validated semantic task in one place."""
    if not isinstance(task, dict):
        raise PlanValidationError("Semantic task must be an object.")
    resource_catalog = resource_catalog or RuntimeResourceCatalog.build()
    operations = task.get("operations", task.get("semantic_operations", []))
    if not isinstance(operations, list) or any(not isinstance(item, str) for item in operations):
        raise PlanValidationError("Semantic task operations must be a list of strings.")
    operations = list(dict.fromkeys(operations))
    capabilities, required_tools = resource_catalog.resources_for_operations(operations)
    task_kind = task.get("task_kind")
    if require_task_kind and task_kind is None:
        raise PlanValidationError("Semantic task task_kind is missing.")
    if task_kind is not None and (
            not isinstance(task_kind, str) or task_kind not in TASK_KIND_VALUES):
        raise PlanValidationError("Semantic task task_kind is not registered.")
    if task_kind in {"testing", "review"} and set(capabilities) & WRITE_CAPABILITIES:
        raise PlanValidationError(
            f"A {task_kind} task cannot own file-write capabilities."
        )
    if task_kind in {"file_creation", "program_creation", "code_change"} and not (
            set(capabilities) & WRITE_CAPABILITIES):
        raise PlanValidationError(
            f"A {task_kind} task must declare a semantic file-write operation."
        )
    if task_kind == "review" and not set(capabilities) & {
            "filesystem.read", "filesystem.list", "filesystem.search", "git.diff"}:
        raise PlanValidationError("A review task must declare a read-only inspection operation.")
    if task_kind == "testing" and not any(
            capability.startswith("execution.") for capability in capabilities):
        raise PlanValidationError("A testing task must declare a registered execution operation.")
    try:
        owned_paths = normalize_owned_paths(task.get("owned_paths", []))
        write_targets = normalize_owned_paths(task.get("write_targets", []))
    except CrossTaskRequestError as exc:
        raise PlanValidationError(f"Invalid semantic task write paths: {exc}") from exc
    if set(capabilities) & WRITE_CAPABILITIES and not (owned_paths or write_targets):
        raise PlanValidationError(
            "Every task with file-write capabilities must declare concrete write_targets or owned_paths."
        )
    compiled = {key: value for key, value in task.items()
                if key not in {"operations", "required_capabilities", "required_tools",
                               "preferred_skills"}}
    compiled["owned_paths"] = owned_paths
    compiled["write_targets"] = write_targets
    compiled["semantic_operations"] = operations
    compiled["required_capabilities"] = capabilities
    compiled["required_tools"] = required_tools
    compiled["preferred_skills"] = []
    return compiled


def compile_semantic_plan(value: Any, task_spec: dict[str, Any], *,
                          resource_catalog=None) -> dict[str, Any]:
    """Assign every internal ID and link; model supplies only task meaning."""
    spec = validate_task_spec(task_spec)
    if spec["status"] != "READY_FOR_PLANNING":
        raise PlanValidationError("Planning requires a ready Task Spec.")
    resource_catalog = resource_catalog or RuntimeResourceCatalog.build()
    resource_catalog.compiler_events = []
    value, scope_adjustments = reconcile_plan_scope(spec, value)
    resource_catalog.scope_adjustments = scope_adjustments
    _compiler_event(resource_catalog, "plan_compiler.scope_reconciled",
                    adjustments=len(scope_adjustments))
    if isinstance(value, dict):
        _verify_unsupported_claims(value, spec, resource_catalog)
    value = resource_catalog.validate_semantic_plan(value)
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        raise PlanValidationError("Semantic plan must contain tasks.")
    raw_tasks = value["tasks"]
    if not 1 <= len(raw_tasks) <= MAX_PLAN_TASKS:
        raise PlanValidationError("Semantic plan task count is invalid.")
    keys: list[str] = []
    for index, item in enumerate(raw_tasks, 1):
        if not isinstance(item, dict):
            raise PlanValidationError("Semantic tasks must be objects.")
        objective = str(item.get("objective") or "").strip()
        if not objective:
            raise PlanValidationError("Semantic task objective is missing.")
        key = _slug(str(item.get("key") or objective)) or f"step_{index}"
        if key in keys:
            raise PlanValidationError("Semantic task references are ambiguous.")
        keys.append(key)
    dependency_indexes = _dependency_indexes(raw_tasks, keys)
    if value.get('execution_strategy', 'single_worker') == 'single_worker':
        try:
            raw_tasks, keys, case_groups = group_case_tasks(raw_tasks, keys)
        except ValueError as exc:
            raise PlanValidationError(str(exc)) from exc
        for group in case_groups:
            _compiler_event(resource_catalog, 'plan_compiler.verification_cases_grouped', **group)
        dependency_indexes = _dependency_indexes(raw_tasks, keys)
    _validate_decomposition(value, raw_tasks, dependency_indexes, resource_catalog)
    preliminary_resources = [
        compile_semantic_task_resources(item, resource_catalog, require_task_kind=True)
        for item in raw_tasks
    ]
    _validate_write_scope_overlaps(
        raw_tasks, keys, preliminary_resources, dependency_indexes, resource_catalog)
    _reconcile_criterion_evidence(
        raw_tasks, keys, preliminary_resources, dependency_indexes, resource_catalog)
    write_owners = _assign_write_owners(raw_tasks, resource_catalog)
    # Compatibility for older/injected planners that omit the typed case field.
    # Read canonical intent only, and assign a global input list only when its
    # Python verifier is unambiguous. Never infer capabilities from case inputs.
    canonical_cases = explicit_cases(spec.get("user_intent", ""))
    python_verifiers = [item for item in raw_tasks if item.get("task_kind") == "testing"
                        and "run_python_script" in item.get("operations", [])]
    tasks = []
    for index, item in enumerate(raw_tasks, 1):
        resolved = []
        for dependency in item.get("depends_on", []):
            identifier = f"task-{keys.index(_slug(str(dependency))) + 1}"
            if identifier not in resolved:
                resolved.append(identifier)
        criteria = item.get("success_criteria") or []
        if not isinstance(criteria, list) or any(not isinstance(x, str) or not x.strip() for x in criteria):
            raise PlanValidationError("Semantic task checks must be text.")
        runtime_resources = compile_semantic_task_resources(
            item, resource_catalog, require_task_kind=True)
        capabilities = runtime_resources["required_capabilities"]
        required_tools = runtime_resources["required_tools"]
        task_kind = runtime_resources["task_kind"]
        semantic_needs = item.get("semantic_needs", [])
        if not isinstance(semantic_needs, list) or any(not isinstance(x, str) or not x.strip()
                                                        for x in semantic_needs):
            raise PlanValidationError("Semantic task semantic_needs must be non-empty strings.")
        owned_paths = runtime_resources["owned_paths"]
        write_targets = runtime_resources["write_targets"]
        foreign_write_targets = [{"path": path, "owner_plan_task_id":
                                  write_owners[owned_path_key(path)]}
                                 for path in write_targets
                                 if write_owners[owned_path_key(path)] != f"task-{index}"]
        for operation in runtime_resources["semantic_operations"]:
            _compiler_event(resource_catalog, "plan_compiler.semantic_operation_resolved",
                            task_key=keys[index - 1], operation=operation)
        compiled_task = {"id": f"task-{index}", "objective": str(item["objective"]).strip(),
                      "description": str(item.get("description") or item["objective"]).strip(),
                      "depends_on": resolved,
                      "semantic_operations": runtime_resources["semantic_operations"],
                      "required_capabilities": capabilities,
                      "required_tools": required_tools,
                      "semantic_needs": list(dict.fromkeys(semantic_needs)),
                      "owned_paths": owned_paths,
                      "write_targets": write_targets,
                      "foreign_write_targets": foreign_write_targets,
                      "preferred_skills": [],
                      "success_criteria": list(dict.fromkeys(criteria)) or
                                          [f"The result of {item['objective']} is verified."]}
        if task_kind is not None:
            compiled_task["task_kind"] = task_kind
        if task_kind == "testing" or item.get("verification_cases"):
            try:
                mode = item.get("verification_mode", "independent_cases")
                if mode not in MODES:
                    raise ValueError("Unknown verification mode.")
                cases = normalize_cases(item.get("verification_cases", []))
                if not cases and mode == "independent_cases":
                    cases = (canonical_cases if canonical_cases and len(python_verifiers) == 1
                             else explicit_cases(compiled_task["description"]))
                if cases:
                    if "run_command" not in required_tools:
                        raise ValueError("Verification cases require a compiled run_command resource.")
                    compiled_task.update(verification_cases=cases, verification_mode=mode)
            except ValueError as exc:
                raise PlanValidationError(str(exc)) from exc
        tasks.append(compiled_task)
    # The global obligations come from the canonical user intent. Model-proposed
    # checks may add detail, but cannot replace the user's validation expectation.
    global_criteria = list(dict.fromkeys([
        *spec["validation_expectations"],
        *[str(item).strip() for item in value.get("success_criteria", [])
          if isinstance(item, str) and item.strip()],
    ]))
    if not global_criteria:
        global_criteria = ["The requested outcome is completed and validated."]
    if len(global_criteria) > 20:
        raise PlanValidationError("Too many global criteria.")
    links = {"global": [{"id": f"GC-{index}", "criterion": criterion}
                         for index, criterion in enumerate(global_criteria, 1)],
             "local": []}
    local_number = 0
    for task in tasks:
        for criterion in task["success_criteria"]:
            local_number += 1
            links["local"].append({
                "id": f"LC-{local_number}", "task_id": task["id"], "criterion": criterion,
                "supports_global_criteria": [f"GC-{index}" for index, global_item in
                                             enumerate(global_criteria, 1) if global_item == criterion],
            })
    for warning in resource_catalog.preferred_skill_warnings_for_tasks(tasks):
        if warning not in resource_catalog.preferred_skill_warnings:
            resource_catalog.preferred_skill_warnings.append(warning)
    try:
        compiled = validate_plan({"goal": spec["objective"],
                          "summary": str(value.get("summary") or spec["objective"]).strip(),
                          "complexity": "simple" if len(tasks) == 1 else "multi_step",
                          **({field: value[field] for field in
                              ("task_complexity", "execution_strategy", "decomposition_reason")
                              if field in value}),
                          "tasks": tasks, "write_owners": write_owners,
                          "success_criteria": global_criteria,
                          "criterion_links": links})
    except PlanValidationError as exc:
        if "Conflicting write ownership" in str(exc):
            _compiler_event(resource_catalog, "plan_compiler.ownership_conflict",
                            message=str(exc)[:600])
        raise
    _compiler_event(resource_catalog, "plan_compiler.worker_assignment_created",
                    strategy=compiled.get("execution_strategy"),
                    task_count=compiled.get("task_count"),
                    worker_count=compiled.get("worker_count"),
                    worker_assignments=compiled.get("worker_assignments", []))
    _compiler_event(resource_catalog, "plan_compiler.completed",
                    task_count=len(compiled["tasks"]),
                    worker_count=compiled.get("worker_count"))
    return compiled
