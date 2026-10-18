"""
Gap-driven re-retrieval (feature 2, stage after_draft).

After each draft the phase looks for what the answer could not support and searches again, within the run budget:

- Self-RAG (Asai et al., ICLR 2024, arXiv:2310.11511): the IsSup rubric {fully, partially, no support} becomes the
  sub-question status {answered, partial, missing}; "partial" and "missing" are gaps that trigger retrieval.
- FLARE (Jiang et al., EMNLP 2023, arXiv:2305.06983): the draft's hedged or unsupported sentences ("unclear",
  "no evidence", "not available", "could not ...") are turned into retrieval queries.
- CRAG (Yan et al. 2024, arXiv:2401.15884): the retrieval for each sub-question is graded correct / ambiguous /
  incorrect (here by deterministic term coverage of the gathered evidence); ambiguous or incorrect means re-query
  with rewritten (keyword-only) terms, against EuropePMC instead of CRAG's web fallback, plus the web when on.
- IRCoT (Trivedi et al., ACL 2023, arXiv:2212.10509): retrieval is interleaved with the reasoning, one retrieval
  round per draft round.

Flow per draft round (once): deterministic coverage per sub-question (state.sub_questions from decomposition, or
pseudo sub-questions from the question and the draft's hedged sentences), an optional model check ([phase:gaps],
{"sub_questions":[{"id","status"}],"queries":[...]}, at most 3 queries), then lithoscribe_literature_synthesis
(and websurfer_search when web search is on and allowed) per query. The phase asks for a re-synthesis only when
passages or web results arrived that were not already in the evidence AND are about the gap they were searched for
(they share the query's distinctive terms); new but off-topic results are dropped from the evidence and never cause
a rewrite. A query is never repeated or near-repeated (token-set Jaccard >= NEAR_DUPLICATE_JACCARD against earlier
gap queries), the model is shown the earlier queries, and a gap whose search found nothing usable is retired
(searched no more, its status kept), so the loop stops when nothing usable is found, when there are no open gaps,
or when the budget is used up.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field

from .guardrails import SPOTLIGHT_RULE, spotlight
from .runtime import AgentPhase, PhaseOutcome, register_phase
from .state import evidence_key_label
from .specialists import (
    LITERATURE_TOOL,
    WEB_TOOL,
    ask_json,
    evidence_digest,
    evidence_text,
    fetch_new_literature,
    fetch_new_web,
    named_tool_allowed,
    not_requested_modules,
    seen_passages,
    seen_urls,
    stems,
)
from .tool_retrieval import tokenize
from ..utils.text import sentence_spans

logger = logging.getLogger(__name__)

MAX_QUERIES = 3
MAX_HEDGED = 3
MAX_ITEMS = 6
# Two gap queries (or two hedged sentences) whose content-stem sets overlap this much are the same search.
NEAR_DUPLICATE_JACCARD = 0.6

_HEDGE_RE = re.compile(
    r"\b(unclear|uncertain|unknown|undetermined|unavailable|not available|could not|cannot be (?:determined|confirmed|assessed)"
    r"|no (?:direct |clear |published |peer[- ]reviewed |clinical |retrieved )?(?:evidence|data|studies)"
    r"|insufficient|not (?:found|reported|established|retrieved|addressed|covered)|lack(?:s|ing)? (?:of )?(?:data|evidence)"
    r"|remains? to be|did not return|was not (?:found|retrieved|available))\b",
    re.I,
)
# Words that say something about the evidence rather than the topic; dropped from rewritten queries (CRAG).
_GENERIC = frozenset(
    "evidence evidences retrieved retrieve sources source data studies study question answer available unavailable "
    "unclear uncertain unknown undetermined insufficient found reported established addressed covered lack lacking "
    "remains remain could cannot determined confirmed assessed direct clear published peer reviewed currently current "
    "here provided results result information whether also however may might still yet any none no not there their "
    "they been being it's its about regarding further more less limited specific particular above below listed".split()
)


# A markdown heading that opens the answer's own limitations section: what it lists is already acknowledged, not a gap.
_LIMITATIONS_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s*|\*\*)\s*(?:evidence\s+)?(?:limitations?|gaps?|caveats?)\b", re.I)
_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s+\S|\*\*[^*]+\*\*\s*:?\s*$)")
# Words that tie a sentence to an analysis the researcher did not ask for (see specialists.not_requested_modules).
MODULE_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "kinetic simulation": re.compile(
        r"\b(?:simulat\w*|kinetic\w*|ODE\b|COPASI|BioModels?|BIOMD\d+|Vivarix|pathway dynamics|time[- ]course|"
        r"steady[- ]state)", re.I,
    ),
    "single-cell analysis": re.compile(r"\b(?:single[- ]cell|scRNA|snRNA|Lumicell|Leiden|cell clusters?)", re.I),
}


def mentions_modules(text: str, modules: List[str]) -> bool:
    """Whether the text is about one of these (not requested) analyses."""
    return any(MODULE_PATTERNS[m].search(text or "") for m in modules if m in MODULE_PATTERNS)


class _GapItem(BaseModel):
    id: str
    status: str = ""


class _GapReply(BaseModel):
    sub_questions: List[_GapItem] = Field(default_factory=list)
    queries: List[str] = Field(default_factory=list)


GAPS_SYSTEM = (
    "[phase:gaps] You check a draft biomedical answer for evidence gaps, using the Self-RAG support rubric. For each "
    "sub-question decide whether the gathered evidence answers it: answered (fully supported), partial (partly "
    "supported) or missing (no support). Then propose at most 3 short literature search queries (key entities and "
    "terms only, no prose) for the partial or missing ones. Do not propose a query for something the evidence "
    "already answers. The draft's own limitations section and analyses the researcher did not ask for (listed as "
    "not requested) are never gaps: propose no query for them. Reply with only JSON: "
    '{"sub_questions":[{"id":"...","status":"answered|partial|missing"}],"queries":["..."]}\n' + SPOTLIGHT_RULE
)


def _without_limitations(draft: str) -> str:
    """The draft minus its "Evidence limitations" / "Limitations" section (up to the next heading)."""
    keep: List[str] = []
    skipping = False
    for line in (draft or "").splitlines():
        if _LIMITATIONS_HEADING_RE.match(line):
            skipping = True
            continue
        if skipping and _HEADING_RE.match(line):
            skipping = False
        if not skipping:
            keep.append(line)
    return "\n".join(keep)


def hedged_sentences(draft: str, limit: int = MAX_HEDGED, skip_modules: Optional[List[str]] = None) -> List[str]:
    """Sentences of the draft that hedge or admit missing evidence (FLARE-style low-confidence spans).

    Sentences under the answer's limitations heading and sentences about analyses that were not requested
    (skip_modules, e.g. "kinetic simulation") are not gaps: they are acknowledged scope, not missing evidence."""
    out: List[str] = []
    body = _without_limitations(draft)
    for raw in (body[s:e] for s, e in sentence_spans(body)):
        s = re.sub(r"^[\s#>*\-|0-9.)]+", "", raw).strip(" *_|")
        if len(s.split()) < 5 or not _HEDGE_RE.search(s):
            continue
        if skip_modules and mentions_modules(s, skip_modules):
            continue
        if s not in out:
            out.append(s[:400])
        if len(out) >= limit:
            break
    return out


def rewrite_query(text: str, focus: List[str], max_words: int = 10) -> str:
    """Keyword-only rewrite of a sub-question or sentence, anchored on the question's focus entities (CRAG)."""
    words: List[str] = []
    seen: Set[str] = set()
    cleaned = _HEDGE_RE.sub(" ", text or "")
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-+/]*", cleaned):
        lw = w.lower()
        if lw in seen or lw in _GENERIC or not tokenize(w) or len(w) < 3 and not w.isupper():
            continue
        seen.add(lw)
        words.append(w)
    lead = [f for f in focus if f and not any(part.lower() in seen for part in f.split())]
    return " ".join([*lead, *words][:max_words]).strip()


def _norm(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().lower())


_GENERIC_STEMS = frozenset(w[:6] for w in _GENERIC)


def content_stems(text: str) -> Set[str]:
    """Topic stems of a query or sentence: stems() minus the words that only talk about the evidence."""
    return stems(text) - _GENERIC_STEMS


def jaccard(a: Set[str], b: Set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


# Requirement facets that make two searches different obligations however many words they share (source audit F5:
# "sotorasib FDA approval NSCLC" vs "... colorectal cancer" is 0.625 Jaccard, but a different requirement).
_DISEASE_ABBREVIATIONS = {"mcrc": "colorectal", "crc": "colorectal", "nsclc": "lung", "sclc": "small-cell lung", "pdac": "pancreatic"}
_VARIANT_RE = re.compile(r"\b[ACDEFGHIKLMNPQRSTVWY]\d{1,4}[ACDEFGHIKLMNPQRSTVWY]\b")
_REGIMEN_RES = {
    "combination": re.compile(r"\bcombination\b|\bcombined\s+with\b|\bplus\b|\s\+\s|\bdoublet\b|\bwith\s+(?:cetuximab|panitumumab)\b", re.I),
    "monotherapy": re.compile(r"\bmono(?:therapy)?\b|\bsingle[- ]agent\b|\balone\b", re.I),
}
_LINE_RES = {
    "1L": re.compile(r"\b(?:first[- ]line|1L|front[- ]line|treatment[- ]na[iï]ve|untreated)\b", re.I),
    "2L": re.compile(r"\b(?:second[- ]line|2L)\b(?!\+)", re.I),
    "2L+": re.compile(r"\b(?:2L\+|second[- ]line[- ]or[- ]later|second[- ]line and beyond|later[- ]line|previously[- ]treated|pretreated|refractory)", re.I),
}
_JURISDICTION_RES = {
    "FDA": re.compile(r"\b(?:fda|u\.?s\.?\s+food)\b", re.I),
    "EMA": re.compile(r"\b(?:ema|european medicines|chmp)\b", re.I),
    "PMDA": re.compile(r"\bpmda\b", re.I),
    "NMPA": re.compile(r"\b(?:nmpa|china)\b", re.I),
}
_ENDPOINT_RES = {
    "OS": re.compile(r"\boverall survival\b|\bOS\b"),
    "PFS": re.compile(r"\bprogression[- ]free\b|\bPFS\b"),
    "ORR": re.compile(r"\b(?:objective |overall )?response rate\b|\bORR\b"),
    "DOR": re.compile(r"\bduration of response\b|\bDOR\b"),
    "safety": re.compile(r"\b(?:adverse events?|toxicit\w*|safety)\b", re.I),
}
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _drugs_in(text: str) -> Set[str]:
    from ..drugintel import names as drug_names

    found = {m.group(1).lower() for m in drug_names.INN_SUFFIX_RE.finditer(text or "") if len(m.group(1)) >= 6}
    found |= {d.lower() for d in drug_names.CURATED_DRUGS if re.search(rf"\b{re.escape(d)}\b", text or "", re.I)}
    return {d for d in found if d not in getattr(drug_names, "_NOT_DRUGS", set())}


def _diseases_in(text: str) -> Set[str]:
    from ..lithoscribe.synthesis_engine import cancer_families

    fams = set(cancer_families(text or ""))
    for word in re.findall(r"[A-Za-z]+", text or ""):
        if word.lower() in _DISEASE_ABBREVIATIONS:
            fams.add(_DISEASE_ABBREVIATIONS[word.lower()])
    return fams


def requirement_keys(text: str) -> Dict[str, frozenset]:
    """The structured requirement a search stands for: disease, variant, drugs, regimen, treatment line,
    jurisdiction, year, endpoint and explicit identifiers. Two searches with different keys are different
    obligations; lexical similarity decides only between searches with the same keys."""
    from ..lithoscribe.identifiers import extract_literature_ids

    t = text or ""
    keys = {
        "disease": frozenset(_diseases_in(t)),
        "variant": frozenset(v.upper() for v in _VARIANT_RE.findall(t)),
        "drugs": frozenset(_drugs_in(t)),
        "regimen": frozenset(k for k, rx in _REGIMEN_RES.items() if rx.search(t)),
        "line": frozenset(k for k, rx in _LINE_RES.items() if rx.search(t)),
        "jurisdiction": frozenset(k for k, rx in _JURISDICTION_RES.items() if rx.search(t)),
        "year": frozenset(_YEAR_RE.findall(t)),
        "endpoint": frozenset(k for k, rx in _ENDPOINT_RES.items() if rx.search(t)),
        "ids": frozenset(extract_literature_ids(t)),
    }
    return keys


def _lexical_stems(text: str) -> Set[str]:
    """Content stems with disease names folded to their family, so "CRC" and "colorectal cancer" are one word."""
    folded = text or ""
    for abbr, fam in _DISEASE_ABBREVIATIONS.items():
        folded = re.sub(rf"\b{abbr}\b", fam, folded, flags=re.I)
    return content_stems(folded) - {"cancer", "cance", "carcin"}


def near_duplicate(text: str, previous: List[str], threshold: float = NEAR_DUPLICATE_JACCARD) -> bool:
    """Whether text is the same search as one of previous: exact after normalisation, or the same requirement keys
    and overlapping content stems. Searches for a different disease, regimen, line, jurisdiction, year, endpoint or
    identifier are never duplicates, however many words they share."""
    n, keys, s = _norm(text), requirement_keys(text), _lexical_stems(text)
    for p in previous:
        if n == _norm(p):
            return True
        if requirement_keys(p) == keys and jaccard(s, _lexical_stems(p)) >= threshold:
            return True
    return False


def gap_outcome(stats: List[Dict[str, Any]]) -> str:
    """What one gap's searches (literature, web) amounted to: filled (usable new evidence), off_topic (new results,
    none about the gap), already_seen (results, all already in the evidence), empty (a successful search with no
    results), incomplete (the search ran out of time or skipped tiers and found nothing usable) or error (every
    search failed or timed out). Only empty and already_seen are definitive; the others get one alternative."""
    if any(s.get("kept") for s in stats):
        return "filled"
    ok = [s for s in stats if s.get("status") in ("ok", "empty")]
    if not ok:
        return "error"
    if any(s.get("new") for s in ok):
        return "off_topic"
    if any(s.get("incomplete") for s in ok):
        return "incomplete"
    if any(s.get("returned") for s in ok):
        return "already_seen"
    return "empty"


DEFINITIVE_OUTCOMES = ("empty", "already_seen")


def useful_for_gap(query: str, item_text: str, doc_text: str, focus: List[str]) -> bool:
    """Whether a newly retrieved passage or web result is about the gap it was searched for.

    The query's content stems (the gap's own when the query has none) must appear in the document: at least one that
    is not a focus entity (every result of the run mentions the question's gene) and, for queries of two or more
    stems, at least two in all. A result that only repeats the focus entity is new but does not fill the gap."""
    # 5-character prefixes: "mutated" and "mutation" (or "approved"/"approval") are the same topic word here.
    def short(words: Set[str]) -> Set[str]:
        return {w[:5] for w in words}

    want = short(content_stems(query) or content_stems(item_text))
    if not want:
        return False
    focus_stems: Set[str] = set()
    for f in focus:
        focus_stems |= short(stems(f))
    have = {t[:5] for t in tokenize(doc_text) if len(t) > 2}
    shared = want & have
    distinctive = want - focus_stems
    if distinctive and not (distinctive & have):
        return False
    return len(shared) >= min(2, len(want))


def _doc_text(kind: str, doc: Dict[str, Any]) -> str:
    if kind == "lit":
        return f"{doc.get('title') or ''} {doc.get('passage_text') or ''}"
    return f"{doc.get('title') or ''} {doc.get('snippet') or doc.get('content') or doc.get('description') or ''}"


def coverage(text: str, ev_stems: Set[str]) -> float:
    terms = stems(text)
    return len(terms & ev_stems) / len(terms) if terms else 1.0


def crag_grade(cov: float) -> str:
    return "correct" if cov >= 0.75 else ("ambiguous" if cov >= 0.4 else "incorrect")


class GapPhase(AgentPhase):
    key = "gaps"
    stage = "after_draft"
    order = 10
    frameworks = None  # every agentic framework

    def applies(self, state: Any) -> bool:
        if getattr(state.ctx, "scope", "research") != "research" or not (state.draft or "").strip():
            return False
        if state.draft_round in (state.bag.get("gap_rounds_done") or []):
            return False
        return self._lit_ok(state) or self._web_ok(state)

    @staticmethod
    def _lit_ok(state: Any) -> bool:
        return named_tool_allowed(state, LITERATURE_TOOL)

    @staticmethod
    def _web_ok(state: Any) -> bool:
        return bool(getattr(state.ctx, "enable_web_search", False)) and named_tool_allowed(state, WEB_TOOL)

    def _items(self, state: Any, ev_stems: Set[str]) -> List[Dict[str, Any]]:
        skip = not_requested_modules(state.ctx, state.tool_results)
        retired = set(state.bag.get("gap_retired") or [])
        searched_hedges = list(state.bag.get("gap_hedged_done") or [])
        # A hedged sentence the loop already searched for comes back in every rewrite ("...was not retrieved"); it is
        # the same gap, not a new one.
        hedged = [h for h in hedged_sentences(state.draft, skip_modules=skip) if not near_duplicate(h, searched_hedges)]
        items: List[Dict[str, Any]] = []
        if state.sub_questions:
            for q in state.sub_questions:
                if q.status == "unanswerable" or q.id in retired:
                    continue  # already searched for in an earlier round without a usable result
                if mentions_modules(q.text, skip) and not mentions_modules(state.clean_query or state.query or "", skip):
                    continue  # a sub-question about an analysis nobody asked for
                q_stems = stems(q.text)
                # Long sub-questions rarely share half their stems with the draft's "X was not retrieved" sentence, so
                # three shared content stems ("silico", "binding", "predict") are enough to count it as hedged.
                is_hedged = any(len(q_stems & stems(h)) >= max(1, min(3, len(q_stems) // 2)) for h in hedged)
                items.append({"id": q.id, "text": q.text, "sq": q, "hedged": is_hedged})
            covered = {h for h in hedged for it in items if it["hedged"] and stems(it["text"]) & stems(h)}
            hedged = [h for h in hedged if h not in covered]
        elif "q0" not in retired:
            items.append({"id": "q0", "text": state.clean_query or state.query, "sq": None, "hedged": False})
        for i, h in enumerate(hedged, 1):
            items.append({"id": f"h{i}", "text": h, "sq": None, "hedged": True})
        items = items[:MAX_ITEMS]
        for it in items:
            cov = coverage(it["text"], ev_stems)
            it["coverage"] = round(cov, 2)
            it["crag"] = crag_grade(cov)
            if it["hedged"]:
                it["status"] = "missing" if cov < 0.75 else "partial"
            else:
                it["status"] = {"correct": "answered", "ambiguous": "partial", "incorrect": "missing"}[it["crag"]]
        return items

    async def _model_check(self, state: Any, items: List[Dict[str, Any]]) -> Optional[_GapReply]:
        digest, _keys = evidence_digest(state, max_items=10, chars=220)
        listing = "\n".join(
            f'- id={it["id"]} (deterministic: {it["status"]}, term coverage {it["coverage"]}): {it["text"]}' for it in items
        )
        skip = not_requested_modules(state.ctx, state.tool_results)
        previous = list(state.bag.get("gap_queries") or [])
        user = (
            f"Question: {state.clean_query or state.query}\n\nSub-questions and draft claims to check:\n{listing}\n\n"
            + (f"Not requested (never a gap, no query): {', '.join(skip)}\n\n" if skip else "")
            + (
                "Already searched in earlier rounds (do not repeat or rephrase these; propose no query when only these "
                "would help):\n" + "\n".join(f"- {q}" for q in previous[-12:]) + "\n\n"
                if previous else ""
            )
            + f"Draft answer:\n{spotlight(_without_limitations(state.draft), 'draft answer', 6000)}\n\n"
            f"Gathered evidence:\n{digest}"
        )
        reply = await ask_json(state, GAPS_SYSTEM, user, phase=self.key, schema=_GapReply)
        return reply if isinstance(reply, _GapReply) else None

    async def run(self, state: Any) -> Optional[PhaseOutcome]:
        round_no = state.draft_round
        state.bag.setdefault("gap_rounds_done", []).append(round_no)
        step_id = f"agent_gaps_{round_no}"
        await state.step(step_id, "Checking the draft for evidence gaps", status="running", agent="Gap checker")

        ev_stems = stems(evidence_text(state.tool_results))
        items = self._items(state, ev_stems)
        by_id = {it["id"]: it for it in items}
        checker = "deterministic"
        model_queries: List[str] = []
        reply = await self._model_check(state, items)
        if reply is not None:
            checker = "llm"
            for row in reply.sub_questions:
                status = (row.status or "").strip().lower()
                if row.id in by_id and status in ("answered", "partial", "missing"):
                    by_id[row.id]["status"] = status
            skip = not_requested_modules(state.ctx, state.tool_results)
            model_queries = [
                str(q).strip()[:200] for q in reply.queries if str(q).strip() and not mentions_modules(str(q), skip)
            ]

        gaps = sorted((it for it in items if it["status"] in ("partial", "missing")), key=lambda it: it["status"] != "missing")
        focus = list(getattr(state.ctx, "focus_terms", lambda: [])() or [])[:2]
        candidates: List[Dict[str, Any]] = []
        if model_queries:
            for q in model_queries:
                q_stems = stems(q)
                target = max(gaps or items, key=lambda it: len(q_stems & stems(it["text"])), default=None)
                candidates.append({"query": q, "item": target})
        elif gaps:
            candidates = [{"query": rewrite_query(it["text"], focus), "item": it} for it in gaps]

        done_queries: List[str] = list(state.bag.get("gap_queries") or [])
        question = _norm(state.clean_query or state.query)
        queries: List[Dict[str, Any]] = []
        duplicates: List[str] = []
        for c in candidates:
            q = c["query"]
            if not _norm(q) or _norm(q) == question:
                continue
            if near_duplicate(q, done_queries):
                duplicates.append(q)
                continue
            done_queries.append(q)
            queries.append(c)
            if len(queries) >= MAX_QUERIES:
                break

        report: Dict[str, Any] = {
            "round": round_no,
            "checker": checker,
            "items": [{k: it[k] for k in ("id", "text", "status", "coverage", "crag", "hedged")} for it in items],
            "queries": [c["query"] for c in queries],
            "skipped_duplicate_queries": duplicates,
            "new_evidence_keys": [],
            "novelty": {"new_passages": 0, "useful_passages": 0, "new_web": 0, "useful_web": 0, "dropped_keys": []},
        }
        state.bag.setdefault("gap_report", []).append(report)

        if not queries:
            self._update_sub_questions(items, {}, searched=set())
            # Gaps whose only queries were repeats are as searched as they will get: stop checking them.
            if duplicates:
                self._retire(state, [c["item"] for c in candidates if c["item"] is not None and c["item"] in gaps])
            text = "No evidence gaps found." if not gaps else (
                f"{len(gaps)} gap(s) remain, but their searches already ran without new evidence."
            )
            await state.step(step_id, "Checked the draft for evidence gaps", text, agent="Gap checker")
            return None

        lit_ok, web_ok = self._lit_ok(state), self._web_ok(state)
        seen = seen_passages(state.tool_results)
        urls = seen_urls(state.tool_results)
        novelty = report["novelty"]
        new_by_item: Dict[str, List[str]] = {}

        async def search(c: Dict[str, Any], q: str) -> Dict[str, Any]:
            """One gap query's literature (and web) search. Only results about the gap are stored (off-topic ones
            never enter the evidence); returns the stored keys and each search's outcome statistics."""
            item_text = c["item"]["text"] if c["item"] is not None else ""
            purpose = f"gap re-retrieval: {q}"
            keywords = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]+", q) if w.lower() not in _GENERIC][:6]
            keys: List[str] = []
            stats: List[Dict[str, Any]] = []

            def keep(kind: str) -> Any:
                return lambda d: useful_for_gap(q, item_text, _doc_text(kind, d), focus)

            if lit_ok:
                st: Dict[str, Any] = {}
                key, kept = await fetch_new_literature(
                    state, phase=self.key, query=q, purpose=purpose, seen=seen, keywords=keywords, keep=keep("lit"), stats=st,
                )
                stats.append(st)
                novelty["new_passages"] += int(st.get("new") or 0)
                novelty["useful_passages"] += len(kept)
                if key:
                    keys.append(key)
            if web_ok and not state.budget.exhausted():
                st = {}
                key, kept = await fetch_new_web(state, phase=self.key, query=q, purpose=purpose, seen=urls, keep=keep("web"), stats=st)
                stats.append(st)
                novelty["new_web"] += int(st.get("new") or 0)
                novelty["useful_web"] += len(kept)
                if key:
                    keys.append(key)
            return {"c": c, "query": q, "keys": keys, "outcome": gap_outcome(stats)}

        def remember(c: Dict[str, Any], q: str) -> None:
            state.bag.setdefault("gap_queries", []).append(q)
            if c["item"] is not None and c["item"].get("hedged"):
                state.bag.setdefault("gap_hedged_done", []).append(c["item"]["text"])

        # The per-query searches are independent: run them concurrently (each still charges the run budget).
        runnable = [c for c in queries if not state.budget.exhausted()]
        for c in runnable:
            remember(c, c["query"])
        results = list(await asyncio.gather(*(search(c, c["query"]) for c in runnable)))

        # A search that failed, timed out, ran out of time, or returned only off-topic results gets one alternative
        # strategy (a keyword-only rewrite of the gap itself, never a rephrasing of the same search); an empty or
        # already-covered result is definitive. A gap still unresolved after that is retired with its reason.
        tried_alt = state.bag.setdefault("gap_alternatives", [])
        alternatives: List[Dict[str, Any]] = []
        for r in results:
            c = r["c"]
            if r["outcome"] == "filled" or r["outcome"] in DEFINITIVE_OUTCOMES or c["item"] is None:
                continue
            if c["item"]["id"] in tried_alt or state.budget.exhausted() or len(alternatives) >= MAX_QUERIES:
                continue
            alt_q = rewrite_query(c["item"]["text"], focus, max_words=8)
            if not _norm(alt_q) or near_duplicate(alt_q, list(state.bag.get("gap_queries") or [])):
                continue
            tried_alt.append(c["item"]["id"])
            remember(c, alt_q)
            alternatives.append({"c": c, "query": alt_q, "first": r["outcome"]})
        if alternatives:
            alt_results = await asyncio.gather(*(search(a["c"], a["query"]) for a in alternatives))
            for a, r in zip(alternatives, alt_results):
                r["first"] = a["first"]
                results.append(r)
        report["alternatives"] = [{"query": a["query"], "after": a["first"]} for a in alternatives]

        outcomes: Dict[str, str] = {}
        for r in results:
            c = r["c"]
            item_id = c["item"]["id"] if c["item"] is not None else ""
            if r["keys"]:
                new_by_item.setdefault(item_id, []).extend(r["keys"])
            # The best outcome per gap wins (filled over anything; a definitive answer over a failure).
            prev = outcomes.get(item_id)
            if prev != "filled" and (r["outcome"] == "filled" or prev is None or prev not in DEFINITIVE_OUTCOMES):
                outcomes[item_id] = r["outcome"]
        report["outcomes"] = outcomes

        new_keys = [k for keys in new_by_item.values() for k in keys]
        report["new_evidence_keys"] = new_keys
        searched_ids = {c["item"]["id"] for c in runnable if c["item"] is not None}
        self._update_sub_questions(items, new_by_item, searched=searched_ids, round_no=round_no, outcomes=outcomes)
        # A gap searched this round without a usable result is retired: re-asking it only rephrases the same search.
        self._retire(
            state,
            [c["item"] for c in runnable if c["item"] is not None and c["item"]["id"] not in new_by_item],
            outcomes=outcomes,
        )
        query_text = "; ".join([*(c["query"] for c in queries), *(a["query"] for a in alternatives)])
        if not new_keys:
            off_topic = novelty["new_passages"] + novelty["new_web"]
            await state.step(
                step_id, "Checked the draft for evidence gaps",
                f"{len(gaps)} gap(s); searched again ({query_text}) but found nothing new"
                + (f" that addresses them ({off_topic} new off-topic result(s) dropped)." if off_topic else "."),
                agent="Gap checker",
            )
            return None
        await state.step(
            step_id, "Found new evidence for gaps in the draft",
            f"{len(gaps)} gap(s); searched: {query_text}. New and on-topic: {novelty['useful_passages']} passage(s), "
            f"{novelty['useful_web']} web result(s).",
            agent="Gap checker",
        )
        filled = [by_id[i]["text"] for i in new_by_item if i in by_id][:3]
        feedback = (
            f"Gap re-retrieval (round {round_no + 1}) added new evidence"
            + (f" for: {'; '.join(t[:160] for t in filled)}" if filled else "")
            + ". Use the new gap-search documents where they answer these points, cite them, and say plainly "
            "what the evidence still cannot answer."
        )
        return PhaseOutcome(resynthesize=True, feedback=[feedback])

    _OUTCOME_NOTES = {
        "empty": "re-retrieval found no further usable evidence (the search returned nothing)",
        "already_seen": "re-retrieval found no further usable evidence (only records already in the evidence)",
        "off_topic": "unresolved: re-retrieval returned only results about other topics",
        "error": "unresolved: the re-retrieval searches failed or timed out",
        "incomplete": "unresolved: the re-retrieval searches ran out of time before finishing",
    }

    @classmethod
    def _retire(cls, state: Any, items: List[Dict[str, Any]], outcomes: Optional[Dict[str, str]] = None) -> None:
        """Stop checking these gaps; a sub-question keeps its status (partial stays partial) with a note saying why
        (a definitive empty search, or failure, timeout or off-topic results after its one alternative)."""
        retired = state.bag.setdefault("gap_retired", [])
        for it in items:
            if it.get("id") and it["id"] not in retired and not it.get("hedged"):
                retired.append(it["id"])
            q = it.get("sq")
            note = cls._OUTCOME_NOTES.get((outcomes or {}).get(it.get("id") or ""), cls._OUTCOME_NOTES["empty"])
            if q is not None and q.status != "answered" and note not in (q.note or ""):
                q.note = (q.note + "; " if q.note else "") + note

    @staticmethod
    def _update_sub_questions(
        items: List[Dict[str, Any]], new_by_item: Dict[str, List[str]], *, searched: Set[str], round_no: int = 0,
        outcomes: Optional[Dict[str, str]] = None,
    ) -> None:
        for it in items:
            q = it.get("sq")
            if q is None:
                continue
            keys = new_by_item.get(it["id"]) or []
            if keys:
                for k in keys:
                    q.add_evidence(k, evidence_key_label(k, round_no))
                q.status = "partial" if it["status"] != "answered" else "answered"
                q.note = f"new evidence retrieved in gap round {round_no + 1}"
            elif it["status"] == "missing" and it["id"] in searched:
                outcome = (outcomes or {}).get(it["id"], "empty")
                if outcome in DEFINITIVE_OUTCOMES:
                    q.status = "unanswerable"
                    q.note = "no evidence found after re-retrieval"
                else:  # a failed, timed-out or off-topic search does not show the evidence is absent
                    q.status = "missing"
                    q.note = GapPhase._OUTCOME_NOTES.get(outcome, "unresolved")
            else:
                q.status = it["status"] if it["status"] in ("answered", "partial") else q.status


GAP_PHASE = GapPhase()
register_phase(GAP_PHASE)
