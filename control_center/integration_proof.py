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


def build_proof_metadata(criteria, tasks, nodes, evaluations):
    """At most two proof candidates per criterion plus two context refs per task.

    A passed runtime check supports only the declared, satisfied local criteria
    of its task. Evaluation evidence must match both that declaration and the
    global criterion exactly after whitespace/case normalization. This is a
    conservative scope rule, not a semantic claim inferred from check prose.
    """
    catalog = {}
    proofs = {criterion_key(item): [] for item in criteria}
    task_by_id = {task["id"]: task for task in tasks}
    for node in nodes:
        tid, eid = node["plan_task_id"], node["evaluation_id"]
        catalog[f"task:{tid}"] = {"type": "task_context", "task_id": tid}
        catalog[f"evaluation:{eid}"] = {"type": "evaluation_context", "task_id": tid}
        evaluation = evaluations[eid]
        declared = {criterion_key(item) for item in task_by_id[tid]["success_criteria"]}
        local = evaluation.get("criteria") or []
        satisfied = {criterion_key(item.get("criterion", "")) for item in local
                     if isinstance(item, dict) and item.get("status") == "satisfied"}
        aligned = declared & satisfied & proofs.keys()
        verification = (((evaluation.get("snapshot") or {}).get("input") or {})
                        .get("runtime_task") or {}).get("verification") or {}
        items = verification.get("evidence") or []
        failed = bool(verification.get("failed")) or any(
            isinstance(item, dict) and item.get("status") == "failed" for item in items)
        unavailable = bool(verification.get("requested")) and (
            verification.get("unavailable") or not verification.get("attempted"))
        if failed or unavailable:
            continue
        # Use the same stable enumeration as the bounded evidence view.
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
                if any(catalog[ref]["type"] == "evaluation_evidence" for ref in proofs[key]):
                    continue
                ref = f"evidence:{eid}:{evidence_index}"
                catalog[ref] = {"type": "evaluation_evidence", "task_id": tid,
                                "criterion": key, "criterion_status": "satisfied"}
                proofs[key].append(ref)
        for index, item in enumerate(items[:20], 1):
            if not isinstance(item, dict) or item.get("status") != "passed":
                continue
            ref = f"verification:{eid}:{index}"
            for key in sorted(aligned):
                if any(catalog[old]["type"] == "verification" for old in proofs[key]):
                    continue
                catalog[ref] = {"type": "verification", "task_id": tid, "status": "passed"}
                proofs[key].append(ref)
    for key, ref in STRUCTURAL_CRITERIA.items():
        if key in proofs:
            # Caller has already proved every active node is success + accepted.
            catalog[ref] = {"type": "structural", "status": "satisfied", "criterion": key}
            proofs[key] = [ref]
    return catalog, proofs
