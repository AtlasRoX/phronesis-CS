"""
Keyed extension registries that let feature modules plug into shared pipeline stages
(prompt, evidence block, orchestrator steps/artifacts/scope notes, verifier, memory).

Every registry is idempotent: registering an existing key replaces it. Call sites wrap each hook
in try/except and log failures, so one faulty extension never breaks a turn. This module imports
nothing from phronesis, so any module may import it without circular-import risk.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# kind -> {key: value}; dicts preserve registration order.
_REGISTRIES: Dict[str, Dict[str, Any]] = {
    "prompt_component": {},
    "prompt_rule": {},
    "evidence_decorator": {},  # keyed by "<slot>::<key>"
    "step_augmenter": {},  # keyed by "<tool_key>::<key>"
    "artifact_builder": {},
    "scope_note_provider": {},
    "serialization_filter": {},
    "evidence_doc_provider": {},
    "identifier_veto": {},
    "verification_check": {},
}

EVIDENCE_SLOTS = (
    "literature_document_attrs",  # fn(passage: dict) -> Dict[str, str]
    "literature_tail",  # fn(lit_data: dict) -> str
    "lumicell_tail",  # fn(lum_data: dict) -> str
    "vivarix_attrs",  # fn(viv_data: dict) -> Dict[str, str]
    "query_entities",  # fn(ctx: QueryContext) -> str
)


def _put(kind: str, key: str, value: Any) -> None:
    if not key:
        raise ValueError(f"{kind} registration needs a non-empty key")
    registry = _REGISTRIES[kind]
    registry.pop(key, None)  # re-registering moves the entry to the end, like a fresh registration
    registry[key] = value


def unregister(kind: str, key: str) -> None:
    """Remove one registration. For evidence decorators pass key='<slot>::<key>' or use unregister_evidence_decorator."""
    _REGISTRIES[kind].pop(key, None)


def clear(kind: Optional[str] = None) -> None:
    """Remove every registration of one kind (or of all kinds). Intended for tests."""
    for k in ([kind] if kind else list(_REGISTRIES)):
        _REGISTRIES[k].clear()


def snapshot() -> Dict[str, Dict[str, Any]]:
    return {k: dict(v) for k, v in _REGISTRIES.items()}


def restore(state: Dict[str, Dict[str, Any]]) -> None:
    for k, v in state.items():
        _REGISTRIES[k].clear()
        _REGISTRIES[k].update(v)


@contextlib.contextmanager
def isolated_hooks() -> Iterator[None]:
    """Context manager for tests: registrations made inside are discarded on exit."""
    state = snapshot()
    try:
        yield
    finally:
        restore(state)


# -----------------------------------------------------------------------------
# Prompt
# -----------------------------------------------------------------------------

def register_prompt_component(key: str, line: str) -> None:
    """A line appended after the component bullet list of SYNTHESIS_SYSTEM_PROMPT (write it as '- Name: ...')."""
    _put("prompt_component", key, line)


def register_prompt_rule(key: str, text: str) -> None:
    """A rule rendered inside <additional_rules>, numbered from 9 in registration order."""
    _put("prompt_rule", key, text)


def get_prompt_components() -> List[str]:
    return list(_REGISTRIES["prompt_component"].values())


def get_prompt_rules() -> List[str]:
    return list(_REGISTRIES["prompt_rule"].values())


# -----------------------------------------------------------------------------
# Evidence block decorators
# -----------------------------------------------------------------------------

def register_evidence_decorator(slot: str, key: str, fn: Callable[..., Any]) -> None:
    if slot not in EVIDENCE_SLOTS:
        raise ValueError(f"Unknown evidence decorator slot '{slot}'; expected one of {EVIDENCE_SLOTS}")
    _put("evidence_decorator", f"{slot}::{key}", fn)


def unregister_evidence_decorator(slot: str, key: str) -> None:
    unregister("evidence_decorator", f"{slot}::{key}")


def get_evidence_decorators(slot: str) -> List[Callable[..., Any]]:
    prefix = f"{slot}::"
    return [fn for k, fn in _REGISTRIES["evidence_decorator"].items() if k.startswith(prefix)]


# -----------------------------------------------------------------------------
# Orchestrator
# -----------------------------------------------------------------------------

def register_step_augmenter(tool_key: str, key: str, fn: Callable[..., Tuple[Optional[str], List[str]]]) -> None:
    """fn(res: ToolResult, title_ctx: dict) -> (title override or None, extra detail parts joined with ' · ')."""
    _put("step_augmenter", f"{tool_key}::{key}", fn)


def unregister_step_augmenter(tool_key: str, key: str) -> None:
    unregister("step_augmenter", f"{tool_key}::{key}")


def get_step_augmenters(tool_key: str) -> List[Callable[..., Tuple[Optional[str], List[str]]]]:
    prefix = f"{tool_key}::"
    return [fn for k, fn in _REGISTRIES["step_augmenter"].items() if k.startswith(prefix)]


def register_artifact_builder(key: str, fn: Callable[..., Dict[str, Any]]) -> None:
    """fn(tool_results: Dict[str, ToolResult], ctx: QueryContext) -> dict merged into execution_artifacts."""
    _put("artifact_builder", key, fn)


def get_artifact_builders() -> List[Callable[..., Dict[str, Any]]]:
    return list(_REGISTRIES["artifact_builder"].values())


def register_scope_note_provider(key: str, fn: Callable[..., List[str]]) -> None:
    """fn(ctx: QueryContext, tool_results: Dict[str, ToolResult]) -> notes added to <tool_status>."""
    _put("scope_note_provider", key, fn)


def get_scope_note_providers() -> List[Callable[..., List[str]]]:
    return list(_REGISTRIES["scope_note_provider"].values())


# -----------------------------------------------------------------------------
# Verifier and memory
# -----------------------------------------------------------------------------

def register_serialization_filter(key: str, fn: Callable[[str, Dict[str, Any]], Dict[str, Any]]) -> None:
    """fn(tool_key, data) -> a copy of data with keys removed before identifier harvest, evidence docs and memory."""
    _put("serialization_filter", key, fn)


def get_serialization_filters() -> List[Callable[[str, Dict[str, Any]], Dict[str, Any]]]:
    return list(_REGISTRIES["serialization_filter"].values())


def apply_serialization_filters(tool_key: str, data: Any) -> Any:
    """Run every registered filter over a dict payload; filters receive and return independent copies."""
    if not isinstance(data, dict):
        return data
    filters = get_serialization_filters()
    if not filters:
        return data
    import copy

    out: Dict[str, Any] = copy.deepcopy(data)
    for fn in filters:
        try:
            filtered = fn(tool_key, out)
            if isinstance(filtered, dict):
                out = filtered
        except Exception as exc:  # noqa: BLE001
            logger.warning("Serialization filter failed for %s: %s", tool_key, exc)
    return out


def register_evidence_doc_provider(key: str, fn: Callable[..., Optional[List[Tuple[str, str]]]]) -> None:
    """fn(tool_key, result) -> [(evidence_key, lowercased doc)] replacing the whole-tool doc, or None to decline."""
    _put("evidence_doc_provider", key, fn)


def get_evidence_doc_providers() -> List[Callable[..., Optional[List[Tuple[str, str]]]]]:
    return list(_REGISTRIES["evidence_doc_provider"].values())


def register_identifier_veto(key: str, fn: Callable[..., Dict[str, Dict[str, str]]]) -> None:
    """fn(tool_results) -> {family: {id: reason}}; families 'pmids', 'dois' or any id family name."""
    _put("identifier_veto", key, fn)


def get_identifier_vetoes() -> List[Callable[..., Dict[str, Dict[str, str]]]]:
    return list(_REGISTRIES["identifier_veto"].values())


def register_verification_check(key: str, fn: Callable[["VerificationContext"], List[Dict[str, Any]]]) -> None:
    """fn(vctx) -> [{category, severity: 'CRITICAL'|'MAJOR'|'MINOR', description, suggested_correction, claim}]."""
    _put("verification_check", key, fn)


def get_verification_checks() -> List[Tuple[str, Callable[["VerificationContext"], List[Dict[str, Any]]]]]:
    return list(_REGISTRIES["verification_check"].items())


@dataclass
class VerificationContext:
    """Everything a registered verification check may read. Treat all fields as read-only."""
    query: str
    response: str
    sentences: List[str]
    table_rows: List[str]
    tool_results: Dict[str, Any]
    evidence_documents: List[str]
    evidence_keys: List[str]
    observed: Dict[str, Set[str]]  # legacy keys (biomodels, pmids, dois, ensembl, uniprot) + every id family
    cited: Dict[str, List[str]]  # ids extracted from the response for the same keys
    historical: Dict[str, Set[str]]
    context_identifiers: Dict[str, Set[str]]
    vetoed: Dict[str, Dict[str, str]]
    query_context: Any = None
    hedge_pattern: re.Pattern = field(default_factory=lambda: re.compile(r"(?!x)x"))
