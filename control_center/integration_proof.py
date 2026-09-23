"""Deterministic, bounded proof metadata; no model text grants proof authority."""
from __future__ import annotations

import re


def criterion_key(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


# Exact supported invariants, deliberately not keyword or substring heuristics.
STRUCTURAL_CRITERIA = {
    "the graph reaches a terminal state.": "structural:graph-terminal",
    "all active tasks are accepted.": "structural:all-active-tasks-accepted",
}


def build_proof_metadata(criteria, tasks, nodes, evaluations, criterion_links=None):
    """Map accepted local decisions to global IDs through persisted plan links."""
    catalog = {}
    proofs = {criterion_key(item): [] for item in criteria}
    task_by_id = {task["id"]: task for task in tasks}
    links = criterion_links or {"global": [], "local": []}
    global_by_id = {item["id"]: criterion_key(item["criterion"])
                    for item in links["global"]}
    local_by_task = {}
    for item in links["local"]:
        local_by_task[(item["task_id"], criterion_key(item["criterion"]))] = item
    for node in nodes:
        tid, eid = node["plan_task_id"], node["evaluation_id"]
        catalog[f"task:{tid}"] = {"type": "task_context", "task_id": tid}
        catalog[f"evaluation:{eid}"] = {"type": "evaluation_context", "task_id": tid}
        evaluation = evaluations[eid]
        declared = {criterion_key(item) for item in task_by_id[tid]["success_criteria"]}
        local = evaluation.get("criteria") or []
        satisfied = {criterion_key(item.get("criterion", "")) for item in local
                     if isinstance(item, dict) and item.get("status") == "satisfied"}
        aligned = {}
        for key in declared & satisfied:
            link = local_by_task.get((tid, key))
            if link is None:
                continue
            for global_id in link["supports_global_criteria"]:
                global_key = global_by_id.get(global_id)
                if global_key in proofs:
                    aligned.setdefault(key, []).append((global_id, global_key, link["id"]))
        verification = (((evaluation.get("snapshot") or {}).get("input") or {})
                        .get("runtime_task") or {}).get("verification") or {}
        items = verification.get("evidence") or []
        failed = bool(verification.get("failed")) or any(
            isinstance(item, dict) and item.get("status") == "failed" for item in items)
        unavailable = bool(verification.get("requested")) and (
            verification.get("unavailable") or not verification.get("attempted"))
        if failed or unavailable:
            continue
        evidence_index = 0
        for decision in local:
            if not isinstance(decision, dict):
                continue
            key = criterion_key(decision.get("criterion", ""))
            for value in decision.get("evidence") or []:
                evidence_index += 1
                if evidence_index > 20:
                    break
                if key not in aligned or not value or decision.get("status") != "satisfied":
                    continue
                ref = f"evidence:{eid}:{evidence_index}"
                for global_id, global_key, local_id in aligned[key]:
                    if any(catalog[old]["type"] == "evaluation_evidence" for old in proofs[global_key]):
                        continue
                    if ref not in catalog:
                        catalog[ref] = {"type": "evaluation_evidence", "task_id": tid,
                                        "criterion": key, "local_criterion_id": local_id,
                                        "global_criterion_ids": [],
                                        "criterion_status": "satisfied"}
                    catalog[ref]["global_criterion_ids"].append(global_id)
                    proofs[global_key].append(ref)
        for index, item in enumerate(items[:20], 1):
            if not isinstance(item, dict) or item.get("status") != "passed":
                continue
            ref = f"verification:{eid}:{index}"
            supported = item.get("supports_acceptance_criteria")
            if isinstance(supported, list):
                applicable = {criterion_key(value) for value in supported if isinstance(value, str)}
            elif len(declared) == 1:
                applicable = declared
            else:
                applicable = set()
            for key in sorted(applicable & aligned.keys()):
                for global_id, global_key, local_id in aligned[key]:
                    if any(catalog[old]["type"] == "verification" for old in proofs[global_key]):
                        continue
                    if ref not in catalog:
                        catalog[ref] = {"type": "verification", "task_id": tid,
                                        "local_criterion_id": local_id,
                                        "global_criterion_ids": [], "status": "passed"}
                    catalog[ref]["global_criterion_ids"].append(global_id)
                    proofs[global_key].append(ref)
    for key, ref in STRUCTURAL_CRITERIA.items():
        if key in proofs:
            catalog[ref] = {"type": "structural", "status": "satisfied", "criterion": key}
            proofs[key] = [ref]
    return catalog, proofs
