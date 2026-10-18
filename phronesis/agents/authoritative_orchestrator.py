"""
Authoritative Biomedical Multi-Agent Orchestrator for Phronesis.
Implements:
- Biomni (2025) & ReAct (2022) unified multi-specialist coordination
- LLMCompiler (2023) dependency-aware DAG scheduling with bounded concurrency
- PaperQA2 (2024) grounded literature synthesis via Lithoscribe
- COPASI / BASICO executable kinetic ODE modeling via Vivarix
- CellAgent (2024) single-cell transcriptomics analytics via Lumicell
- Reflexion (2023) deterministic verification and bounded correction
- Mem0 (2025) isolated persistent memory with true BM25 retrieval
- BioDiscoveryAgent (2024) evidence-backed hypothesis prioritization
"""

from __future__ import annotations

# The inquiry stages live in phronesis/agents/orchestration/. They look up the functions imported here through
# this module at call time, so these names stay the patch points tests use (get_llm, get_tool_registry,
# build_citation_map, ...) and stay importable from here; `noqa: F401` marks the ones only the stages use.
import asyncio
import contextlib
import hashlib  # noqa: F401
import json  # noqa: F401
import logging
import os
import re
import time
import uuid  # noqa: F401
from collections import defaultdict  # noqa: F401
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple  # noqa: F401
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: F401
from pydantic import BaseModel, Field

from ..agentic import config as agent_config
from ..agentic.budget import RunBudget  # noqa: F401
from ..agentic.control import RunCancelled, RunControl, create_control, release_control
from ..agentic.evidence import render_agent_evidence  # noqa: F401
from ..agentic.guardrails import SPOTLIGHT_RULE  # noqa: F401
from ..agentic.llm import AgentLLM  # noqa: F401
from ..agentic.runtime import AgenticRuntime  # noqa: F401
from ..agentic.state import AgentState  # noqa: F401
from ..datasets.attach import resolve_attachments  # noqa: F401
from ..datasets.provenance import postprocess_lumicell_result, postprocess_vivarix_result  # noqa: F401
from ..extensions import load_extensions  # noqa: F401
from ..utils.text import fold_typography
from ..extensions.hooks import get_artifact_builders, get_scope_note_providers, get_step_augmenters  # noqa: F401
from ..phronesis.agents.verifier import ClinicalVerifier, VerificationReport  # noqa: F401
from ..provenance.run_manifest import RunRecorder, activate_recorder, reset_recorder
from ..sources import EvidenceSource, SourceRunState, get_sources  # noqa: F401
from ..sources.names import INTERNAL_ONLY_KEYS
from ..sources.names import public_name as source_public_name
from ..tools.registry import BiomedicalToolRegistry, ErrorType, ToolResult, get_tool_registry
from ..utils.llm.llm_factory import DEFAULT_ORCHESTRATOR_MODEL, astream_with_reasoning, get_llm, stream_timeout  # noqa: F401
from ..utils.memory import get_clinical_memory_engine
from ..utils.memory.clinical_memory import MemoryContextPackage  # noqa: F401
from ..vivarix.engine import VivarixSimulationEngine
from . import query_planner
from .citations import build_citation_map  # noqa: F401
from .discovery import BioDiscoveryEngine, DiscoveryHypothesisReport  # noqa: F401
from .prompts import (
    CORRECTION_SYSTEM_PROMPT,  # noqa: F401
    build_correction_request,  # noqa: F401
    build_evidence_block,  # noqa: F401
    build_synthesis_request,  # noqa: F401
    build_tool_status_block,  # noqa: F401
    get_synthesis_system_prompt,  # noqa: F401
)
from .query_context import (
    SIM_DURATION_MAX_SECONDS,
    SIM_DURATION_MIN_SECONDS,
    SIM_SCAN_MAX_POINTS,
    SimulationPlan,
    SourceDecision,  # noqa: F401
    TaskOutcome,
    TaskStatus,
)
from .query_planner import build_query_context  # noqa: F401
from .orchestration import agent_rounds
from .orchestration import draft as draft_stage
from .orchestration import finalize as finalize_stage
from .orchestration import gather as gather_stage
from .orchestration import plan as plan_stage
from .orchestration import verify as verify_stage
from .orchestration.context import RunContext

logger = logging.getLogger(__name__)


class OrchestrationStep(BaseModel):
    agent: str
    role: str = "assistant"
    action: Optional[str] = None
    duration_seconds: float = 0.0


class OrchestrationThought(BaseModel):
    stage: str
    title: str
    text: str
    elapsed_seconds: float = 0.0
    agent: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


class AuthoritativeOrchestrationResult(BaseModel):
    thread_id: str
    response: str
    thoughts: List[Dict[str, Any]] = Field(default_factory=list)
    reasoning: str = ""
    steps: List[Dict[str, Any]] = Field(default_factory=list)
    selected_agents: List[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
    model_used: str = DEFAULT_ORCHESTRATOR_MODEL
    framework_used: str = "authoritative-react-compiler"
    verification_report: Optional[Dict[str, Any]] = None
    execution_artifacts: Dict[str, Any] = Field(default_factory=dict)
    citations: Dict[str, Any] = Field(default_factory=dict)
    run_manifest: Optional[Dict[str, Any]] = None
    query_context: Optional[Dict[str, Any]] = None
    task_status: Optional[Dict[str, Any]] = None  # query_context.TaskOutcome; separate from verification

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


def _make_recorder(**kwargs: Any) -> Optional[RunRecorder]:
    try:
        return RunRecorder(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Run recorder unavailable: %s", exc)
        return None


# Lenticular-bracket citation markers some models emit: 【PMID: 123】, 【42310679】, 【clinical_trials】.
_CITE_MARKER_RE = re.compile(r"\s?【([^】\n]{0,160})】")
_MARKER_ID_RE = re.compile(
    r"\b(?:PMID:?\s*\d{1,9}|10\.\d{4,9}/\S+|NCT\d{8}|CHEMBL\d{1,9}|BIOMD\d{10}|VCV\d{9}|rs\d{3,12}|[EA]ID\d{1,6}|GS[EM]\d{1,9}|ENSG\d{11}|DB\d{5})",
    re.IGNORECASE,
)

# Greetings tolerate stretched or misspelled forms ('heelooo', 'hiii', 'heyy') so they never reach the tools.
_GREETING = (
    r"(?:h+e+l+o+w*|h+a+l+o+|h+i+y*a*|h+e+y+a*|y+o+|s+u+p+|h+o+l+a+|h+o+w+d+y+|greetings|"
    r"good\s*(?:morning|afternoon|evening|day)|help)"
)
_CAPABILITY = (
    r"(?:who\s+are\s+you|what\s+are\s+you|what\s+(?:can|do)\s+you\s+do|how\s+can\s+you\s+help|"
    r"what\s+(?:are\s+your\s+capabilities|is\s+this(?:\s+app)?)|introduce\s+yourself|tell\s+me\s+about\s+yourself)"
    r"(?:\s+(?:(?:for|to)\s+)?(?:m+e+|u+s+))?(?:\s+(?:here|exactly|then))?"
)
_CASUAL_RES = [
    re.compile(rf"^{_GREETING}(?:\s+(?:there|all|everyone|team|phronesis))?[\s\.\?!,~:)]*$", re.I),
    # Greeting plus small talk only: anything after it ("Hi, how are you? What are the phase 3 results for
    # sotorasib", "Hello, help me evaluate EGFR") is a real question and goes to the planner.
    re.compile(
        rf"^{_GREETING}[\s,!\.]+(?:{_CAPABILITY}|how\s+are\s+(?:you|u)(?:\s+doing)?(?:\s+today)?|help(?:\s+me)?)[\s\.\?!,~:)]*$",
        re.I,
    ),
    re.compile(rf"^(?:so\s+|and\s+|ok\s+|okay\s+)?{_CAPABILITY}[\s\.\?!,~:)]*$", re.I),
]


_INTRODUCTION = (
    "Hello! I am **Phronesis**, your evidence-backed Clinical R&D AI assistant.\n\n"
    "I provide publication-grade scientific research and systems biology capabilities:\n"
    "- **🧬 Synapta**: Knowledge Graph topology, OpenTargets genetic associations, KEGG pathway mapping, "
    "HGNC/OpenTargets entity normalization, ChEMBL drug mechanisms and bioactivities, and variant evidence "
    "from ClinVar, CIViC, cBioPortal and gnomAD\n"
    "- **⚙️ Vivarix**: Executable COPASI/BASICO kinetic ODE simulations of curated BioModels or your uploaded SBML models\n"
    "- **🔬 Lumicell**: Single-cell transcriptomics (Scanpy/AnnData) on validated real datasets, including your uploaded .h5ad/.csv files\n"
    "- **📚 Lithoscribe**: Grounded literature synthesis with real full-text XML from EuropePMC/PubMed (graded by study "
    "design and checked for retractions), ClinicalTrials.gov registry records with posted results, and openFDA "
    "adverse-event reports and labels\n"
    "- **🌐 WebSurfer**: Real-time biomedical literature and trial search\n"
    "- **🔍 Reflexion Verifier**: Mathematical and citation auditing preventing ungrounded claims\n\n"
    "Answers carry clickable citations and can be exported as a DOCX or PDF report with a run manifest, "
    "then re-run to compare.\n\n"
    "To get started, please ask a biomedical question, specify a target gene/protein (e.g. *KRAS*, *EGFR*, *BRAF*), or provide a scientific topic for literature review!"
)

_EXAMPLE_QUESTIONS = (
    "- *Is KRAS G12C a tractable target in non-small cell lung cancer?*\n"
    "- *What adverse events are reported for sotorasib?*\n"
    "- *Simulate BIOMD0000000010 for 60 seconds.*"
)
_THANKS_RE = re.compile(r"\b(?:thanks?|thx|ty|cheers|appreciate)\b", re.I)
_FAREWELL_RE = re.compile(r"\b(?:bye|goodbye|see\s+(?:you|ya)|good\s*night)\b", re.I)


def _non_research_reply(query: str, scope: str) -> str:
    """A fixed reply for a message that is not a research question. Nothing is looked up, so nothing is claimed."""
    if scope == "conversational":
        if _THANKS_RE.search(query):
            return "You're welcome! Ask me about a target, drug, disease, variant or BioModel whenever you're ready."
        if _FAREWELL_RE.search(query):
            return "Goodbye! Your conversation stays here when you come back."
        return _INTRODUCTION
    shown = " ".join(query.split())
    shown = shown if len(shown) <= 80 else shown[:77] + "..."
    shown = re.sub(r"([\\`*_\[\]<>])", r"\\\1", shown)
    return (
        f"I couldn't find a biomedical question in “{shown}”, so I didn't search any sources.\n\n"
        "Ask about a gene or protein target, a drug, a disease, a variant or a BioModel, or attach a dataset. "
        f"For example:\n{_EXAMPLE_QUESTIONS}"
    )


_BIOMODEL_LITERATURE = {
    "BIOMD0000000010": "Kholodenko MAPK cascade ultrasensitivity negative feedback oscillations 10712587",
    "BIOMD0000000019": "Hatakeyama ErbB receptor MAP kinase 12683957",
    "BIOMD0000000372": "Sedaghat insulin receptor signaling 12376338",
}


# Trial acronyms written in a question ("AURA3", "KEYNOTE-716", "CheckMate 76K", "SOLO-1"): a mixed/upper-case word
# with at least two capitals followed by a number. Genes and drug codes the resolver recognised are excluded below.
_QUESTION_TRIAL_NAME_RE = re.compile(r"\b([A-Z][A-Za-z]{2,}(?:[\s-]?\d{1,4}[A-Z]{0,2}))\b")
_NOT_TRIAL_WORDS = {"COVID", "SARS", "ICD", "ECOG", "RECIST", "CTCAE", "TNM", "AJCC", "WHO"}


def question_trial_names(ctx: Any, text: str) -> List[str]:
    """Named trials mentioned in the question, so literature search can look up their publications directly."""
    known = set()
    for group in ("genes", "drugs", "variants"):
        for ent in getattr(ctx, group, None) or []:
            for attr in ("mention", "label", "symbol", "name"):
                val = getattr(ent, attr, None)
                if isinstance(val, str) and val:
                    known.add(re.sub(r"[\s-]", "", val).upper())
    out: List[str] = []
    for m in _QUESTION_TRIAL_NAME_RE.finditer(text or ""):
        name = m.group(1)
        word = re.match(r"[A-Za-z]+", name).group(0)  # type: ignore[union-attr]
        if sum(c.isupper() for c in word) < 2 or word.upper() in _NOT_TRIAL_WORDS:
            continue
        if re.sub(r"[\s-]", "", name).upper() in known or word.upper() in known:
            continue
        if name not in out:
            out.append(name)
    return out


_GENERIC_DISEASE_WORDS = {
    "cancer", "cancers", "carcinoma", "carcinomas", "tumor", "tumors", "tumour", "tumours", "neoplasm", "neoplasms",
    "disease", "diseases", "syndrome", "disorder", "malignant", "primary", "adenocarcinoma", "of", "the", "and", "cell",
}


_INHERITED_WORDS = {"familial", "hereditary", "inherited", "germline", "syndrome"}


_TRANSIENT_STREAM_ERRORS = ("ReadError", "RemoteProtocolError", "APIConnectionError", "ConnectError", "ReadTimeout",
                            "APITimeoutError", "InternalServerError")


async def stream_buffered_with_retry(llm: Any, messages: List[Any], parts: List[str], *, on_reasoning: Any = None,
                                     attempts: int = 2, before_retry: Optional[Callable[[int], None]] = None,
                                     timeout_s: Optional[float] = None) -> Tuple[str, str]:
    """A buffered (not shown live) completion, retried once from scratch when the connection drops mid-stream
    ("ReadError"): the buffer is cleared, so nothing partial or duplicated is kept.

    before_retry(attempt) runs before each retry: a retry is another provider call, so an agentic caller settles
    the dropped call and admits the retry there (it raises BudgetExhausted when the retry cannot be afforded).
    timeout_s bounds each stream by the run's remaining time (None: STREAM_TOTAL_TIMEOUT); the retry's pause and
    attempt never extend past it."""
    t_end = None if timeout_s is None else time.perf_counter() + max(0.0, timeout_s)
    for i in range(attempts):
        parts.clear()

        async def buffer(delta: str) -> None:
            parts.append(delta)

        kwargs: Dict[str, Any] = {}
        if t_end is not None:
            kwargs["total_timeout"] = stream_timeout(max(0.0, t_end - time.perf_counter()))
        try:
            return await astream_with_reasoning(llm, messages, on_reasoning=on_reasoning, on_text=buffer, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if i + 1 >= attempts or type(exc).__name__ not in _TRANSIENT_STREAM_ERRORS:
                raise
            if t_end is not None and t_end - time.perf_counter() <= 2.0:
                raise  # no time left in the run for a second attempt
            if before_retry is not None:
                before_retry(i + 1)
            logger.warning("Model stream dropped (%s); retrying once", type(exc).__name__)
            await asyncio.sleep(2.0)
    return "", ""


def _edit_llm_kwargs(model_name: str) -> Dict[str, Any]:
    """Rewrites and revisions edit an existing draft: the model's long reasoning pass adds minutes (a revision hit
    the 600 s stream cap) without improving an edit. The first draft keeps its reasoning."""
    from ..agentic.llm import agent_model_kwargs

    return agent_model_kwargs(model_name)


def question_disease_words(ctx: Any) -> List[set]:
    """Distinctive words of each disease the question names ({"pancreatic"}, {"cutaneous", "melanoma"}). A class term
    ("cancer", "solid tumors", "malignancy") has none, so it never ranks associations next to a specific disease."""
    generic = _GENERIC_DISEASE_WORDS | {"malignancy", "malignancies", "solid", "neoplastic", "human", "illness", "disorders"}
    out: List[set] = []
    for ent in getattr(ctx, "diseases", None) or []:
        for attr in ("mention", "label"):
            text = getattr(ent, attr, None)
            words = {w for w in re.findall(r"[a-z]{3,}", str(text or "").lower()) if w not in generic}
            if words and words not in out:
                out.append(words)
    return out


def prioritise_question_diseases(assocs: List[Dict[str, Any]], q_diseases: List[set], keep: int = 5) -> List[Dict[str, Any]]:
    """Associations with the question's disease first (stable), then the strongest others, `keep` in all."""
    if not q_diseases:
        return list(assocs)[:keep]

    def matches(a: Dict[str, Any]) -> bool:
        name = set(re.findall(r"[a-z]{3,}", str(a.get("disease_name") or "").lower()))
        # "pancreatic" matches "pancreatic adenocarcinoma"; a stem match catches "pancreas" vs "pancreatic".
        return any(words <= name or any(n[:6] == w[:6] for w in words for n in name if len(w) >= 6) for words in q_diseases)

    hit = [a for a in assocs if matches(a)]
    rest = [a for a in assocs if not matches(a)]
    # "familial pancreatic carcinoma" matches "pancreatic" too, but a germline form is not the somatic disease a
    # "KRAS-mutant pancreatic cancer" question means: inherited forms go after the plain disease unless asked for.
    asked_inherited = any(w in _INHERITED_WORDS for words in q_diseases for w in words)
    if not asked_inherited:
        hit.sort(key=lambda a: bool(_INHERITED_WORDS & set(re.findall(r"[a-z]+", str(a.get("disease_name") or "").lower()))))
    return (hit + rest)[: max(keep, min(len(hit), keep + 3))]


def literature_request(ctx: Any, clean_query: str, plan: Any = None) -> Dict[str, Any]:
    """Arguments of the Lithoscribe call for a question (shared with the evaluation harness, so both search alike)."""
    from ..agentic.config import get_effort_profile
    effort_level = getattr(ctx, "effort_level", None) or "medium"
    profile = get_effort_profile(effort_level)
    max_articles = profile.get("max_passages", 8)

    if ctx.biomodel_id in _BIOMODEL_LITERATURE:
        return {"query": _BIOMODEL_LITERATURE[ctx.biomodel_id], "max_articles": max_articles}
    if plan is not None and hasattr(plan, "trial_names") and not plan.trial_names:
        from .search_plan import clean_trial_names

        plan.trial_names = clean_trial_names(question_trial_names(ctx, getattr(ctx, "raw_query", "") or clean_query))
    queries = plan.europepmc_queries() if plan is not None else []
    if queries:
        keywords = plan.keywords() if plan is not None else []
        return {"query": clean_query, "max_articles": max_articles, "search_queries": queries, "keywords": keywords}
    return {"query": clean_query, "max_articles": max_articles}


def _show_reasoning() -> bool:
    """Raw model reasoning is kept server-side unless PHRONESIS_SHOW_REASONING is set: it is not checked by the verifier."""
    return os.getenv("PHRONESIS_SHOW_REASONING", "").strip().lower() in ("1", "true", "yes", "on")


def _cite_marker(m: "re.Match[str]") -> str:
    """Keep the identifiers of a bracket marker as a plain parenthetical citation; drop markers naming evidence tags."""
    inner = m.group(1).strip()
    ids = [i.group(0) for i in _MARKER_ID_RE.finditer(inner)]
    if not ids and re.fullmatch(r"\d{6,9}", inner):
        ids = [f"PMID: {inner}"]  # a bare PMID-length number inside a marker
    if not ids:
        return ""
    ids = ["PMID: " + re.sub(r"\D", "", i) if i.upper().startswith("PMID") else i for i in ids]
    return f" ({'; '.join(dict.fromkeys(ids))})"


# Internal source keys the model sometimes copies into the answer as a parenthetical "citation": their public
# names (from the shared SOURCE_REGISTRY), or None to drop the parenthetical (it names a pipeline step, not a source
# the reader can check). Only these keys are rewritten; other source keys in parentheses are left as written.
_INTERNAL_SOURCE_KEYS = (
    "structure_intel", "structintel", "clinical_trials", "clinicaltrials", "synapta", "lithoscribe", "vivarix",
    "lumicell", "websurfer", "disease_targets",
)
_INTERNAL_SOURCE_NAMES: Dict[str, Optional[str]] = {
    **{k: source_public_name(k) for k in _INTERNAL_SOURCE_KEYS},
    **dict.fromkeys(INTERNAL_ONLY_KEYS),
}
# Evidence attribute keys and how a value reads in prose ("total_reports = 1454" -> "1,454 FAERS reports").
_FIELD_PROSE = {
    "total_reports": "{} FAERS reports",
    "serious_reports": "{} serious reports",
    "reports_listing_death": "{} reports listing death",
    "application_number": "application number {}",
    "overall_score": "overall score {}",
}
_FIELD_VALUE_RE = re.compile(
    r"\b(" + "|".join(_FIELD_PROSE) + r")\b(?:\s*[=:]\s*|\s+)((?:[A-Z]{1,4})?\d+(?:[.,]\d+)*)\b"
)
_FIELD_BARE_RE = re.compile(r"\b(" + "|".join(_FIELD_PROSE) + r"|structure_intel|clinical_trials)\b")
_PAREN_RE = re.compile(r"[ \t]*\(([^()\n]{1,80})\)")
# Internal evidence keys of agent steps ("agent.plan_execute.4.structure", "agent.gaps.9-12 lithoscribe"): with
# their parentheses or brackets, a lead-in like "evidence:", and list separators, they are removed from the answer.
_STEP_KEY = (
    r"agent\.[a-z_]+(?:\.[\w-]+)+"
    r"(?:\s+(?:lithoscribe|synapta|vivarix|lumicell|chembl|openfda|primekg|structure|websurfer|dgidb|pharos|"
    r"bindingdb|pubtator|synlethdb|clinical_trials|trials|variants|gwas|gtex|string_ppi|reactome|pubchem))?"
)
_STEP_KEY_RE = re.compile(
    rf"[ \t]*[(\[](?:(?:evidence|source|see)\s*:?\s*)?{_STEP_KEY}(?:\s*[,;]\s*{_STEP_KEY})*[)\]]"
    rf"|(?:\b(?:evidence|source)\s*:\s*)?\b{_STEP_KEY}(?:\s*[,;]\s*{_STEP_KEY})*",
    re.IGNORECASE,
)
# "the BioDiscovery evidence-weighted ranking", "BioDiscovery ranking": an internal component named in prose.
_BIODISCOVERY_RE = re.compile(r"\b(?:the\s+)?BioDiscovery(?:'s)?(?:\s+(?:evidence-weighted\s+)?(?:ranking|score))?(?![\w-])", re.IGNORECASE)
# A bracketed tag that is not a Markdown link or checkbox: "[Synapta]", "[Lithoscribe; PMID: 1]".
_BRACKET_RE = re.compile(r"[ \t]*\[([^\[\]\n]{2,80})\](?!\()")
_SNAKE_KEY_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
# Typographic spaces and hyphens the model writes ("et al.", "on‑target"): shown as plain characters so the
# answer reads the same everywhere and a flagged claim's sentence can be found again in it (claim removal). The table
# is shared with the verifier (phronesis.utils.text.fold_typography), so both sides fold the same characters.


def normalize_typography(text: str) -> str:
    return fold_typography(text) if text else text


# Code, URLs and Markdown link targets are left exactly as written.
_PROTECTED_RE = re.compile(r"```.*?```|`[^`\n]*`|https?://[^\s)>\]]+|\]\([^)\s]*\)", re.S)


def _field_prose(m: "re.Match[str]") -> str:
    value = m.group(2)
    if value.isdigit() and len(value) > 3:
        value = f"{int(value):,}"
    return _FIELD_PROSE[m.group(1)].format(value)


def _public_paren(m: "re.Match[str]") -> str:
    """A parenthetical naming internal source keys, rewritten with public names (or dropped); others unchanged."""
    items = [i.strip() for i in re.split(r"[;,]", m.group(1))]
    keys = [re.sub(r"[\s\-]+", "_", i.lower()) for i in items]
    # A bare snake_case token ("hypothesis_experiments", "tool_status") is an evidence key, never prose.
    snake = [bool(_SNAKE_KEY_RE.fullmatch(i)) for i in items]
    if not any(k in _INTERNAL_SOURCE_NAMES for k in keys) and not any(snake):
        return m.group(0)
    kept = [
        _INTERNAL_SOURCE_NAMES[k] if k in _INTERNAL_SOURCE_NAMES else ("" if is_key else i)
        for i, k, is_key in zip(items, keys, snake)
    ]
    kept = list(dict.fromkeys(k for k in kept if k))
    lead = m.group(0)[: m.start(1) - m.start(0) - 1]
    return f"{lead}({'; '.join(kept)})" if kept else ""


def _bare_key(m: "re.Match[str]") -> str:
    key = m.group(1)
    return _INTERNAL_SOURCE_NAMES.get(key) or key.replace("_", " ")


# Data field names and enum values of the SynLethDB/DGIdb druggable-partner evidence ("druggable_partners",
# "fda_status = not_checked", "interaction_class: untyped") the model sometimes copies into prose: a safety net
# behind the evidence wording, so the reader sees plain words. Applied before the parenthetical rules, so
# "(fda_status = not_checked)" keeps its parentheses as "(FDA status not checked)".
_FDA_STATUS_PROSE = {
    "not_checked": "FDA status not checked",
    "approved": "FDA-approved",
    "not_approved": "not FDA-approved",
    "error": "FDA status lookup failed",
    "unknown": "FDA status unknown",
    "not_found": "no FDA approval record found",
}
_INTERACTION_CLASS_PROSE = {
    "inhibits": "inhibitor-type interaction",
    "other": "interaction of another type",
    "untyped": "interaction type not specified",
}
_DRUGGABLE_PARTNER_DATA = "SynLethDB/DGIdb druggable-partner data"
_DATA_FIELD_RULES: Tuple[Tuple["re.Pattern[str]", Any], ...] = (
    (
        re.compile(r"\bfda_status\s*(?:=|:|is)\s*[\"'“”]?([a-z_]+)[\"'“”]?", re.IGNORECASE),
        lambda m: _FDA_STATUS_PROSE.get(m.group(1).lower(), f"FDA status {m.group(1).replace('_', ' ')}"),
    ),
    (
        re.compile(r"\binteraction_class\s*(?:=|:|is)\s*[\"'“”]?([a-z_]+)[\"'“”]?", re.IGNORECASE),
        lambda m: _INTERACTION_CLASS_PROSE.get(m.group(1).lower(), f"interaction type {m.group(1).replace('_', ' ')}"),
    ),
    # "druggable_partners table", "the druggable_partners list": the data, named by its sources.
    (re.compile(r"\bdruggable_partners?(?:\s+(?:table|list|data|field|rows?|section|array|entries|output))?\b", re.IGNORECASE),
     lambda m: _DRUGGABLE_PARTNER_DATA),
    (re.compile(r"\bdruggable_lookup\b", re.IGNORECASE), lambda m: "druggable-partner lookup"),
    (re.compile(r"\bpartners_not_checked\b", re.IGNORECASE), lambda m: "partners not checked"),
    (re.compile(r"\bfda_status\b", re.IGNORECASE), lambda m: "FDA status"),
    (re.compile(r"\binteraction_class\b", re.IGNORECASE), lambda m: "interaction type"),
    (re.compile(r"\bnot_checked\b", re.IGNORECASE), lambda m: "not checked"),
    # "an untyped DGIdb interaction" / "untyped interactions": the interaction type is simply not given.
    (re.compile(r"\b(an?\s+)?untyped\s+((?:DGIdb\s+)?)(drug[- ]gene\s+)?interactions?\b", re.IGNORECASE),
     lambda m: (("a " if m.group(2) or m.group(3) else "an ") if m.group(1) else "")
     + f"{m.group(2) or ''}{m.group(3) or ''}{m.group(0).rsplit(None, 1)[-1]} of unspecified type"),
    (re.compile(r"\((?:class\s*[:=]?\s*)?untyped\)", re.IGNORECASE), lambda m: "(type not specified)"),
)


def _clean_data_fields(segment: str) -> str:
    for pattern, repl in _DATA_FIELD_RULES:
        segment = pattern.sub(repl, segment)
    return segment


# The tags the evidence block wraps documents in (agentic/evidence.py, agentic/guardrails.py spotlight,
# agentic/analyst.py, agents/prompts.py). A model sometimes copies them into the answer, raw, entity-escaped
# ("&lt;untrusted_data source=...&gt;") or in a code span; they are markup for the model, never prose.
_EVIDENCE_TAGS = ("agent_evidence", "untrusted_data", "tool_output", "document", "sandbox_analysis")
_EVIDENCE_TAG_RE = re.compile(
    r"`?(?:<|&lt;|&#60;|&#x3c;)\s*/?\s*(?:" + "|".join(_EVIDENCE_TAGS) + r")\b"
    r"(?:\s+[a-z_][\w\-]*\s*=\s*(?:\"[^\"<>\n]*\"|'[^'<>\n]*'|&quot;.*?&quot;|[^\s<>&`]+))*"
    r"\s*/?\s*(?:>|&gt;|&#62;|&#x3e;)`?",
    re.IGNORECASE,
)
# A pointer to one of the evidence block's own sections: "(see `<literature_limitations>`)", "`<tool_status>`".
# Only snake_case tag names: those are the prompt's internal sections, never prose or real markup.
_INTERNAL_TAG_REF_RE = re.compile(
    r"\s*\((?:see|in|per|from|cf\.?)\s+`?(?:<|&lt;)/?[a-z]+(?:_[a-z]+)+(?:>|&gt;)`?\)"
    r"|`(?:<|&lt;)/?[a-z]+(?:_[a-z]+)+(?:>|&gt;)`"
)
# An attribute the model copied without its tag ("key=\"\"", "pmid=\"\" doi=\"\"") says nothing.
_EMPTY_ATTR_RE = re.compile(r"(?<![\w\-])(?:key|pmid|doi|tool|phase|purpose|source)\s*=\s*(?:\"\"|''|&quot;&quot;)", re.IGNORECASE)


def strip_evidence_markup(text: str) -> str:
    """The answer without the evidence block's tags (the prose inside them is kept), references to the evidence
    block's own sections ("(see `<literature_limitations>`)") or empty copied attributes."""
    if not text or not (
        any(t in text.lower() for t in (*_EVIDENCE_TAGS, '=""', "=''", "=&quot;&quot;")) or _INTERNAL_TAG_REF_RE.search(text)
    ):
        return text
    out = _INTERNAL_TAG_REF_RE.sub("", text)
    out = _EVIDENCE_TAG_RE.sub("", out)
    out = _EMPTY_ATTR_RE.sub("", out)
    out = re.sub(r"[ \t]{2,}(?=\S)", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def clean_internal_keys(text: str) -> str:
    """Rewrite evidence field names and internal source keys the model copied into the answer as plain prose.

    "total_reports = 1454" becomes "1,454 FAERS reports", "(clinical_trials)" becomes "(ClinicalTrials.gov)" and
    "(tool_status note)" is removed. Code spans, URLs and link targets are untouched; identifiers (NCT, PMID,
    CHEMBL) never match the patterns.
    """
    if not text:
        return text

    def clean(segment: str) -> str:
        segment = _FIELD_VALUE_RE.sub(_field_prose, segment)
        segment = _PAREN_RE.sub(_public_paren, segment)
        segment = _BRACKET_RE.sub(_public_paren, segment)  # "[Synapta]" citation-style tags
        segment = _BIODISCOVERY_RE.sub("the evidence-weighted ranking", segment)
        segment, removed = _STEP_KEY_RE.subn("", segment)  # "(agent.gaps.9.lithoscribe)", "evidence: agent.swarm.2.chembl"
        if removed:
            segment = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", segment)  # the gap a removed key left (not "  \n" breaks)
        return _FIELD_BARE_RE.sub(_bare_key, segment)

    out: List[str] = []
    pos = 0
    for m in _PROTECTED_RE.finditer(text):
        out.append(clean(text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(clean(text[pos:]))
    return "".join(out)


class AuthoritativeOrchestrator:
    """
    Authoritative Orchestrator coordinating Synapta, Vivarix, Lumicell,
    Lithoscribe, and WebSurfer with genuine tool execution and verification.
    """

    def __init__(self) -> None:
        # Tools run through the registry; the engine is kept only for BioModels search.
        self.vivarix = VivarixSimulationEngine()
        self.verifier = ClinicalVerifier()
        self.memory = get_clinical_memory_engine()

    @property
    def registry(self) -> BiomedicalToolRegistry:
        return get_tool_registry()

    # Query parsing lives in query_planner; these wrappers keep the historical entry points.
    @staticmethod
    def _extract_primary_target(query: str) -> Optional[str]:
        return query_planner.extract_primary_target(query)

    _NEGATIVE_SIMULATION_PATTERNS = query_planner.NEGATIVE_SIMULATION_PATTERNS

    @classmethod
    def _detect_simulation_suppression(cls, query: str) -> bool:
        """Return True when the user explicitly asked not to run simulations."""
        return query_planner.detect_simulation_suppression(query)

    @staticmethod
    def _extract_dataset_path(query: str) -> Optional[str]:
        """Extract a referenced single-cell dataset file (.h5ad / .zarr) from the query."""
        return query_planner.extract_dataset_path(query)

    @staticmethod
    def _has_single_cell_intent(query: str) -> bool:
        return query_planner.has_single_cell_intent(query)

    @staticmethod
    def _sanitize_agent_names(text: str) -> str:
        """Strip internal platform names and enforce official designations."""
        # Only internal agent identifiers: bare words such as "S2" (spike S2 subunit) or "SCP" are science.
        # ORC-005: organisation and product names (OpenAI, NVIDIA, LangGraph) are never rewritten: they occur in
        # the literature ("GPT-4 from OpenAI", "NVIDIA BioNeMo"), and rewriting them falsifies cited statements.
        # Provider non-disclosure is the system prompt's job, not a filter over scientific prose.
        replacements = [
            ("t2kg_agent", "Synapta"),
            ("t2kg", "Synapta"),
            ("t2b_agent", "Vivarix"),
            ("t2b", "Vivarix"),
            ("scp_agent", "Lumicell"),
            ("s2_agent", "Lithoscribe"),
            ("t2s_agent", "Lithoscribe"),
        ]
        out = text
        for old, new in replacements:
            out = re.sub(rf"\b{old}\b", new, out, flags=re.IGNORECASE)

        # Strip robotic compliance disclaimers
        out = re.sub(r"(?im)^\s*All agent references follow the official naming convention:.*$", "", out)
        out = re.sub(r"(?im)^\s*Note: All agent references adhere to.*$", "", out)
        out = _CITE_MARKER_RE.sub(_cite_marker, out)
        return out.strip()

    @classmethod
    def _sanitize_answer(cls, text: str) -> str:
        """Answer text: internal names stripped, copied evidence keys rewritten as plain prose and the evidence
        block's own markup (<agent_evidence>, <untrusted_data>, <tool_output>, ...) removed."""
        return clean_internal_keys(cls._sanitize_agent_names(strip_evidence_markup(normalize_typography(text))))

    # Progress labels for each tool step while it runs; the finished label comes from _describe_tool_result.
    _RUNNING_TITLES = {
        "synapta": "Looking up {target} in OpenTargets and KEGG",
        "lithoscribe": "Searching EuropePMC and PubMed",
        "websurfer": "Searching the web",
        "vivarix": "Simulating {model} in COPASI",
        "vivarix_steady_state": "Solving for the steady state of {model}",
        "vivarix_scan": "Scanning {scan_parameter} in {model}",
        "lumicell": "Analyzing {dataset}",
    }

    @staticmethod
    def _describe_tool_result(name: str, res: ToolResult, ctx: Dict[str, Any]) -> tuple[str, str]:
        """Summarize what a tool actually returned, as (title, detail) for the activity timeline."""
        if not res.success:
            err_type = getattr(res.error_type, "value", res.error_type) or "ERROR"
            labels = {
                "synapta": f"OpenTargets/KEGG lookup for {ctx.get('target')} failed",
                "lithoscribe": "Literature search failed",
                "websurfer": "Web search failed",
                "vivarix": f"Simulation of {ctx.get('model')} failed",
                "vivarix_steady_state": "Steady-state solve failed",
                "vivarix_scan": f"Parameter scan of {ctx.get('model')} failed",
                "lumicell": f"Analysis of {ctx.get('dataset')} failed",
            }
            return labels.get(name, f"{name} failed"), f"{err_type}: {(res.error or '')[:240]}"

        data: Dict[str, Any] = res.data if isinstance(res.data, dict) else {}
        parts: List[str] = []
        if name == "synapta":
            coverage = data.get("coverage") or {}
            status = coverage.get("status")
            title = (
                f"Looked up {ctx.get('target')} in OpenTargets and KEGG"
                + (" (partial)" if status == "partial" else " (nothing found)" if status == "success_empty" else "")
            )
            if res.canonical_ids.get("ensembl_id"):
                parts.append(res.canonical_ids["ensembl_id"])
            assocs = data.get("associations") or []
            parts.append(f"{len(assocs)} disease association{'s' if len(assocs) != 1 else ''}")
            parts.append(f"{len(data.get('pathways') or [])} KEGG pathways")
            if assocs and assocs[0].get("disease_name"):
                parts.append(f"top: {assocs[0]['disease_name']}")
            failed = [k for k, v in (coverage.get("sources") or {}).items() if isinstance(v, dict) and v.get("status") == "error"]
            if failed:
                parts.append(f"failed: {', '.join(failed)}")
        elif name == "lithoscribe":
            passages = data.get("ranked_passages") or []
            title = f"Found {len(passages)} relevant literature passage{'s' if len(passages) != 1 else ''}"
            labels = [
                str(p.get("citation") or p.get("citation_label") or (f"PMID {p['pmid']}" if p.get("pmid") else ""))
                for p in passages[:3]
                if isinstance(p, dict)
            ]
            labels = list(dict.fromkeys(label for label in labels if label))
            if labels:
                parts.append("; ".join(labels))
        elif name == "websurfer":
            results = data.get("results") or []
            title = f"Found {len(results)} web result{'s' if len(results) != 1 else ''}"
            titles = [str(r.get("title")) for r in results[:3] if isinstance(r, dict) and r.get("title")]
            if titles:
                parts.append("; ".join(titles))
        elif name == "vivarix":
            title = f"Simulated {data.get('model_id') or ctx.get('model')}"
            if data.get("model_name"):
                parts.append(str(data["model_name"]))
            parts.append(f"{len(data.get('species_list') or [])} species")
            units = data.get('units')
            time_unit = units.get('time', 's') if isinstance(units, dict) else 's'
            parts.append(f"t = {data.get('duration')}{time_unit}")
            diag = data.get("diagnostics")
            convergence = diag.get("convergence_status") if isinstance(diag, dict) else None
            if convergence:
                parts.append(f"endpoint drift: {convergence}")
        elif name == "vivarix_steady_state":
            title = f"Steady state: {data.get('steady_state_status', 'computed')}"
            if data.get("stability"):
                parts.append(f"stability: {data['stability']}")
        elif name == "vivarix_scan":
            n_ok = int(data.get("successful_points") or 0)
            n_all = int(data.get("total_points") or 0)
            title = f"Scanned {data.get('resolved_parameter') or data.get('parameter_name')} over {n_all} point{'s' if n_all != 1 else ''}"
            req = data.get("requested")
            req_d = req if isinstance(req, dict) else {}
            parts.append(f"{req_d.get('start')} → {req_d.get('end')} ({req_d.get('spacing', 'linear')})")
            parts.append(f"t = {data.get('duration_seconds')} s per point")
            parts.append(f"{n_ok} succeeded" + (f", {n_all - n_ok} failed" if n_all - n_ok else ""))
        elif name == "lumicell":
            title = f"Analyzed {data.get('dataset_name') or ctx.get('dataset')}"
            parts.append(f"{data.get('n_cells_post_qc')} cells after QC")
            parts.append(f"{data.get('n_clusters')} Leiden clusters")
        else:
            title = f"{name} finished"
        return title, " · ".join(p for p in parts if p)

    # -- simulation plan and task status ------------------------------------------------------------------
    @staticmethod
    def _simulation_plan(ctx: Any) -> Optional[SimulationPlan]:
        """The planner's SimulationPlan, or one derived from the legacy routing fields when it set none."""
        plan = getattr(ctx, "simulation_plan", None)
        if isinstance(plan, SimulationPlan):
            return plan
        if not getattr(ctx, "is_simulation_intent", False) and getattr(ctx, "model_attachment", None) is None:
            return None
        return SimulationPlan(
            action="steady_state" if getattr(ctx, "wants_steady_state", False) else "time_course",
            biomodel_id=getattr(ctx, "biomodel_id", None),
            duration_seconds=float(getattr(ctx, "sim_duration", 60.0) or 60.0),
            intervals=40,
        )

    @staticmethod
    def _plan_errors(plan: SimulationPlan) -> List[Dict[str, str]]:
        """The planner's validation errors plus the execution contract checked here, so nothing out of range runs."""
        errors = [dict(e) for e in plan.validation_errors if isinstance(e, dict)]
        codes = {e.get("code") for e in errors}

        def add(code: str, message: str) -> None:
            if code not in codes:
                errors.append({"code": code, "message": message})
                codes.add(code)

        d = plan.duration_seconds
        if not isinstance(d, (int, float)) or d != d or d <= 0:
            add("DURATION_INVALID", f"Duration must be a positive number of seconds (got {d!r}).")
        elif not SIM_DURATION_MIN_SECONDS <= d <= SIM_DURATION_MAX_SECONDS:
            add(
                "DURATION_OUT_OF_RANGE",
                f"Requested duration {d:g} s is outside the supported range "
                f"{SIM_DURATION_MIN_SECONDS:g}-{SIM_DURATION_MAX_SECONDS:g} s; it was not replaced with another duration.",
            )
        if plan.action == "parameter_scan":
            missing = [n for n, v in (("parameter", plan.scan_parameter), ("start value", plan.scan_start),
                                      ("end value", plan.scan_end), ("number of points", plan.scan_points)) if v is None]
            if missing:
                add("SCAN_INCOMPLETE", f"A parameter scan needs a {', '.join(missing)}.")
            elif not 2 <= (plan.scan_points or 0) <= SIM_SCAN_MAX_POINTS:
                add("SCAN_POINTS_INVALID", f"A scan takes 2-{SIM_SCAN_MAX_POINTS} points (got {plan.scan_points}).")
            if plan.parameter_overrides:
                add("UNSUPPORTED_COMBINATION", "Fixed parameter overrides cannot be combined with a parameter scan.")
        return errors

    @staticmethod
    def _task_outcome(
        ctx: Any,
        plan: Optional[SimulationPlan],
        tool_results: Dict[str, ToolResult],
        verification: Any,
        llm_ok: bool,
        core_phase_note: str = "",
    ) -> TaskOutcome:
        """Whether the requested operation ran as asked; kept separate from how well the answer is grounded.

        core_phase_note (agentic frameworks): why the framework's own phase (debate, supervisor, swarm,
        plan-and-execute) did not run as asked; a run that would otherwise be "completed" is then "partial"."""
        grounding = "not_checked"
        if verification is not None:
            claims = getattr(verification, "claims", None) or []
            statuses = {str(c.get("status") if isinstance(c, dict) else getattr(c, "status", "")) for c in claims}
            if "contradicted" in statuses:
                grounding = "contradicted"
            elif getattr(verification, "passed", False):
                grounding = "partial" if "unsupported" in statuses else "supported"
            else:
                grounding = "unsupported" if float(getattr(verification, "verification_score", 0.0) or 0.0) < 0.5 else "partial"

        def out(status: TaskStatus, requested: Optional[str], executed: Optional[str], reason: str, match: bool = True) -> TaskOutcome:
            if core_phase_note and status in ("completed", "partial"):
                status = "partial"
                reason = core_phase_note if reason in ("", "Evidence gathered and synthesized.") else f"{core_phase_note} {reason}"
            return TaskOutcome(task_status=status, requested_operation=requested, executed_operation=executed,
                               request_match=match, reason=reason, grounding_status=grounding)

        def _simulation_outcome(plan: SimulationPlan) -> TaskOutcome:
            """The simulation part of the request: the requested action against what ran."""
            requested = plan.action
            viv, ss, scan = tool_results.get("vivarix"), tool_results.get("vivarix_steady_state"), tool_results.get("vivarix_scan")
            first_fail = next((r for r in (scan, ss, viv) if r is not None and not r.success), None)
            meta = (first_fail.metadata or {}) if first_fail is not None else {}
            if first_fail is not None and meta.get("reason_code") == "MODEL_REQUIRED":
                return out("needs_input", requested, None, first_fail.error or "No suitable model was found.", match=False)
            if requested == "parameter_scan":
                if scan is not None and scan.success:
                    data = scan.data if isinstance(scan.data, dict) else {}
                    if data.get("partial_failure"):
                        n_ok, n_all = data.get("successful_points", 0), data.get("total_points", 0)
                        return out("partial", requested, "parameter_scan", f"{n_ok} of {n_all} scan points succeeded.")
                    return out("completed", requested, "parameter_scan", "All scan points ran.")
                executed = "time_course" if viv is not None and viv.success else None
                reason = (first_fail.error if first_fail is not None else "The parameter scan did not run.") or ""
                return out("failed", requested, executed, reason[:300], match=False)
            if requested == "steady_state":
                if ss is not None and ss.success:
                    return out("completed", requested, "steady_state", "Steady state computed.")
                return out("failed", requested, "time_course" if viv is not None and viv.success else None,
                           ((first_fail.error if first_fail else "") or "The steady state was not computed.")[:300], match=False)
            if viv is not None and viv.success:
                return out("completed", requested, "time_course", "Time course computed.")
            return out("failed", requested, None, ((first_fail.error if first_fail else "") or "The simulation did not run.")[:300], match=False)

        _sim_keys = ("vivarix", "vivarix_steady_state", "vivarix_scan")
        others = {k: r for k, r in tool_results.items() if r is not None and k not in _sim_keys and k != "lumicell"}

        def combined(primary: TaskOutcome) -> TaskOutcome:
            """A computational request is one part of the question: failures of the other routed sources (literature,
            targets, trials) and an unavailable model also decide the status (ORC-009)."""
            if primary.task_status != "completed":
                return primary
            failed = sorted(k for k, r in others.items() if not r.success)
            notes: List[str] = []
            if failed:
                notes.append(f"{len(failed)} of {len(others)} other evidence sources failed ({', '.join(failed[:5])}).")
            if not llm_ok:
                notes.append("The model was unavailable; the raw evidence is shown instead.")
            if not notes:
                return primary
            return out("partial", primary.requested_operation, primary.executed_operation,
                       f"{primary.reason} {' '.join(notes)}".strip(), match=primary.request_match)

        if plan is not None and getattr(ctx, "should_run", lambda _n: False)("vivarix"):
            return combined(_simulation_outcome(plan))

        lum = tool_results.get("lumicell")
        if lum is not None and getattr(ctx, "has_single_cell_intent", False) or (lum is not None and lum.error_type == ErrorType.DATASET_REQUIRED):
            if lum.success:
                return combined(out("completed", "single_cell_analysis", "single_cell_analysis", "Dataset analyzed."))
            if lum.error_type == ErrorType.DATASET_REQUIRED:
                return out("needs_input", "single_cell_analysis", None, lum.error or "A dataset is required.", match=False)
            return out("failed", "single_cell_analysis", None, (lum.error or "Analysis failed.")[:300], match=False)

        ran = [r for r in tool_results.values() if r is not None]
        n_ok = sum(1 for r in ran if r.success)
        # ORC-007: nothing retrieved is "failed" whether or not the model was available (no raw evidence to show).
        if ran and n_ok == 0:
            reason = "Every evidence source failed." + ("" if llm_ok else " The model was also unavailable.")
            return out("failed", "evidence_synthesis", None, reason, match=False)
        if not llm_ok:
            return out("partial", "evidence_synthesis", "evidence_listing", "The model was unavailable; the raw evidence is shown instead.")
        if n_ok < len(ran):
            return out("partial", "evidence_synthesis", "evidence_synthesis", f"{len(ran) - n_ok} of {len(ran)} sources failed.")
        return out("completed", "evidence_synthesis", "evidence_synthesis", "Evidence gathered and synthesized.")

    async def execute_inquiry(
        self,
        query: str,
        thread_id: str,
        model_name: str = DEFAULT_ORCHESTRATOR_MODEL,
        enable_web_search: bool = True,
        api_key: Optional[str] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]] = None,
        user_id: str = "default_user",
        dataset_ids: Optional[List[str]] = None,
        persist_memory: bool = True,
        recall_memory: bool = True,
        framework: Optional[str] = None,
        project_id: Optional[str] = None,
        agent_options: Optional[Dict[str, Any]] = None,
        run_control: Optional[RunControl] = None,
        interactive: bool = False,
    ) -> AuthoritativeOrchestrationResult:
        """
        Execute full LLMCompiler dependency-aware workflow:
        Stage 1 (Parallel Evidence): Synapta, Lithoscribe, WebSurfer, plus registered EvidenceSources
        Stage 2 (Computational Systems): Vivarix, Lumicell
        Stage 3 (Hypothesis Prioritization): BioDiscoveryAgent
        Stage 4 (Synthesis & Grounding): Model invocation with streaming
        Stage 5 (Reflexion Verification): Deterministic defect audit & bounded correction
        Stage 6 (Persistent Memory Consolidation)

        event_callback receives typed events as they happen:
        - {"type": "thought", "data": step}: an activity step was started or updated (upsert by step["id"])
        - {"type": "reasoning_delta", "delta": str}: the model's own reasoning tokens, only with PHRONESIS_SHOW_REASONING=1
          (raw reasoning is not checked by the verifier and can restate prompt rules, so it is off by default)
        - {"type": "content_delta", "delta": str}: answer tokens
        - {"type": "content_reset", "reason": str}: discard streamed answer text (a revision follows)

        dataset_ids attaches the caller's uploaded datasets; persist_memory/recall_memory=False run the turn
        without writing to or reading from session memory (used by re-runs).

        framework selects the orchestration architecture (phronesis.agentic.config). The default classic pipeline
        runs no agentic phase. Agentic frameworks add phases around the pipeline stages (decomposition, planned
        tool DAG, supervisor/swarm specialists, gap re-retrieval, debate, NLI claim checks, confidence) and emit
        {"type": "run_control", "run_id": ...} first, so the client can approve plans, steer or cancel
        (interactive=True makes approval requests wait for the user). project_id scopes project memory;
        agent_options carries per-request settings (budget limits, require_plan_approval, ...).
        """
        start_time = time.perf_counter()
        rec = _make_recorder(
            query=query,
            thread_id=thread_id,
            model_name=model_name,
            enable_web_search=enable_web_search,
            user_id=user_id,
            api_key_supplied=bool(api_key),
        )
        token = None
        if rec is not None:
            try:
                token = activate_recorder(rec)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Run recorder activation failed: %s", exc)
        pending: Dict[str, Any] = {}  # planner task / search-plan future to stop if the turn ends early
        resolved_framework = agent_config.resolve_framework(framework, agent_config.CLASSIC)
        if rec is not None:
            try:  # PRV-004: what a full re-run needs to reproduce this run's configuration
                rec.set_request_config(
                    requested_framework=framework, framework=resolved_framework, agent_options=agent_options,
                    project_id=project_id, interactive=interactive, recall_memory=recall_memory,
                    persist_memory=persist_memory, dataset_ids=list(dataset_ids or []),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Run recorder request config failed: %s", exc)
        control: Optional[RunControl] = None
        if agent_config.is_agentic(resolved_framework):
            control = run_control or create_control(user_id, interactive)
        scope_cm: Any = contextlib.nullcontext()
        if control is not None and dataset_ids:
            try:
                from ..agentic.sandbox import bound_scope

                scope_cm = bound_scope(user_id, list(dataset_ids))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Sandbox scope unavailable: %s", exc)
        try:
            with scope_cm:
                return await self._execute_inquiry(
                    rec,
                    start_time,
                pending=pending,
                query=query,
                thread_id=thread_id,
                model_name=model_name,
                enable_web_search=enable_web_search,
                api_key=api_key,
                event_callback=event_callback,
                user_id=user_id,
                dataset_ids=dataset_ids,
                persist_memory=persist_memory,
                recall_memory=recall_memory,
                framework=resolved_framework,
                project_id=project_id,
                agent_options=dict(agent_options or {}),
                control=control,
            )
        except RunCancelled as exc:
            duration = round(time.perf_counter() - start_time, 2)
            resp = f"The run was cancelled ({exc}). Nothing further was searched or written."
            return AuthoritativeOrchestrationResult(
                thread_id=thread_id,
                response=resp,
                thoughts=list(pending.get("thoughts") or []),
                steps=[],
                selected_agents=[],
                duration_seconds=duration,
                model_used=model_name,
                framework_used=resolved_framework,
                verification_report={"passed": False, "verification_score": 0.0, "defects": []},
                execution_artifacts={"agent_run": control.to_dict()} if control is not None else {},
                run_manifest=None,
                task_status=TaskOutcome(task_status="cancelled", reason=str(exc) or "Cancelled by the user.").model_dump(),
            )
        finally:
            if control is not None and run_control is None:
                release_control(control.run_id)
            # An exception or cancellation must not leave the planner model call or search-plan task running.
            ctx_ = pending.get("ctx")
            if ctx_ is not None:
                try:
                    query_planner.cancel_planner(ctx_)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Planner cancellation failed: %s", exc)
            fut = pending.get("search_plan_task")
            if fut is not None and not fut.done():
                fut.cancel()
            if token is not None:
                try:
                    reset_recorder(token)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Run recorder reset failed: %s", exc)

    async def _execute_inquiry(
        self,
        rec: Any,
        start_time: float,
        *,
        pending: Optional[Dict[str, Any]] = None,
        query: str,
        thread_id: str,
        model_name: str,
        enable_web_search: bool,
        api_key: Optional[str],
        event_callback: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]],
        user_id: str,
        dataset_ids: Optional[List[str]],
        persist_memory: bool,
        recall_memory: bool,
        framework: str = agent_config.CLASSIC,
        project_id: Optional[str] = None,
        agent_options: Optional[Dict[str, Any]] = None,
        control: Optional[RunControl] = None,
    ) -> AuthoritativeOrchestrationResult:
        run = RunContext(
            orch=self,
            rec=rec,
            start_time=start_time,
            pending=pending if pending is not None else {},
            query=query,
            thread_id=thread_id,
            model_name=model_name,
            enable_web_search=enable_web_search,
            api_key=api_key,
            event_callback=event_callback,
            user_id=user_id,
            dataset_ids=dataset_ids,
            persist_memory=persist_memory,
            recall_memory=recall_memory,
            framework=framework,
            project_id=project_id,
            agent_options=agent_options,
            control=control,
        )
        run.pending["thoughts"] = run.thoughts
        run.show_reasoning = _show_reasoning()

        # Stage 0: greeting, query context and memory, agentic pre-evidence phases, source routing.
        result = await plan_stage.start(run)
        if result is None:
            result = await plan_stage.read_question(run)
        if result is not None:
            return result
        # Each pipeline stage's wall time is recorded (run.pipeline_phases -> agent_run.phases, stage "pipeline"), so
        # the run's phase chart accounts for drafting, verification and memory as well as the agent phases.
        await run.timed("route", plan_stage.route_sources(run))

        # Stages 1-3: evidence and computational tools, agentic follow-up work, artifacts, prioritization.
        await run.timed("evidence", gather_stage.run_evidence(run))
        await run.timed("computations", gather_stage.run_computations(run))
        await agent_rounds.augment(run)
        gather_stage.collect_artifacts(run)
        await gather_stage.prioritize(run)

        # Stage 4: synthesis prompt and the streamed first draft, then the agentic draft loop.
        draft_stage.prepare_context(run)
        await run.timed("first_draft", draft_stage.write_first_draft(run))
        await agent_rounds.draft_rounds(run)  # records rewrite_1, rewrite_2, ... itself

        # Stage 5: verification and bounded correction, citations, agentic finalize phases, reported result.
        await run.timed("verification", verify_stage.check_and_correct(run))
        run.timed_sync("citations", lambda: verify_stage.build_citations(run))
        await agent_rounds.finalize_phases(run)
        await verify_stage.report_result(run)

        # Stage 6: memory consolidation and the result.
        await run.timed("memory", finalize_stage.consolidate_memory(run))
        return await finalize_stage.assemble_result(run)


_GLOBAL_ORCHESTRATOR: Optional[AuthoritativeOrchestrator] = None


def get_authoritative_orchestrator() -> AuthoritativeOrchestrator:
    global _GLOBAL_ORCHESTRATOR
    if _GLOBAL_ORCHESTRATOR is None:
        _GLOBAL_ORCHESTRATOR = AuthoritativeOrchestrator()
    return _GLOBAL_ORCHESTRATOR
