"""
Project memory phases (feature 16): recall before the evidence search, consolidation after verification.

A research project spans many threads. Its memory is a bi-temporal fact graph (phronesis/utils/memory/
project_store.py) built from verified tool results, following:
- Zep / Graphiti (arXiv:2501.13956): bi-temporal facts; contradictions invalidate, never delete.
- Mem0 (arXiv:2504.19413): extract, then ADD / UPDATE / INVALIDATE / NOOP (deterministic here).
- HippoRAG (NeurIPS 2024, arXiv:2405.14831): Personalized PageRank from the question's entities for recall.
- Generative Agents (UIST 2023, arXiv:2304.03442): recency x importance x relevance ranking.
- MemGPT (arXiv:2310.08560): the 12 recalled facts are the working memory paged into the prompt; the archive
  stays in the store and the projects API.
- A-MEM (NeurIPS 2025, arXiv:2502.12110): facts sharing an entity are linked notes in the graph.

Phases:
- `project_memory` (before_evidence, order 20): only when `state.project_id` is set and owned by the user.
  Writes `state.project_memory` (a text block the orchestrator appends to the memory summary) and the
  artifact `project_memory.recalled`.
- `project_memory_consolidate` (finalize, order 50): extracts facts deterministically from `state.tool_results`
  (gene-disease association scores, drug mechanisms and targets, trial to condition, BioModel to topic,
  variant classifications), the `hypotheses` artifact and the researcher's lasting instructions
  (`classify_instructions`, investigation or profile scope). Facts are `verified` only when the turn's
  verification passed; otherwise they are tagged `unverified`. Artifact `project_memory.written/invalidated`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..utils.memory.clinical_memory import classify_instructions, turn_fully_verified
from ..utils.memory.project_store import FactInput, ProjectStore, get_project_store
from .guardrails import neutralise
from .runtime import AgentPhase, PhaseOutcome, register_phase
from .state import AgentState

logger = logging.getLogger(__name__)

RECALL_K = 12
MAX_FACTS_PER_TURN = 60


def _store() -> ProjectStore:
    return get_project_store()


def query_entities(state: AgentState) -> List[str]:
    """Entity strings for the PPR seeds: resolved genes, drugs, diseases, variants, the BioModel id."""
    ctx = state.ctx
    out: List[str] = []
    for e in list(getattr(ctx, "entities", None) or []):
        for value in (getattr(e, "label", None), getattr(e, "mention", None)):
            if value:
                out.append(str(value))
    for value in (getattr(ctx, "primary_symbol", None), getattr(ctx, "biomodel_id", None)):
        if value:
            out.append(str(value))
    return list(dict.fromkeys(out))


def _fmt_fact(f: Dict[str, Any]) -> str:
    rel = str(f["relation"]).replace("_", " ")
    value = f" ({f['value']})" if f.get("value") else ""
    flags = "verified" if f.get("verified") else "unverified"
    when = (f.get("recorded_at") or "")[:10]
    src = f.get("source") or {}
    origin = src.get("tool") or src.get("origin") or ""
    return neutralise(f"- {f['subject']} {rel} {f['object']}{value} [{flags}, {when}{', ' + str(origin) if origin else ''}]")


def format_memory_block(project: Dict[str, Any], facts: List[Dict[str, Any]]) -> str:
    if not facts:
        return ""
    name = neutralise(str(project.get("name", "")))[:120]
    lines = [
        f'<project_memory project="{name}">',
        "Facts recorded in earlier turns of this research project (background only: cite the current evidence, "
        "and say so when it disagrees with a recorded fact). Each line says whether its turn was verified; an "
        "unverified fact is a lead to check, never evidence:",
        *(_fmt_fact(f) for f in facts),
        "</project_memory>",
    ]
    return "\n".join(lines)


def _public(f: Dict[str, Any]) -> Dict[str, Any]:
    return {k: f.get(k) for k in ("id", "subject", "relation", "object", "value", "valid_from", "source", "confidence", "verified")}


# ---------------------------------------------------------------------------------------------------------
# Deterministic extraction
# ---------------------------------------------------------------------------------------------------------

def _data(result: Any) -> Tuple[bool, Any, str]:
    if result is None:
        return False, None, ""
    if isinstance(result, dict) and "success" not in result:
        return True, result, ""
    success = bool(getattr(result, "success", result.get("success") if isinstance(result, dict) else False))
    data = getattr(result, "data", result.get("data") if isinstance(result, dict) else None)
    tool = str(getattr(result, "tool_name", result.get("tool_name", "") if isinstance(result, dict) else "") or "")
    return success, data, tool


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iter_dicts(data: Any, key: str, depth: int = 0) -> Iterable[Dict[str, Any]]:
    """Dicts that carry `key`, found in data, its lists, or one level of nested dicts."""
    if depth > 2:
        return
    if isinstance(data, dict):
        if key in data:
            yield data
        for v in data.values():
            if isinstance(v, (list, dict)):
                yield from _iter_dicts(v, key, depth + 1)
    elif isinstance(data, list):
        for item in data[:50]:
            yield from _iter_dicts(item, key, depth + 1)


def _associations(subject: str, data: Dict[str, Any]) -> List[FactInput]:
    out: List[FactInput] = []
    for a in (data.get("associations") or [])[:10]:
        if not isinstance(a, dict):
            continue
        nested = a.get("disease") if isinstance(a.get("disease"), dict) else {}
        disease = a.get("disease_name") or nested.get("name")
        score = _num(a.get("overall_association_score", a.get("score")))
        if subject and disease:
            out.append(
                FactInput(
                    subject=subject,
                    relation="association_score_with",
                    object=str(disease),
                    value=f"{score:.3f}" if score is not None else None,
                    confidence=min(0.95, 0.5 + 0.5 * score) if score is not None else 0.6,
                )
            )
    return out


def _drugs(data: Dict[str, Any]) -> List[FactInput]:
    out: List[FactInput] = []
    for d in (data.get("drugs") or [])[:10]:
        if not isinstance(d, dict):
            continue
        name = d.get("pref_name") or d.get("chembl_id") or d.get("name")
        if not name:
            continue
        for m in (d.get("mechanisms") or [])[:5]:
            if not isinstance(m, dict):
                continue
            moa = m.get("mechanism_of_action")
            if moa:
                out.append(FactInput(subject=str(name), relation="mechanism", object=str(moa), confidence=0.85))
            target = m.get("target_pref_name") or m.get("target_chembl_id")
            if target:
                out.append(FactInput(subject=str(name), relation="targets", object=str(target), confidence=0.85))
    for d in (data.get("target_approved_drugs") or [])[:10]:
        if isinstance(d, dict) and (d.get("pref_name") or d.get("chembl_id")) and d.get("mechanism_of_action"):
            out.append(
                FactInput(subject=str(d.get("pref_name") or d.get("chembl_id")), relation="mechanism", object=str(d["mechanism_of_action"]), confidence=0.85)
            )
    return out


def _trials(data: Dict[str, Any]) -> List[FactInput]:
    out: List[FactInput] = []
    for t in (data.get("trials") or [])[:15]:
        if not isinstance(t, dict) or not t.get("nct_id"):
            continue
        for cond in (t.get("conditions") or [])[:3]:
            out.append(FactInput(subject=str(t["nct_id"]), relation="studies_condition", object=str(cond), confidence=0.9))
    return out


def _variants(data: Any) -> List[FactInput]:
    out: List[FactInput] = []
    for v in _iter_dicts(data, "clinvar"):
        gene = v.get("gene") or ""
        change = v.get("protein_change") or v.get("hgvs_c") or v.get("rsid") or ""
        subject = f"{gene} {change}".strip()
        for rec in (v.get("clinvar") or [])[:1]:
            if isinstance(rec, dict) and rec.get("germline_classification") and subject:
                out.append(FactInput(subject=subject, relation="classification", object=str(rec["germline_classification"]), confidence=0.85))
    return out


def _model(data: Dict[str, Any], topic: Optional[str]) -> List[FactInput]:
    model_id = data.get("model_id")
    obj = topic or data.get("model_name")
    if model_id and obj:
        return [FactInput(subject=str(model_id), relation="models_topic", object=str(obj), confidence=0.8)]
    return []


def _hypotheses(artifact: Any) -> List[FactInput]:
    if not isinstance(artifact, dict):
        return []
    subject = str(artifact.get("target") or artifact.get("model_id") or "").strip()
    out: List[FactInput] = []
    for h in (artifact.get("hypotheses") or [])[:5]:
        if not isinstance(h, dict) or not h.get("statement"):
            continue
        supports = (h.get("result") or {}).get("supports") if isinstance(h.get("result"), dict) else None
        relation = "hypothesis_supported" if supports is True else "hypothesis_not_supported" if supports is False else "hypothesis_untested"
        value = None
        lfc = _num((h.get("result") or {}).get("log2_fold_change")) if isinstance(h.get("result"), dict) else None
        if lfc is not None:
            value = f"log2FC {lfc:.2f}"
        out.append(
            FactInput(subject=subject or "hypothesis", relation=relation, object=str(h["statement"]), value=value, confidence=0.6, tags=["hypothesis"])
        )
    return out


def extract_facts(state: AgentState) -> List[FactInput]:
    """Facts from this turn's tool results, artifacts and lasting instructions (deterministic)."""
    ctx = state.ctx
    symbol = getattr(ctx, "primary_symbol", None)
    topic = symbol  # a Vivarix model is recorded against the target it was simulated for, else its own name
    facts: List[FactInput] = []
    for key, result in list(state.tool_results.items()):
        ok, data, tool = _data(result)
        if not ok or data is None:
            continue
        name = f"{key} {tool}".lower()
        src = {"tool": key}
        found: List[FactInput] = []
        if isinstance(data, dict) and data.get("associations") and ("synapta" in name or "association" in name):
            subject = symbol or str(getattr(result, "canonical_ids", {}).get("target_symbol", "") or "")
            found = _associations(subject, data)
        elif isinstance(data, dict) and ("chembl" in name or "drug" in name) and (data.get("drugs") or data.get("target_approved_drugs")):
            found = _drugs(data)
        elif isinstance(data, dict) and ("trial" in name) and data.get("trials"):
            found = _trials(data)
        elif "vivarix" in name and isinstance(data, dict):
            found = _model(data, topic)
        elif "variant" in name:
            found = _variants(data)
        for f in found:
            f.source = {**src, **(f.source or {})}
        facts.extend(found)
    for f in _hypotheses(state.artifacts.get("hypotheses")):
        f.source = {"tool": "hypotheses"}
        facts.append(f)
    for text, scope in classify_instructions(state.query or ""):
        if scope in ("investigation", "profile"):
            facts.append(FactInput(subject="researcher", relation="instructs", object=neutralise(text), confidence=1.0, source={"origin": "user_instruction", "scope": scope}, tags=["instruction"]))
    return facts[:MAX_FACTS_PER_TURN]


def _verified(state: AgentState) -> bool:
    """A turn's facts are verified only when its answer passed with no unsupported, contradicted or unchecked claim:
    "passed" alone tolerates unsupported claims, which must not turn into verified project facts."""
    v = state.verification
    if v is None:
        return False
    report = v if isinstance(v, dict) else (v.model_dump() if hasattr(v, "model_dump") else {
        k: getattr(v, k, None) for k in ("passed", "fully_verified", "unsupported_claims", "contradicted_claims", "unchecked_claims")
    })
    return turn_fully_verified(bool(report.get("passed")), {k: val for k, val in report.items() if val is not None})


# ---------------------------------------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------------------------------------

class ProjectRecallPhase(AgentPhase):
    key = "project_memory"
    stage = "before_evidence"
    order = 20

    def applies(self, state: AgentState) -> bool:
        return bool(state.project_id)

    async def run(self, state: AgentState) -> Optional[PhaseOutcome]:
        store = _store()
        project = await asyncio.to_thread(store.get_project, state.project_id or "", state.user_id)
        if project is None:
            state.scope_notes.append("The selected research project was not found; project memory was not used.")
            state.bag["project_owned"] = False
            return None
        state.bag["project_owned"] = True
        facts = await asyncio.to_thread(store.recall, project["id"], state.user_id, state.clean_query, query_entities(state), RECALL_K)
        state.project_memory = format_memory_block(project, facts)
        artifact = {"project_id": project["id"], "project_name": project["name"], "recalled": [_public(f) for f in facts], "written": 0, "invalidated": 0}
        state.artifacts["project_memory"] = {**(state.artifacts.get("project_memory") or {}), **artifact}
        await state.step(
            "agent_project_memory",
            f"Recalled {len(facts)} fact(s) from project {project['name'][:60]}" if facts else f"No recorded facts yet in project {project['name'][:60]}",
            "; ".join(f"{f['subject']} {f['relation'].replace('_', ' ')} {f['object']}"[:120] for f in facts[:3]),
            agent="Project memory",
        )
        return None


class ProjectConsolidatePhase(AgentPhase):
    key = "project_memory_consolidate"
    stage = "finalize"
    order = 50

    def applies(self, state: AgentState) -> bool:
        return bool(state.project_id) and state.bag.get("project_owned") is not False

    async def run(self, state: AgentState) -> Optional[PhaseOutcome]:
        store = _store()
        if not await asyncio.to_thread(store.owns, state.project_id, state.user_id):
            return None
        verified = _verified(state)
        facts = extract_facts(state)
        base_source = {"thread_id": state.thread_id, "run_id": state.control.run_id}
        for f in facts:
            f.source = {**base_source, **(f.source or {})}
            f.verified = verified
        result = await asyncio.to_thread(store.upsert_facts, state.project_id or "", state.user_id, facts) if facts else {"written": 0, "invalidated": 0, "ops": []}
        art = dict(state.artifacts.get("project_memory") or {"project_id": state.project_id, "recalled": []})
        art.update({"project_id": state.project_id, "written": result["written"], "invalidated": result["invalidated"], "verified": verified})
        art["operations"] = {op: sum(1 for o in result["ops"] if o["op"] == op) for op in ("ADD", "UPDATE", "INVALIDATE", "NOOP")}
        state.artifacts["project_memory"] = art
        if facts:
            await state.step(
                "agent_project_memory_write",
                f"Saved {result['written']} fact(s) to the project" + (f", {result['invalidated']} superseded" if result["invalidated"] else ""),
                "verified turn" if verified else "unverified turn: facts tagged unverified",
                agent="Project memory",
            )
        return None


register_phase(ProjectRecallPhase())
register_phase(ProjectConsolidatePhase())

__all__ = ["ProjectConsolidatePhase", "ProjectRecallPhase", "extract_facts", "format_memory_block", "query_entities"]
