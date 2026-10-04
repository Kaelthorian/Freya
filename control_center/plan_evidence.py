"""Classify plan criteria by the minimum evidence needed to prove them."""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from .plan_scope import semantic_categories


ARTIFACT_EXISTS = "artifact_exists"
STATIC_CONTENT = "static_content"
STATIC_STRUCTURE = "static_structure"
RUNTIME_BEHAVIOR = "runtime_behavior"
TEST_RESULT = "test_result"
COMPILATION_RESULT = "compilation_result"
VISUAL_RESULT = "visual_result"
EXTERNAL_STATE = "external_state"
TASK_OUTPUT = "task_output"


def normalize_criterion_reference(criterion: str) -> str:
    """Shared safe surface identity, preserving words and diacritics."""
    text = str(criterion or "").casefold()
    text = re.sub(r"(?<!`)`([\w./\\-]+\.[a-z0-9]+)`(?!`)", r"\1", text)
    return re.sub(r"\s+([.!])$", r"\1", re.sub(r"\s+", " ", text).strip())


def normalize_mechanical_criterion(criterion: str) -> str:
    """Mechanical language grammars additionally fold grammatical accents."""
    return normalize_criterion_reference(_fold(str(criterion or "")))


def verification_mode(criterion: str) -> str:
    """Decision authority is separate from the ability to gather evidence.

    Full-match grammars recognize only bare observable assertions. Mechanical
    execution still requires exact criterion evidence before deciding its truth.
    """
    text = normalize_mechanical_criterion(criterion)
    path = r"[\w./\\-]+\.[a-z0-9]+"
    nouns = r"(?:files?|archivos?|artifacts?|artefactos?)"
    prefix = r"(?:(?:the|a|an|el|la|los|las|un|una)\s+)?"
    adjective = r"(?:(?:project|required|requested|output|expected|del proyecto)\s+)?"
    named = r"(?:" + nouns + r"\s+)?" + path + r"(?:\s+" + nouns + r")?"
    multiple = named + r"(?:(?:,\s*|\s+and\s+|\s+y\s+)" + named + r")*"
    subject = prefix + r"(?:" + multiple + r"|" + adjective + nouns + r"(?:\s+del proyecto)?)"
    readable = r"(?:\s+and\s+(?:is readable|can be read)|\s+y\s+(?:es legible|puede leerse))"
    if re.fullmatch(subject + r"\s+(?:exists?|exist|existe[n]?)" + readable + r"[.!]?", text):
        return "file_readable"
    if re.fullmatch(subject + r"\s+(?:(?:is|are|esta[n]?)\s+)?(?:readable|legible[s]?)[.!]?", text):
        return "file_readable"
    if re.fullmatch(subject + r"\s+(?:(?:is|are|ha(?:ve|s)? been|fue(?:ron)?|estan?)\s+)?(?:exists?|exist|existe[n]?|created|present|saved|cread[oa]s?|guardad[oa]s?)(?:\s+(?:in|en)\s+(?:the |el )?(?:workspace|project|proyecto))?[.!]?", text):
        return "file_exists"
    if re.fullmatch(r"(?:the )?(?:script|command|program|process) "
                    r"(?:executes|runs|exits) successfully"
                    r"(?: with (?:temperature|input) [+-]?\d+(?:\.\d+)?)?[.!]?", text):
        return "command_success"
    # An aggregate execution predicate remains mechanical only when its input
    # list contains labels/values, without another behavioral assertion. Actual
    # coverage is checked against compiled cases by Evaluator, never by prose.
    input_atom = r"(?:[+-]?\d+(?:\.\d+)?|(?:an? )?[a-z-]+ input|`[^`\n]+`|'[^'\n]+')"
    input_list = input_atom + r"(?:(?:,\s*(?:and )?| and )" + input_atom + r")*"
    if re.fullmatch(r"(?:the )?(?:script|command|program|process) "
                    r"(?:executes|runs|exits) successfully with (?:inputs?|temperatures?) "
                    + input_list + r"[.!]?", text):
        return "case_set_success"
    if re.fullmatch(r"(?:all )?(?:pytest|unittest)(?: tests)? pass[.!]?", text):
        return "test_suite_success"
    if re.fullmatch(r"(?:the )?(?:program|project|code) compiles successfully[.!]?", text):
        return "compilation_success"
    if re.fullmatch(r"ruff completes successfully[.!]?", text):
        return "lint_success"
    return "semantic"


def decision_authority(criterion: str) -> str:
    """Evidence type selects collection resources; authority selects a judge."""
    return "semantic" if verification_mode(criterion) == "semantic" else "deterministic"


def mechanical_execution_requirement(criterion: str) -> str | None:
    mode = verification_mode(criterion)
    return {"command_success": "command", "case_set_success": "command", "compilation_success": "compilation",
            "lint_success": "ruff", "test_suite_success":
            "pytest" if "pytest" in normalize_mechanical_criterion(criterion) else "unittest"}.get(mode)


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in normalized
                   if not unicodedata.combining(character))


_VISUAL_PATTERNS = (
    re.compile(r"\b(?:ui|interface|page|screen|layout|interfaz|pagina|pantalla|diseno)\b.{0,80}"
               r"\b(?:looks?|appears?|renders?|visually|responsive|aligned|se ve|luce|"
               r"renderiza|visual(?:mente)?|alinead[oa]s?|correctamente)\b"),
    re.compile(r"\b(?:visual appearance|rendered appearance|apariencia visual|resultado visual)\b"),
)
_COMPILATION_PATTERNS = (
    re.compile(r"\b(?:source|code|project|program|codigo|proyecto|programa)\b.{0,70}"
               r"\b(?:compiles?|builds?|compile|compila|compilar|construye)\b"),
    re.compile(r"\b(?:compile|build|compilacion)\b.{0,70}"
               r"\b(?:succeeds?|passes?|without errors?|exitosa?|sin errores?)\b"),
    re.compile(r"\b(?:syntax|sintaxis)\b.{0,40}\b(?:valid|valida|correcta|without errors?|sin errores?)\b"),
)
_TEST_PATTERNS = (
    re.compile(r"\b(?:tests?|pytest|unittest|test suite|pruebas?)\b.{0,70}"
               r"\b(?:pass|passes|succeed|succeeds|complete|pasan|aprueban|exitosas?)\b"),
    re.compile(r"\b(?:command|process|script|comando|proceso)\b.{0,70}"
               r"\b(?:exit status|exit code|stdout|output|codigo de salida|salida)\b"),
)
_RUNTIME_PATTERNS = (
    re.compile(r"^(?:the )?(?:script|command|program|process)\b.{0,70}"
               r"\b(?:executes?|runs?|exits?) successfully\b"),
    re.compile(r"\b(?:application|app|program|calculator|interface|feature|function|operation|"
               r"aplicacion|programa|calculadora|interfaz|funcion|operacion|acciones?)\b.{0,100}"
               r"\b(?:works?|working|functional|operates?|behaves?|performs?|responds?|handles?|"
               r"funciona|funcional|responde|maneja|realiza)\b"),
    re.compile(r"\b(?:works?|working|functional|operates?|funciona|funcional)\b.{0,100}"
               r"\b(?:application|app|program|calculator|interface|feature|operation|"
               r"aplicacion|programa|calculadora|interfaz|operacion)\b"),
    re.compile(r"\b(?:application|app|program|function|aplicacion|programa|funcion)\b.{0,70}"
               r"\b(?:functions?|runs?|funciona|corre)\b.{0,30}"
               r"\b(?:correctly|as expected|correctamente|como se espera)\b"),
    re.compile(r"\b(?:returns?|produces?|outputs?|calculates?|computes?|devuelve|produce|muestra|calcula)\b"
               r".{0,100}\b(?:expected|correct|correctly|resultado esperado|correct[oa]s?|correctamente)\b"),
    re.compile(r"\b(?:addition|subtraction|multiplication|division|operations?|calculation|"
               r"suma|resta|multiplicacion|division|operaciones?|calculo)\b.{0,100}"
               r"\b(?:works?|expected|correct|correctly|funciona|resultado|correctamente)\b"),
    re.compile(r"\b(?:clicks?|input|requests?|clics?|entrada|solicitudes?)\b.{0,80}"
               r"\b(?:updates?|changes?|responds?|handles?|actualiza|cambia|responde|maneja)\b"),
)
_EXISTENCE_PATTERNS = (
    re.compile(r"\b(?:file|files|artifact|artifacts|directory|directories|archivo|archivos|"
               r"artefacto|artefactos|directorio|directorios)\b.{0,70}"
               r"\b(?:exists?|exist|created|present|available|readable|existe|existen|cread[oa]s?|"
               r"presente|disponible|legible)\b"),
    re.compile(r"\b(?:exists?|exist|created|present|available|readable|existe|existen|cread[oa]s?|"
               r"presente|disponible|legible)\b.{0,70}"
               r"\b(?:file|files|artifact|artifacts|archivo|archivos|artefacto|artefactos)\b"),
    re.compile(r"\bcan be (?:read|inspected)\b|\bpuede(?:n)? ser (?:leido|leidos|inspeccionado|inspeccionados)\b"),
)
_STATIC_STRUCTURE_PATTERNS = (
    re.compile(r"\b(?:html|css|javascript|typescript|python|source|code|markup|stylesheet|"
               r"codigo|fuente|hoja de estilo)\b.{0,100}"
               r"\b(?:contains?|includes?|defines?|declares?|references?|imports?|exports?|"
               r"contiene|incluye|define|declara|referencia|importa|exporta)\b"),
    re.compile(r"\b(?:contains?|includes?|defines?|declares?|references?|imports?|exports?|"
               r"contiene|incluye|define|declara|referencia|importa|exporta)\b.{0,100}"
               r"\b(?:handlers?|functions?|classes?|elements?|controls?|styles?|selectors?|modules?|logic|"
               r"manejadores?|funciones?|clases?|elementos?|controles?|estilos?|selectores?|modulos?|logica)\b"),
    re.compile(r"\b(?:structure|wiring|source content|static content|estructura|conexion|contenido)\b"
               r".{0,80}\b(?:present|defined|recorded|presente|definid[oa]|registrad[oa])\b"),
)


def classify_criterion(criterion: str) -> str:
    """Return the strongest minimum evidence type required by a criterion.

    Rules match predicates and their subjects, rather than rejecting a criterion
    because it contains one broad adjective such as ``correct`` or ``functional``.
    """
    mode = verification_mode(criterion)
    mechanical_evidence = {"file_exists": ARTIFACT_EXISTS, "file_readable": ARTIFACT_EXISTS,
                           "command_success": RUNTIME_BEHAVIOR, "case_set_success": RUNTIME_BEHAVIOR, "test_suite_success": TEST_RESULT,
                           "compilation_success": COMPILATION_RESULT, "lint_success": TEST_RESULT}
    if mode in mechanical_evidence:
        return mechanical_evidence[mode]
    text = _fold(str(criterion or "").strip())
    categories = semantic_categories(text)
    if categories & {"deployment", "publication", "network_action", "external_action"}:
        return EXTERNAL_STATE
    if any(pattern.search(text) for pattern in _VISUAL_PATTERNS):
        return VISUAL_RESULT
    if any(pattern.search(text) for pattern in _COMPILATION_PATTERNS):
        return COMPILATION_RESULT
    if any(pattern.search(text) for pattern in _TEST_PATTERNS):
        return TEST_RESULT
    if any(pattern.search(text) for pattern in _RUNTIME_PATTERNS):
        return RUNTIME_BEHAVIOR
    if any(pattern.search(text) for pattern in _EXISTENCE_PATTERNS):
        return ARTIFACT_EXISTS
    if any(pattern.search(text) for pattern in _STATIC_STRUCTURE_PATTERNS):
        return STATIC_STRUCTURE
    if re.search(r"\b(?:content|source|text|diff|contenido|fuente|texto|cambios?)\b", text):
        return STATIC_CONTENT
    return TASK_OUTPUT


def evidence_is_supported(evidence_type: str, capabilities: Iterable[str], *,
                          task_kind: str = "") -> tuple[bool, str]:
    """Compare one evidence requirement with the task's compiled capabilities."""
    available = {str(item) for item in capabilities}
    filesystem_evidence = bool(available & {
        "filesystem.read", "filesystem.list", "filesystem.search", "filesystem.create",
        "filesystem.modify", "filesystem.overwrite", "git.diff", "git.status",
    })
    execution_evidence = any(item.startswith("execution.") for item in available)
    if evidence_type in {ARTIFACT_EXISTS, STATIC_CONTENT, STATIC_STRUCTURE}:
        return filesystem_evidence, (
            "filesystem evidence is available" if filesystem_evidence
            else "requires filesystem read, search, diff, or write evidence"
        )
    if evidence_type in {RUNTIME_BEHAVIOR, TEST_RESULT}:
        return execution_evidence, (
            "execution evidence is available" if execution_evidence
            else "requires a registered execution or test capability"
        )
    if evidence_type == COMPILATION_RESULT:
        supported = "execution.py_compile" in available
        return supported, (
            "compiler evidence is available" if supported
            else "requires a registered compiler capability"
        )
    if evidence_type == VISUAL_RESULT:
        supported = any(item.startswith(("browser.", "render.", "vision.")) for item in available)
        return supported, (
            "visual evidence is available" if supported
            else "requires a registered browser, rendering, or vision capability"
        )
    if evidence_type == EXTERNAL_STATE:
        supported = any(item.startswith(("external.", "deployment.", "publication.", "network."))
                        for item in available)
        return supported, (
            "external-state evidence is available" if supported
            else "requires a registered external-state capability"
        )
    return True, (
        "task output can provide this evidence"
        if task_kind in {"analysis", "general", "review"} or not available
        else "the task action ledger can provide this evidence"
    )
