"""
Specialist agents for the multi-agent topologies (feature 4), plus the shared evidence helpers the loop phases
(gaps, counter-evidence, topologies) use.

A specialist is a named group of registry tools with a standard operating procedure written into its role
prompt (MetaGPT SOP-style roles, Hong et al., ICLR 2024, arXiv:2308.00352). Groups are built from the live
registry at run time, from each tool's domain, name prefix and tags, so tools that other packages register
(trials, drugs, structure, omics sources, ...) join the right specialist without a code change here:

- literature: lithoscribe, clinical trials, web search, preprints
- target: synapta / OpenTargets, disease targets, PrimeKG, variants, pathways
- simulation: vivarix (absent when the turn forbids simulation)
- omics: lumicell and omics sources (GWAS, GTEx, STRING, Reactome, DepMap)
- chemistry: drugs (ChEMBL, openFDA, PubChem) and structure (AlphaFold, PDB, docking)

Each tool also passes the turn's hard policy: the source allowlist ("papers only"), web search on/off, simulation
and single-cell bans, and sources the researcher switched off at plan approval. Conversable specialists that call
tools or hand off follow AutoGen (Wu et al., COLM 2024, arXiv:2308.08155).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional, Set, Tuple, Type

from pydantic import BaseModel

from .budget import BudgetExhausted
from .guardrails import spotlight
from .llm import AgentLLMUnavailable
from .plan_graph import excluded_by_question, single_cell_dataset_path
from .tool_retrieval import tokenize, tool_card

logger = logging.getLogger(__name__)

LITERATURE_TOOL = "lithoscribe_literature_synthesis"
WEB_TOOL = "websurfer_search"


@dataclass(frozen=True)
class SpecialistSpec:
    name: str
    label: str
    role: str  # SOP: what the specialist is for and how it works
    domains: FrozenSet[str]
    prefixes: Tuple[str, ...]
    tags: FrozenSet[str]


SPECIALIST_SPECS: Tuple[SpecialistSpec, ...] = (
    SpecialistSpec(
        name="literature",
        label="Literature specialist",
        role=(
            "Find peer-reviewed papers, clinical trials and (when allowed) web sources that answer the instruction. "
            "Procedure: 1) turn the instruction into a focused search query with the key entities; 2) call one "
            "search tool; 3) read the passages; 4) search again only for what is still missing; 5) finish with a "
            "summary that names the PMIDs/NCT ids found and what they show."
        ),
        domains=frozenset({"lithoscribe", "websurfer", "literature", "trials"}),
        prefixes=("lithoscribe", "websurfer", "europepmc", "pubmed", "biorxiv", "medrxiv", "preprint", "trials"),
        tags=frozenset({"literature", "europepmc", "citations", "paperqa2", "clinical_trials", "nct", "web",
                        "preprint", "preprints", "biorxiv", "medrxiv", "pubmed"}),
    ),
    SpecialistSpec(
        name="target",
        label="Target specialist",
        role=(
            "Characterise genes and targets: target-disease associations, pathways, knowledge-graph relations and "
            "variants. Procedure: 1) identify the gene/disease identifiers needed; 2) call the lookup that answers "
            "the instruction; 3) report scores and identifiers exactly as returned, never estimated."
        ),
        domains=frozenset({"synapta", "targets", "primekg", "genetics"}),
        prefixes=("synapta", "opentargets", "primekg", "kegg", "variant", "clinvar"),
        tags=frozenset({"genetics", "targets", "associations", "pathways", "kegg", "primekg", "knowledge_graph",
                        "variant", "clinvar", "civic", "gnomad", "disease", "normalization", "hgnc"}),
    ),
    SpecialistSpec(
        name="simulation",
        label="Simulation specialist",
        role=(
            "Run kinetic models (BioModels / SBML): time courses, steady states and parameter scans. Procedure: "
            "1) use only a model id given in the context; 2) run the one computation the instruction asks for; "
            "3) report the numbers with their units; never invent a model or a parameter."
        ),
        domains=frozenset({"vivarix", "simulation"}),
        prefixes=("vivarix",),
        tags=frozenset({"simulation", "ode", "copasi", "kinetics", "steady_state", "parameter_scan", "sbml",
                        "calibration", "fluxes"}),
    ),
    SpecialistSpec(
        name="omics",
        label="Omics specialist",
        role=(
            "Answer expression, single-cell, genetic-association and interaction questions from omics sources. "
            "Procedure: 1) pick the source that measures what the instruction asks; 2) call it with the gene or "
            "trait; 3) report tissues, effect sizes and p-values as returned."
        ),
        domains=frozenset({"lumicell", "omics"}),
        prefixes=("lumicell", "omics", "gwas", "gtex", "string", "reactome", "depmap"),
        tags=frozenset({"single_cell", "scanpy", "anndata", "clustering", "gwas", "gtex", "expression", "string",
                        "reactome", "interaction", "depmap", "omics", "eqtl"}),
    ),
    SpecialistSpec(
        name="chemistry",
        label="Chemistry specialist",
        role=(
            "Characterise drugs and structures: mechanism, potency, safety signals, compound properties and "
            "protein structures. Procedure: 1) resolve the drug or protein; 2) call the profile, safety or "
            "structure lookup the instruction needs; 3) report identifiers (ChEMBL, PubChem, PDB, AlphaFold) as returned."
        ),
        domains=frozenset({"structure", "chemistry", "drugs"}),
        prefixes=("chembl", "openfda", "pubchem", "structure", "alphafold", "pdb", "diffdock", "molmim", "drug"),
        tags=frozenset({"drugs", "chembl", "mechanism", "bioactivity", "potency", "safety", "faers", "openfda",
                        "pubchem", "structure", "alphafold", "pdb", "docking", "chemistry", "compound"}),
    ),
)

SPECS_BY_NAME: Dict[str, SpecialistSpec] = {s.name: s for s in SPECIALIST_SPECS}
_EXCLUDED_DOMAINS = frozenset({"analysis"})  # the sandbox runs only through the analyst phase (opt-in)


def classify_tool(entry: Any) -> Optional[str]:
    """The specialist a registry tool belongs to (domain 3 points, name prefix 2, each tag 1); None if no signal."""
    domain = (getattr(entry, "domain", "") or "").lower()
    if domain in _EXCLUDED_DOMAINS:
        return None
    name = (getattr(entry, "name", "") or "").lower()
    tags = {str(t).lower() for t in (getattr(entry, "tags", None) or [])}
    best, best_score = None, 0
    for spec in SPECIALIST_SPECS:
        score = 3 * (domain in spec.domains)
        score += 2 * any(name.startswith(p) for p in spec.prefixes)
        score += len(tags & spec.tags)
        if score > best_score:
            best, best_score = spec.name, score
    return best


def tool_source_key(entry: Any) -> str:
    """The source key a tool is gated by in ctx.source_allowlist / ctx.decisions."""
    name = (getattr(entry, "name", "") or "").lower()
    tags = {str(t).lower() for t in (getattr(entry, "tags", None) or [])}
    if name == LITERATURE_TOOL:
        return "lithoscribe"
    if "clinical_trials" in tags or "trials" in name:
        return "clinical_trials"
    if name.startswith("websurfer"):
        return "websurfer"
    return name.split("_", 1)[0] or (getattr(entry, "domain", "") or "").lower()


def _switched_off(ctx: Any, key: str) -> bool:
    decision = (getattr(ctx, "decisions", None) or {}).get(key)
    reason = str(getattr(decision, "reason", "") or "").lower()
    return decision is not None and not getattr(decision, "run", True) and reason.startswith("switched off")


def simulation_forbidden(ctx: Any) -> bool:
    constraints = getattr(ctx, "simulation_constraints", None)
    return bool(getattr(ctx, "suppress_simulation", False) or getattr(constraints, "simulation_forbidden", False))


def tool_allowed(ctx: Any, entry: Any) -> Tuple[bool, str]:
    """(allowed, reason) for one tool under the turn's hard policy."""
    if entry is None:
        return False, "tool is not registered"
    domain = (getattr(entry, "domain", "") or "").lower()
    name = (getattr(entry, "name", "") or "").lower()
    if domain in _EXCLUDED_DOMAINS:
        return False, "sandboxed analysis runs only through the analyst phase"
    if (domain == "vivarix" or name.startswith("vivarix") or classify_tool(entry) == "simulation") and simulation_forbidden(ctx):
        return False, "the researcher asked for no simulation"
    if (domain == "lumicell" or name.startswith("lumicell")) and (getattr(ctx, "extras", None) or {}).get("single_cell_forbidden"):
        return False, "the researcher asked for no single-cell analysis"
    if (domain == "lumicell" or name.startswith("lumicell")) and not single_cell_dataset_path(ctx):
        return False, "no single-cell dataset (.h5ad) is attached"
    excluded = excluded_by_question(entry, ctx)
    if excluded:
        return False, excluded
    key = tool_source_key(entry)
    if key == "websurfer" and not getattr(ctx, "enable_web_search", True):
        return False, "web search is switched off"
    allowlist = getattr(ctx, "source_allowlist", None)
    if allowlist is not None and key not in allowlist:
        return False, getattr(ctx, "source_restriction_reason", "") or "excluded by the source restriction"
    if _switched_off(ctx, key):
        return False, "switched off by the researcher"
    return True, ""


def simulation_requested(ctx: Any, tool_results: Optional[Dict[str, Any]] = None) -> bool:
    """Whether this turn asked for (or already ran) a kinetic simulation; never when simulation is forbidden."""
    if simulation_forbidden(ctx):
        return False
    if getattr(ctx, "is_simulation_intent", False) or getattr(ctx, "simulation_plan", None) is not None:
        return True
    if getattr(ctx, "biomodel_id", None) or getattr(ctx, "model_attachment", None) is not None:
        return True
    return any(str(k).startswith("vivarix") for k in (tool_results or {}))


def single_cell_requested(ctx: Any, tool_results: Optional[Dict[str, Any]] = None) -> bool:
    """Whether this turn asked for (or already ran) a single-cell analysis."""
    if (getattr(ctx, "extras", None) or {}).get("single_cell_forbidden"):
        return False
    return bool(getattr(ctx, "has_single_cell_intent", False)) or any(str(k).startswith("lumicell") for k in (tool_results or {}))


def not_requested_modules(ctx: Any, tool_results: Optional[Dict[str, Any]] = None) -> List[str]:
    """Analyses the researcher did not ask for and that did not run: never a gap, never a limitation."""
    out: List[str] = []
    if not simulation_requested(ctx, tool_results):
        out.append("kinetic simulation")
    if not single_cell_requested(ctx, tool_results):
        out.append("single-cell analysis")
    return out


def named_tool_allowed(state: Any, tool_name: str) -> bool:
    entry = state.registry.get_entry(tool_name) if hasattr(state.registry, "get_entry") else None
    return tool_allowed(state.ctx, entry)[0]


@dataclass
class Specialist:
    spec: SpecialistSpec
    tools: List[Any]

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def label(self) -> str:
        return self.spec.label

    def tool_names(self) -> List[str]:
        return [e.name for e in self.tools]

    def get(self, tool_name: str) -> Optional[Any]:
        return next((e for e in self.tools if e.name == tool_name), None)

    def cards(self) -> List[Dict[str, Any]]:
        return [tool_card(e) for e in self.tools]


def build_specialists(state: Any) -> Dict[str, Specialist]:
    """Specialists with at least one allowed tool, in SPECIALIST_SPECS order (built from the live registry)."""
    groups: Dict[str, List[Any]] = {s.name: [] for s in SPECIALIST_SPECS}
    for entry in state.registry.list_tools():
        group = classify_tool(entry)
        if group is None or not tool_allowed(state.ctx, entry)[0]:
            continue
        groups[group].append(entry)
    if simulation_forbidden(state.ctx):
        groups["simulation"] = []
    return {name: Specialist(SPECS_BY_NAME[name], tools) for name, tools in groups.items() if tools}


def team_specialists(state: Any) -> Dict[str, Specialist]:
    """The specialists a supervisor or swarm works with: the Simulation specialist joins only when the question
    asks for a simulation (or a model is in play), so it is never dispatched for a clinical or literature question."""
    team = build_specialists(state)
    if "simulation" in team and not simulation_requested(state.ctx, getattr(state, "tool_results", None)):
        team.pop("simulation")
    return team


# Model access --------------------------------------------------------------------------------------------------
def _note_ask(state: Any, phase: str, kind: str) -> None:
    bag = getattr(state, "bag", None)
    if isinstance(bag, dict):
        bag.setdefault("_ask_outcomes", {})[phase] = kind


def last_ask_outcome(state: Any, phase: str) -> str:
    """How the last ask_json call of a phase ended: ok | unavailable | llm_failed | budget | unusable | ""."""
    bag = getattr(state, "bag", None)
    return str(((bag or {}).get("_ask_outcomes") or {}).get(phase, "")) if isinstance(bag, dict) else ""


def model_failing(state: Any, phase: str) -> bool:
    """True when the last model call of the phase failed outright (no reply at all), so further calls would most
    likely fail the same way and only burn the time budget."""
    return last_ask_outcome(state, phase) in ("unavailable", "llm_failed", "budget")


async def ask_json(state: Any, system: str, user: str, *, phase: str, schema: Optional[Type[BaseModel]] = None) -> Any:
    """A parsed model reply, or None when the model is unavailable, the budget is used up or the reply is not JSON.

    The outcome is noted per phase (last_ask_outcome) so callers can tell a failed call from an unusable reply."""
    if not state.llm.available:
        _note_ask(state, phase, "unavailable")
        return None
    try:
        reply = await state.llm.json(system, user, schema=schema, phase=phase)
    except AgentLLMUnavailable as exc:
        logger.info("Agent model call for %s failed: %s", phase, exc)
        _note_ask(state, phase, "llm_failed")
        return None
    except BudgetExhausted as exc:
        logger.info("Agent model call for %s stopped: %s", phase, exc)
        _note_ask(state, phase, "budget")
        return None
    except ValueError as exc:
        logger.info("Agent model call for %s gave no usable reply: %s", phase, exc)
        _note_ask(state, phase, "unusable")
        return None
    _note_ask(state, phase, "ok")
    return reply


# Evidence helpers ----------------------------------------------------------------------------------------------
def iter_passages(tool_results: Dict[str, Any]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """(result key, passage) for every passage of every successful literature-shaped result."""
    for key, res in list(tool_results.items()):
        data = getattr(res, "data", None)
        if not getattr(res, "success", False) or not isinstance(data, dict):
            continue
        for p in data.get("ranked_passages") or []:
            if isinstance(p, dict):
                yield key, p


def passage_source_key(p: Dict[str, Any], fallback: str = "") -> str:
    """The verifier's source key for a passage: PMID:<n>, DOI:<doi> or the result key."""
    if p.get("pmid"):
        return f"PMID:{p['pmid']}"
    if p.get("doi"):
        return f"DOI:{str(p['doi']).lower()}"
    return fallback


def passage_identity(p: Dict[str, Any]) -> str:
    text = re.sub(r"\s+", " ", str(p.get("passage_text") or "")).strip().lower()
    digest = hashlib.sha1(text[:400].encode("utf-8")).hexdigest()[:16]
    ident = f"pmid:{p['pmid']}" if p.get("pmid") else (f"doi:{str(p['doi']).lower()}" if p.get("doi") else "")
    return f"{ident}|{digest}"


def seen_passages(tool_results: Dict[str, Any]) -> Set[str]:
    return {passage_identity(p) for _k, p in iter_passages(tool_results)}


def seen_urls(tool_results: Dict[str, Any]) -> Set[str]:
    urls: Set[str] = set()
    for res in list(tool_results.values()):
        data = getattr(res, "data", None)
        if getattr(res, "success", False) and isinstance(data, dict) and str(getattr(res, "tool_name", "")).startswith("websurfer"):
            urls.update(str(r.get("url")) for r in data.get("results") or [] if isinstance(r, dict) and r.get("url"))
    return urls


def compact(data: Any, limit: int = 600) -> str:
    try:
        text = json.dumps(data, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(data)
    return text if len(text) <= limit else text[:limit] + " ...(truncated)"


def evidence_text(tool_results: Dict[str, Any], limit_per_result: int = 4000) -> str:
    """Lower-cased text of every successful result (passages, web snippets, compact JSON) for coverage checks."""
    parts: List[str] = []
    for res in list(tool_results.values()):
        if not getattr(res, "success", False):
            continue
        data = getattr(res, "data", None)
        if isinstance(data, dict) and isinstance(data.get("ranked_passages"), list):
            parts.extend(str(p.get("passage_text") or "") for p in data["ranked_passages"] if isinstance(p, dict))
        else:
            parts.append(compact(data, limit_per_result))
    return " ".join(parts).lower()


def stems(text: str) -> Set[str]:
    """Crude prefix stems (first 6 characters) of the content words, so 'diseases'/'disease' and
    'associated'/'association' match."""
    return {t[:6] for t in tokenize(text) if len(t) > 2}


def evidence_digest(state: Any, max_items: int = 14, chars: int = 280) -> Tuple[str, List[str]]:
    """A spotlighted digest of the gathered evidence for a critic or supervisor, and the evidence keys it names."""
    lines: List[str] = []
    keys: List[str] = []
    seen: Set[str] = set()
    for rkey, p in iter_passages(state.tool_results):
        skey = passage_source_key(p, rkey)
        if skey in seen:
            continue
        seen.add(skey)
        text = re.sub(r"\s+", " ", str(p.get("passage_text") or ""))[:chars]
        setting = p.get("grade_label") or p.get("experimental_setting") or ""
        lines.append(f"[{skey}] ({setting}) {text}" if setting else f"[{skey}] {text}")
        keys.append(skey)
        if len(lines) >= max_items:
            break
    for rkey, res in list(state.tool_results.items()):
        if len(lines) >= max_items + 6:
            break
        data = getattr(res, "data", None)
        if not getattr(res, "success", False) or (isinstance(data, dict) and "ranked_passages" in data):
            continue
        lines.append(f"[{rkey}] {compact(data, chars)}")
        keys.append(rkey)
    return spotlight("\n".join(lines) or "(no evidence gathered)", "gathered evidence", 9000), keys


def _filtered_result(res: Any, data: Dict[str, Any]) -> Any:
    try:
        return res.model_copy(update={"data": data})
    except AttributeError:  # not a pydantic ToolResult
        res.data = data
        return res


async def fetch_new_literature(
    state: Any,
    *,
    phase: str,
    query: str,
    purpose: str,
    seen: Set[str],
    keywords: Optional[List[str]] = None,
    max_articles: int = 5,
    render: bool = True,
    keep: Optional[Callable[[Dict[str, Any]], bool]] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Run the literature tool and keep only passages not already in the evidence (and, with keep, only those it
    accepts: off-topic passages are never stored).

    Returns (stored result key or None, kept passages). `seen` is updated with the new passages' identities. stats,
    when given, receives the search outcome: status ok | empty | error, returned, new, kept, and incomplete when the
    engine skipped or cut searches for time.
    """
    kwargs: Dict[str, Any] = {"query": query[:300], "max_articles": max_articles}
    if keywords:
        kwargs["keywords"] = [k for k in keywords if k][:8]
    res = await state.call_tool(LITERATURE_TOOL, phase=phase, purpose=purpose, store=False, **kwargs)
    data = getattr(res, "data", None)
    st = stats if stats is not None else {}
    if not getattr(res, "success", False) or not isinstance(data, dict):
        error_type = str(getattr(getattr(res, "error_type", None), "value", getattr(res, "error_type", "")) or "")
        st["status"] = "empty" if error_type == "NOT_FOUND" else "error"
        return None, []
    returned = [p for p in data.get("ranked_passages") or [] if isinstance(p, dict) and str(p.get("passage_text") or "").strip()]
    new: List[Dict[str, Any]] = []
    for p in returned:
        ident = passage_identity(p)
        if ident in seen:
            continue
        seen.add(ident)
        new.append(p)
    kept = [p for p in new if keep is None or keep(p)]
    st.update({"status": "ok", "returned": len(returned), "new": len(new), "kept": len(kept)})
    queries = (data.get("retrieval") or {}).get("queries") or []
    if any("time budget" in str(q.get("error") or "") or "deadline" in str(q.get("error") or "") for q in queries if isinstance(q, dict)):
        st["incomplete"] = True
    new = kept
    if not new:
        return None, []
    key = state.add_result(
        state.next_key(phase, LITERATURE_TOOL),
        _filtered_result(res, {**data, "ranked_passages": new}),
        phase=phase,
        purpose=purpose,
        render=render,
    )
    return key, new


async def fetch_new_web(
    state: Any, *, phase: str, query: str, purpose: str, seen: Set[str], max_results: int = 3,
    keep: Optional[Callable[[Dict[str, Any]], bool]] = None, stats: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Run web search and keep only results whose URL is new (and, with keep, that it accepts); stats as in
    fetch_new_literature."""
    res = await state.call_tool(WEB_TOOL, phase=phase, purpose=purpose, store=False, query=query[:300], max_results=max_results)
    data = getattr(res, "data", None)
    st = stats if stats is not None else {}
    if not getattr(res, "success", False) or not isinstance(data, dict):
        st["status"] = "error"
        return None, []
    returned = [r for r in data.get("results") or [] if isinstance(r, dict) and r.get("url")]
    fresh = [r for r in returned if str(r["url"]) not in seen]
    seen.update(str(r["url"]) for r in fresh)
    new = [r for r in fresh if keep is None or keep(r)]
    st.update({"status": "ok" if returned else "empty", "returned": len(returned), "new": len(fresh), "kept": len(new)})
    if not new:
        return None, []
    key = state.add_result(
        state.next_key(phase, WEB_TOOL), _filtered_result(res, {**data, "results": new}), phase=phase, purpose=purpose
    )
    return key, new


def query_entities(ctx: Any) -> Dict[str, List[str]]:
    """Gene, drug and disease names of the question, as the user wrote them (primary gene first)."""
    genes: List[str] = []
    if getattr(ctx, "primary_symbol", None):
        genes.append(ctx.primary_symbol)
    for e in list(getattr(ctx, "genes", []) or []):
        genes.append(getattr(e, "label", "") or getattr(e, "mention", ""))
    drugs = [getattr(e, "mention", "") or getattr(e, "label", "") for e in list(getattr(ctx, "drugs", []) or [])]
    diseases = [getattr(e, "mention", "") or getattr(e, "label", "") for e in list(getattr(ctx, "diseases", []) or [])]
    clean = lambda xs: list(dict.fromkeys(x.strip() for x in xs if x and x.strip()))  # noqa: E731
    return {"genes": clean(genes), "drugs": clean(drugs), "diseases": clean(diseases)}


__all__ = [
    "LITERATURE_TOOL",
    "SPECIALIST_SPECS",
    "WEB_TOOL",
    "Specialist",
    "SpecialistSpec",
    "ask_json",
    "build_specialists",
    "classify_tool",
    "compact",
    "evidence_digest",
    "evidence_text",
    "fetch_new_literature",
    "last_ask_outcome",
    "model_failing",
    "not_requested_modules",
    "fetch_new_web",
    "iter_passages",
    "named_tool_allowed",
    "passage_identity",
    "passage_source_key",
    "query_entities",
    "seen_passages",
    "seen_urls",
    "simulation_forbidden",
    "simulation_requested",
    "single_cell_requested",
    "stems",
    "team_specialists",
    "tool_allowed",
    "tool_source_key",
]
