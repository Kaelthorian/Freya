"""Bounded case contracts; one independent case means one controlled process."""
from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any

MAX_CASES = 20
MAX_INPUT_CHARS = 16_000
MODES = {"independent_cases", "interactive_session"}


def normalize_cases(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_CASES:
        raise ValueError("verification_cases must contain at most 20 cases.")
    cases, used = [], set()
    for index, case in enumerate(value, 1):
        if not isinstance(case, dict) or set(case) - {"id", "input"} or "input" not in case:
            raise ValueError("A verification case needs id and input only.")
        identifier = case.get("id", f"case-{index}")
        content = case["input"]
        if (not isinstance(identifier, str) or not re.fullmatch(r"[\w:-]{1,100}", identifier)
                or identifier in used or not isinstance(content, str) or len(content) > MAX_INPUT_CHARS
                or "\x00" in content):
            raise ValueError("Invalid or duplicate verification case identity/input.")
        used.add(identifier)
        cases.append({"id": identifier, "input": content})
    return cases


def explicit_cases(text: str) -> list[dict[str, str]]:
    """Conservative legacy bridge for an explicit input list, never line splitting.

    General wording is handled by Planner's structured contract. Session wording
    cannot enter this path; numbered input values must follow an explicit cue.
    """
    if re.search(r"session|sesion|username|password|usuario|contrasena|confirmation|confirmacion", text, re.I):
        return []
    matches = re.finditer(r"(?=\b(?:inputs?|entradas?|with|con)\s+"
        r"(?:(?:(?:the|los|las)\s+)?(?:temperatures?|values?|inputs?|temperaturas|valores|entradas)\s+)?(?:of\s+)?"
        r"((?:[+-]?\d+(?:\.\d+)?|[\"'][^\"']+[\"']).*))", text, re.I)
    lists = []
    for match in matches:
        cases = _explicit_input_list(match[1])
        if cases and cases not in lists:
            lists.append(cases)
    return lists[0] if len(lists) == 1 else []


def _explicit_input_list(body: str) -> list[dict[str, str]]:
    body = re.split(r"\b(?:to ensure|to verify|para verificar|para comprobar)\b", body, maxsplit=1, flags=re.I)[0]
    # Parse only a list prefix; never collect unrelated numbers later in prose.
    token = re.compile(r"[+-]?\d+(?:\.\d+)?|[\"'][^\"']+[\"']|"
        r"(?:(?:an?|una?)\s+)?(?:invalid(?:\s+input)?|entrada\s+inv[aá]lida)|[a-zA-Z]+", re.I)
    values, cursor = [], 0
    while cursor < len(body):
        item = token.match(body, cursor)
        if not item:
            return []
        value = item[0].strip()
        folded = ''.join(c for c in unicodedata.normalize('NFD', value.casefold())
                         if not unicodedata.combining(c))
        values.append('abc' if 'invalid' in folded else value.strip('"\''))
        cursor = item.end()
        separator = re.match(r"\s*(?:,\s*(?:(?:and|y)\s+)?|(?:and|y)\s+)", body[cursor:], re.I)
        if not separator:
            tail = body[cursor:].strip()
            if tail and not re.match(r'[.!](?:\s|$)', tail):
                return []
            break
        cursor += separator.end()
    if len(values) < 2 or len(values) > MAX_CASES:
        return []
    return normalize_cases([{"id": f"case-{i}", "input": value + "\n"}
                            for i, value in enumerate(dict.fromkeys(values), 1)])


def expand_case_calls(calls: list[dict[str, Any]], cases: list[dict[str, str]]) -> tuple[list[dict[str, Any]], bool]:
    """Expand one model-selected argv without bypassing normal tool/policy dispatch."""
    for index, call in enumerate(calls):
        function = call.get("function", {}) if isinstance(call, dict) else {}
        if function.get("name") != "run_command":
            continue
        import json
        arguments = function.get("arguments", {})
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(arguments, dict):
            raise ValueError("Invalid verification command arguments.")
        expanded = []
        for case in cases:
            item = copy.deepcopy(call)
            item["function"]["arguments"] = {**arguments, "stdin": case["input"]}
            item["verification_case"] = case
            expanded.append(item)
        # Preserve prerequisite inspections selected in this same decision.
        return calls[:index] + expanded, True
    return calls, False


def group_case_tasks(tasks: list[dict[str, Any]], keys: list[str]) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Coalesce equivalent independent Python case tasks without adding resources.

    Only typed cases, identical operations/dependencies and one identical script
    qualify. Ordered cases, distinct scripts and any write declaration stay intact.
    Caller validates keys/dependencies first; the resulting DAG is validated again.
    """
    def key_of(value):
        return re.sub(r'[^a-z0-9]+', '_', str(value).casefold()).strip('_')[:64]
    groups = {}
    for task, key in zip(tasks, keys):
        if (task.get('task_kind') != 'testing' or not task.get('verification_cases')
                or task.get('verification_mode') != 'independent_cases'
                or task.get('owned_paths') or task.get('write_targets')
                or task.get('operations') != ['run_python_script']):
            continue
        scripts = set(re.findall(r'(?<![\w./\\-])[\w./\\-]+\.py(?![\w./\\-])',
                                 task.get('objective', '') + ' ' + task.get('description', '')))
        if len(scripts) != 1:
            continue
        signature = (next(iter(scripts)), tuple(sorted(key_of(dep) for dep in task.get('depends_on', []))))
        groups.setdefault(signature, []).append((task, key))
    replacements, aliases, removed, diagnostics = {}, {}, set(), []
    for (_script, dependencies), members in groups.items():
        if len(members) < 2 or set(dependencies) & {key for _, key in members}:
            continue
        anchor, anchor_key = members[0]
        merged = copy.deepcopy(anchor)
        combined = []
        for task, key in members:
            for case in normalize_cases(task['verification_cases']):
                if case not in combined:
                    combined.append(case)
            aliases[key] = anchor_key
        merged['verification_cases'] = normalize_cases(combined)
        for field in ('success_criteria', 'semantic_needs'):
            merged[field] = list(dict.fromkeys(value for task, _ in members for value in task.get(field, [])))
        merged['description'] = '\n'.join(task.get('description', '') for task, _ in members)
        replacements[anchor_key] = merged
        removed.update(key for _, key in members[1:])
        diagnostics.append({'source_task_keys': [key for _, key in members], 'target_task_key': anchor_key,
                            'case_ids': [case['id'] for case in combined]})
    result, result_keys = [], []
    for task, key in zip(tasks, keys):
        if key in removed:
            continue
        item = copy.deepcopy(replacements.get(key, task))
        item['depends_on'] = list(dict.fromkeys(aliases.get(key_of(dep), dep) for dep in item.get('depends_on', [])))
        result.append(item)
        result_keys.append(key)
    return result, result_keys, diagnostics
