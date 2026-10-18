"""
Counter-evidence and contradiction search (feature 7, stage augment).

After the pipeline's evidence exists, the phase looks for literature that argues against its main claims, so the
answer discusses conflicts instead of presenting one side:

- PaperQA2 / ContraCrow (Skarlinski et al. 2024, arXiv:2409.13740): extract claims, search for contradicting
  literature, rate each (claim, passage) pair on an 11-point Likert scale (0 = strongly supports, 5 = lack of
  evidence, 10 = strongly contradicts) and flag a contradiction at 8 or more.
- SciFact (Wadden et al., EMNLP 2020, arXiv:2004.14974) and SciFact-Open (Findings EMNLP 2022, arXiv:2210.13777):
  the SUPPORTS / REFUTES / NOINFO label scheme, here "supports" / "contradicts" / "neutral".
- Chain-of-Verification (Dhuliawala et al., Findings ACL 2024, arXiv:2309.11495): the "find evidence against X"
  verification questions are answered independently of the draft (this phase runs before any draft exists).

Claims come from the evidence (the top target-disease association, and passage sentences that state an
association, mechanism or clinical outcome) or, failing that, from the question. Queries are deterministic
negations ("<entity> resistance", "<entity> no significant effect", "<entity> failed trial", "<entity>
contradicting evidence", "<drug> adverse events"); at most 3 literature calls run and only passages not already in
the evidence are kept. Each pair is classified by, in order: the NLI judge another phase installed
(state.bag["nli_judge_async"], then state.bag["nli_judge_fn"]), the agent model with the Likert rubric
([phase:counter_evidence]), or a cue-word heuristic that is always labelled "heuristic" and never reported as a
certain contradiction.

After the answer is written and verified, a second, model-free pass (CounterEvidenceRecheckPhase, stage finalize)
re-checks the answer's own top claims (numeric and clinical ones from the verifier report, recheck_claims) against
the counter-evidence passages already fetched, with the NLI judge or the cue-word heuristic; rows are added to the
same artifact under "answer_rechecks". No new literature calls are made.

Off when agent_options.enable_counter_evidence is false, when PHRONESIS_COUNTER_EVIDENCE=off, outside research
scope, or when the turn's source restriction excludes the literature tool.
"""

from __future__ import annotations

import inspect
import logging
import re
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from ..phronesis.agents.verifier import truncate_words
from ..utils.text import sentence_spans
from .config import env_flag
from .guardrails import SPOTLIGHT_RULE, neutralise, spotlight
from .runtime import AgentPhase, PhaseOutcome, register_phase
from .specialists import (
    LITERATURE_TOOL,
    ask_json,
    fetch_new_literature,
    iter_passages,
    named_tool_allowed,
    passage_source_key,
    query_entities,
    seen_passages,
    stems,
)

logger = logging.getLogger(__name__)

MAX_QUERIES = 3
MAX_CLAIMS = 3
MAX_PAIRS = 10
FLAG_AT = 8  # ContraCrow: a claim is contradicted at a Likert score of 8 or more

_CLAIM_CUE_RE = re.compile(
    r"\b(associat\w*|inhibit\w*|activat\w*|improv\w*|increas\w*|reduc\w*|decreas\w*|surviv\w*|respon\w*|efficac\w*|"
    r"effective|promot\w*|suppress\w*|driv\w*|mediat\w*|correlat\w*|benefit\w*|sensitiv\w*|prolong\w*)\b",
    re.I,
)
# Cue words for the heuristic classifier: (pattern, weight). Strong cues state a negative finding outright.
_CONTRA_CUES: Tuple[Tuple[re.Pattern, int], ...] = (
    (re.compile(r"\bno (?:statistically )?significant\b", re.I), 3),
    (re.compile(r"\b(?:did|does|do) not (?:improve|reduce|increase|prolong|show|respond|benefit|differ|predict)\b", re.I), 3),
    (re.compile(r"\b(?:not|no longer) associated\b|\bno association\b", re.I), 3),
    (re.compile(r"\bno (?:clinical )?(?:benefit|effect|improvement|difference|activity)\b", re.I), 3),
    (re.compile(r"\b(?:failed|failure) to\b|\bfailed (?:trial|to meet)\b|\bterminated for futility\b", re.I), 3),
    (re.compile(r"\black of (?:efficacy|benefit|response)\b|\bcontrary to\b|\brefut\w*\b", re.I), 3),
    (re.compile(r"\bresistan(?:ce|t)\b", re.I), 2),
    (re.compile(r"\b(?:adverse events?|toxicit(?:y|ies)|discontinu\w*)\b", re.I), 2),
    (re.compile(r"\b(?:inconsistent|conflicting|controvers\w*|contradict\w*)\b", re.I), 2),
)
# A negated safety finding ("no significant adverse events", "without serious toxicity", "was not associated with
# increased toxicity") reports the absence of harm: it is not a negative finding about the claim, so these spans are
# removed before the contradiction cues are counted (AGT-008: a single "no significant" used to read as a contradiction).
_SAFETY_TERMS = (
    r"(?:adverse (?:events?|effects?|reactions?|drug reactions?)|aes?|saes?|toxicit(?:y|ies)|side[- ]effects?"
    r"|safety (?:signals?|concerns?|issues?)|hepatotoxicity|cardiotoxicity|nephrotoxicity|neurotoxicity"
    r"|discontinuations?(?: due to (?:adverse events?|toxicity))?|dose[- ](?:limiting toxicit(?:y|ies)|reductions?)"
    r"|deaths?|mortality|complications?|(?:risk of )?(?:bleeding|infections?|hospitali[sz]ations?))"
)
_SAFETY_QUALIFIERS = (
    r"(?:(?:statistically |clinically )?significant(?:ly)?|serious|severe|major|new|unexpected|additional|increased|excess"
    r"|treatment[- ](?:related|emergent)|drug[- ]related|grade\s*(?:[3-5]|≥\s*3|>=\s*3)(?:\s*(?:or|to|-)\s*(?:[3-5]))?"
    r"|unacceptable|clinically relevant|dose[- ]limiting|increase in|difference in|rise in)\s+"
)
_NEGATED_SAFETY_RE = re.compile(
    rf"\b(?:no|without|absence of|free of|lack of|nor|not any)\s+(?:{_SAFETY_QUALIFIERS}){{0,3}}{_SAFETY_TERMS}\b"
    rf"|\b(?:was|were|is|are)\s+not\s+associated\s+with\s+(?:an?\s+)?(?:{_SAFETY_QUALIFIERS}){{0,3}}{_SAFETY_TERMS}\b"
    rf"|\b(?:did|does|do)\s+not\s+(?:cause|increase|raise|lead to|result in|show|demonstrate|exhibit|observe|produce|induce|reveal|display|report|experience|encounter|have)\s+(?:{_SAFETY_QUALIFIERS}){{0,3}}{_SAFETY_TERMS}\b",
    re.I,
)
_SUPPORT_CUE_RE = re.compile(
    r"\b(significantly (?:improved|increased|reduced|prolonged|associated)|was associated with|were associated with|"
    r"is associated with|improved|effective|efficacy|durable (?:clinical )?benefit|response rate)\b",
    re.I,
)


class _Rating(BaseModel):
    pair: int
    likert: float = 5
    reason: str = ""


class _RatingReply(BaseModel):
    ratings: List[_Rating] = Field(default_factory=list)


COUNTER_SYSTEM = (
    "[phase:counter_evidence] You rate whether a retrieved passage contradicts a biomedical claim, using the "
    "PaperQA2/ContraCrow 11-point scale: 0 = strongly supports, 2 = supports, 5 = lack of evidence or unrelated, "
    "8 = contradicts, 10 = strongly contradicts. A passage contradicts a claim only if it reports a finding that "
    "is incompatible with it in the same setting (a negative trial, resistance, no effect, an opposite direction); a "
    "different setting or population is at most a partial conflict (6-7). Reply with only JSON: "
    '{"ratings":[{"pair":<number>,"likert":<0-10>,"reason":"<one sentence>"}]}\n' + SPOTLIGHT_RULE
)


def _sentences(text: str) -> List[str]:
    text = text or ""
    return [text[s:e].strip() for s, e in sentence_spans(text) if len(text[s:e].split()) >= 5]


def extract_claims(state: Any, entities: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """Up to 3 checkable claims from the gathered evidence, else one from the question."""
    names = [*entities["genes"], *entities["drugs"]]
    name_stems = stems(" ".join(names))
    claims: List[Dict[str, Any]] = []
    gene = entities["genes"][0] if entities["genes"] else ""
    for key, res in list(state.tool_results.items()):  # the top target-disease association
        data = getattr(res, "data", None)
        if not gene or not getattr(res, "success", False) or not isinstance(data, dict):
            continue
        assoc = [a for a in data.get("associations") or [] if isinstance(a, dict) and a.get("disease_name")]
        if assoc:
            top = max(assoc, key=lambda a: float(a.get("overall_association_score") or 0.0))
            claims.append({"text": f"{gene} is associated with {top['disease_name']}", "source_key": key, "entity": gene})
            break
    scored: List[Tuple[float, str, str]] = []
    for rkey, p in iter_passages(state.tool_results):
        for s in _sentences(str(p.get("passage_text") or "")):
            cues = len(_CLAIM_CUE_RE.findall(s))
            if not cues:
                continue
            mention = 2.0 if name_stems & stems(s) else 0.0
            if names and not mention:
                continue
            scored.append((mention + min(cues, 3), truncate_words(s, 300), passage_source_key(p, rkey)))
    for _score, text, skey in sorted(scored, key=lambda t: -t[0]):
        if len(claims) >= MAX_CLAIMS:
            break
        if all(c["text"] != text for c in claims):
            claims.append({"text": text, "source_key": skey, "entity": names[0] if names else ""})
    if not claims:
        claims.append({"text": state.clean_query or state.query, "source_key": "question", "entity": names[0] if names else ""})
    for i, c in enumerate(claims, 1):
        c["id"] = f"c{i}"
    return claims


def negation_queries(entities: Dict[str, List[str]]) -> List[str]:
    """Deterministic contradiction queries for the question's main entity (and drug)."""
    entity = (entities["genes"] or entities["drugs"] or [""])[0]
    drug = entities["drugs"][0] if entities["drugs"] else ""
    if not entity:
        return []
    queries = [f"{entity} resistance"]
    if drug:
        queries.append(f"{drug} adverse events")
    queries += [f"{entity} no significant effect", f"{entity} failed trial", f"{entity} contradicting evidence"]
    return list(dict.fromkeys(queries))[:MAX_QUERIES]


def label_for(likert: float) -> str:
    return "contradicts" if likert >= FLAG_AT else ("supports" if likert <= 2 else "neutral")


def heuristic_rating(claim: str, passage: str) -> Tuple[int, str]:
    """Cue-word estimate of the Likert score: at most FLAG_AT - 1, so cue words alone never reach "contradicts"
    (they can still be flagged as a possible conflict to check at 7). Negated safety findings are not counted as
    negative findings."""
    claim_stems = stems(claim)
    overlap = len(claim_stems & stems(passage))
    if not claim_stems or overlap == 0:
        return 5, "passage does not mention the claim's terms"
    negated_safety = bool(_NEGATED_SAFETY_RE.search(passage))
    scan = _NEGATED_SAFETY_RE.sub(" ", passage)
    hits = [(pat.pattern, w) for pat, w in _CONTRA_CUES if pat.search(scan)]
    contra = sum(w for _p, w in hits)
    if contra:
        return min(FLAG_AT - 1, 5 + contra), "negative-finding cue words in the passage (heuristic, unverified)"
    if _SUPPORT_CUE_RE.search(scan):
        return 3, "supporting cue words in the passage (heuristic, unverified)"
    if negated_safety:
        return 5, "the passage reports an absence of harm (e.g. no significant adverse events), not a negative finding (heuristic)"
    return 5, "no cue words either way (heuristic)"


async def _judge(fn: Any, claim: str, source_key: str, passage: str) -> Optional[Dict[str, Any]]:
    try:
        out = fn(claim, [{"source_key": source_key, "passage": passage}])
        if inspect.isawaitable(out):
            out = await out
    except Exception as exc:  # noqa: BLE001
        logger.info("NLI judge failed on a counter-evidence pair: %s", exc)
        return None
    return out if isinstance(out, dict) and out.get("status") else None


_NLI_LIKERT = {"contradicted": 9, "supported": 1, "unsupported": 5}


class CounterEvidencePhase(AgentPhase):
    key = "counter_evidence"
    stage = "augment"
    order = 60
    frameworks = None  # every agentic framework

    def applies(self, state: Any) -> bool:
        if state.options.get("enable_counter_evidence") is False or env_flag("PHRONESIS_COUNTER_EVIDENCE") == "off":
            return False
        if getattr(state.ctx, "scope", "research") != "research":
            return False
        return named_tool_allowed(state, LITERATURE_TOOL)

    async def run(self, state: Any) -> Optional[PhaseOutcome]:
        entities = query_entities(state.ctx)
        queries = negation_queries(entities)
        if not queries:
            return None
        await state.step("agent_counter_evidence", "Searching for contradicting evidence", "; ".join(queries),
                         status="running", agent="Counter-evidence searcher")
        claims = extract_claims(state, entities)
        seen = seen_passages(state.tool_results)
        new: List[Tuple[str, Dict[str, Any]]] = []
        for q in queries:
            if state.budget.exhausted():
                break
            keywords = [w for w in q.split() if w.lower() not in ("evidence", "effect", "events")] + entities["diseases"][:1]
            key, passages = await fetch_new_literature(
                state, phase=self.key, query=q, purpose=f"counter-evidence search: {q}", seen=seen,
                keywords=keywords, max_articles=4, render=False,
            )
            new.extend((key, p) for p in passages if key)

        pairs: List[Dict[str, Any]] = []
        for rkey, p in new[:MAX_PAIRS]:
            text = re.sub(r"\s+", " ", str(p.get("passage_text") or "")).strip()
            p_stems = stems(text)
            claim = max(claims, key=lambda c: len(stems(c["text"]) & p_stems))
            pairs.append({
                "claim_id": claim["id"], "claim": claim["text"], "passage": text[:1200],
                "source_key": passage_source_key(p, rkey), "result_key": rkey,
                "pmid": str(p.get("pmid") or ""), "doi": str(p.get("doi") or ""),
                "citation": str(p.get("citation") or p.get("citation_label") or ""),
                "setting": str(p.get("grade_label") or p.get("experimental_setting") or ""),
            })
        await self._classify(state, pairs)

        flagged = [r for r in pairs if r["likert"] >= FLAG_AT or (r["classifier"] == "heuristic" and r["likert"] >= 7)]
        classifiers = [r["classifier"] for r in pairs]
        top = max(set(classifiers), key=classifiers.count) if classifiers else "heuristic"
        artifact = {
            "queries": queries,
            "claims_checked": len(claims),
            "classifier": top,
            "claims": [{"id": c["id"], "text": c["text"], "source_key": c["source_key"]} for c in claims],
            "conflicts": [
                {
                    "claim": r["claim"], "passage": r["passage"][:600], "source_key": r["source_key"], "pmid": r["pmid"],
                    "doi": r["doi"], "label": r["label"], "likert": r["likert"], "score": r["score"],
                    "classifier": r["classifier"], "reason": r["reason"][:240],
                }
                for r in pairs
            ][:50],
        }
        state.artifacts["evidence_conflicts"] = artifact
        if flagged:
            state.evidence_sections.append(self._section(claims, flagged, top))
            heuristic = any(r["classifier"] == "heuristic" for r in flagged)
            state.brief_notes.append(
                f"A counter-evidence search found {len(flagged)} passage(s) that may conflict with the main claims "
                "(<evidence_conflicts>). Discuss these conflicts explicitly: name the claim each bears on, compare the "
                "strength and setting of the sources, and say whether the conflict changes the conclusion."
                + (" Some were flagged by cue words only (classifier=heuristic): present them as possible conflicts "
                   "to check, not as established contradictions." if heuristic else "")
            )
        text = (
            f"Checked {len(claims)} claim(s) against {len(pairs)} new passage(s): "
            f"{len(flagged)} possible conflict(s) (classifier: {top})." if pairs else
            "No new passages beyond the evidence already gathered."
        )
        await state.step("agent_counter_evidence", "Searched for contradicting evidence", text, agent="Counter-evidence searcher")
        return None

    async def _classify(self, state: Any, pairs: List[Dict[str, Any]]) -> None:
        # The async judge may call the NLI model on a cache miss; the sync one answers from its cache or local model.
        judges = [j for j in (state.bag.get("nli_judge_async"), state.bag.get("nli_judge_fn")) if callable(j)]
        pending: List[Dict[str, Any]] = []
        for r in pairs:
            out = None
            for judge in judges:
                out = await _judge(judge, r["claim"], r["source_key"], r["passage"])
                if out is not None:
                    break
            status = str((out or {}).get("status") or "").lower()
            if status in _NLI_LIKERT:
                likert = _NLI_LIKERT[status]
                score = (out or {}).get("score", (out or {}).get("probability"))
                r.update(likert=likert, label=label_for(likert), classifier="nli", reason=str((out or {}).get("reason") or ""),
                         score=round(float(score), 3) if isinstance(score, (int, float)) else round(likert / 10, 2))
            else:
                pending.append(r)
        if pending:
            ratings = await self._model_ratings(state, pending)
            for i, r in enumerate(pending, 1):
                if i in ratings:
                    likert, reason = ratings[i]
                    r.update(likert=likert, label=label_for(likert), classifier="llm", reason=reason, score=round(likert / 10, 2))
                else:
                    likert, reason = heuristic_rating(r["claim"], r["passage"])
                    # A heuristic score is a weak signal: its score is scaled down so it never reads as certain.
                    r.update(likert=likert, label=label_for(likert), classifier="heuristic", reason=reason,
                             score=round(likert / 10 * 0.6, 2))

    async def _model_ratings(self, state: Any, pairs: List[Dict[str, Any]]) -> Dict[int, Tuple[int, str]]:
        blocks = [
            f"Pair {i}:\nClaim: {spotlight(r['claim'], 'claim', 400)}\nPassage ({r['source_key']}): "
            f"{spotlight(r['passage'], r['source_key'] or 'passage', 1000)}"
            for i, r in enumerate(pairs, 1)
        ]
        reply = await ask_json(state, COUNTER_SYSTEM, "\n\n".join(blocks), phase=self.key, schema=_RatingReply)
        if not isinstance(reply, _RatingReply):
            return {}
        out: Dict[int, Tuple[int, str]] = {}
        for row in reply.ratings:
            if 1 <= row.pair <= len(pairs):
                out[row.pair] = (int(round(max(0.0, min(10.0, float(row.likert))))), row.reason[:300])
        return out

    @staticmethod
    def _section(claims: List[Dict[str, Any]], rows: List[Dict[str, Any]], classifier: str) -> str:
        heuristic = any(r["classifier"] == "heuristic" for r in rows)
        note = (
            "Passages from a targeted search for contradicting evidence (PaperQA2/ContraCrow scale: 0 strongly supports, "
            "5 no evidence, 10 strongly contradicts; flagged at 8 or more). Discuss each conflict explicitly."
            + (" Rows with classifier=heuristic were flagged by cue words only and are unverified possible conflicts."
               if heuristic else "")
        )
        used = {r["claim_id"] for r in rows}
        parts = [f'<evidence_conflicts classifier="{escape(classifier, quote=True)}" note="{escape(note, quote=True)}">']
        for c in claims:
            if c["id"] in used:
                parts.append(f'<claim id="{escape(c["id"], quote=True)}">{escape(neutralise(c["text"]), quote=False)}</claim>')
        for r in rows:
            attrs = {
                "claim": r["claim_id"], "label": r["label"], "likert": str(r["likert"]), "classifier": r["classifier"],
                "flag": "flagged" if r["likert"] >= FLAG_AT else "possible",
                "certainty": "low" if r["classifier"] == "heuristic" else ("model" if r["classifier"] == "llm" else "nli"),
                "source_key": r["source_key"], "pmid": r["pmid"], "doi": r["doi"], "citation": r["citation"],
                "setting": r["setting"] or "unspecified",
            }
            attr_text = " ".join(f'{k}="{escape(v, quote=True)}"' for k, v in attrs.items())
            parts.append(f"<conflict {attr_text}>{spotlight(r['passage'], r['source_key'] or 'passage', 700)}</conflict>")
        parts.append("</evidence_conflicts>")
        return "\n".join(parts)


COUNTER_EVIDENCE_PHASE = CounterEvidencePhase()
register_phase(COUNTER_EVIDENCE_PHASE)


# After-draft re-check ----------------------------------------------------------------------------------------
MAX_RECHECK_CLAIMS = 5
_FIGURE_RE = re.compile(r"\d")


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def recheck_claims(report: Any, limit: int = MAX_RECHECK_CLAIMS) -> List[Dict[str, Any]]:
    """The answer's top claims for a counter-evidence re-check: clinical claims and claims with a figure from the
    verifier report (contradicted ones are already flagged and skipped), clinical first."""
    picked: List[Tuple[int, Dict[str, Any]]] = []
    for c in list(_attr(report, "claims", None) or []):
        text = str(_attr(c, "claim_text", "") or "").strip()
        kind = str(_attr(c, "claim_type", "") or "")
        status = str(_attr(c, "status", "") or "")
        if not text or status == "contradicted" or kind == "absence":
            continue
        numeric = bool(_FIGURE_RE.search(re.sub(r"\bPMID:?\s*\d+|\bNCT\d{8}\b", " ", text, flags=re.IGNORECASE)))
        if kind != "clinical" and not numeric:
            continue
        evidence = list(_attr(c, "evidence", None) or [])
        source = str(_attr(evidence[0], "source_key", "") or "") if evidence else ""
        rank = (0 if kind == "clinical" else 1) * 2 + (0 if numeric else 1)
        picked.append((rank, {"id": f"a{len(picked) + 1}", "text": text, "source_key": source or "answer"}))
    picked.sort(key=lambda t: t[0])
    return [c for _r, c in picked[:limit]]


def recheck_pairs(claims: List[Dict[str, Any]], passages: List[Dict[str, Any]], max_pairs: int = MAX_PAIRS) -> List[Dict[str, Any]]:
    """(answer claim, counter-evidence passage) pairs: each claim with the passages sharing at least two stems,
    never the claim's own supporting source."""
    pairs: List[Dict[str, Any]] = []
    for c in claims:
        c_stems = stems(c["text"])
        for p in passages:
            text = str(p.get("passage") or "")
            if not text or p.get("source_key") == c["source_key"] or len(c_stems & stems(text)) < 2:
                continue
            pairs.append({
                "claim_id": c["id"], "claim": c["text"], "passage": text, "source_key": str(p.get("source_key") or ""),
                "pmid": str(p.get("pmid") or ""), "doi": str(p.get("doi") or ""),
            })
            if len(pairs) >= max_pairs:
                return pairs
    return pairs


async def recheck_answer_claims(state: Any, claims: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Re-check the answer's claims against the counter-evidence passages already in the evidence_conflicts
    artifact, with the NLI judge (if installed) or the cue-word heuristic. No model or literature calls. A heuristic
    row is capped below the contradiction flag so cue words alone never count as a contradiction."""
    art = state.artifacts.get("evidence_conflicts") if isinstance(state.artifacts, dict) else None
    passages = [r for r in ((art or {}).get("conflicts") or []) if isinstance(r, dict)]
    claims = recheck_claims(state.verification) if claims is None else claims
    pairs = recheck_pairs(claims, passages)
    judges = [j for j in (state.bag.get("nli_judge_async"), state.bag.get("nli_judge_fn")) if callable(j)]
    for r in pairs:
        out = None
        for judge in judges:
            out = await _judge(judge, r["claim"], r["source_key"], r["passage"])
            if out is not None:
                break
        status = str((out or {}).get("status") or "").lower()
        if status in _NLI_LIKERT:
            likert, classifier, reason = _NLI_LIKERT[status], "nli", str((out or {}).get("reason") or "")
        else:
            likert, reason = heuristic_rating(r["claim"], r["passage"])  # already capped below FLAG_AT
            likert, classifier = min(likert, FLAG_AT - 1), "heuristic"
        r.update(likert=likert, label=label_for(likert), classifier=classifier, reason=truncate_words(reason, 240),
                 score=round(likert / 10 * (0.6 if classifier == "heuristic" else 1.0), 2), phase="after_draft")
    return pairs


class CounterEvidenceRecheckPhase(AgentPhase):
    """After the answer is verified: re-check its top numeric/clinical claims against the counter-evidence
    passages fetched before the draft. Runs before confidence (order 10), which reads contradicting rows."""

    key = "counter_evidence_recheck"
    stage = "finalize"
    order = 5
    frameworks = None

    def applies(self, state: Any) -> bool:
        art = state.artifacts.get("evidence_conflicts") if isinstance(state.artifacts, dict) else None
        return bool(isinstance(art, dict) and art.get("conflicts") and state.verification is not None)

    async def run(self, state: Any) -> Optional[PhaseOutcome]:
        claims = recheck_claims(state.verification)
        if not claims:
            return None
        rows = await recheck_answer_claims(state, claims)
        art = state.artifacts["evidence_conflicts"]
        art["answer_rechecks"] = rows[:MAX_PAIRS]
        flagged = [r for r in rows if r["likert"] >= FLAG_AT]
        if flagged:
            art["conflicts"] = list(art.get("conflicts") or []) + [
                {k: r[k] for k in ("claim", "passage", "source_key", "pmid", "doi", "label", "likert", "score", "classifier", "reason", "phase")}
                for r in flagged
            ]
        if rows:
            await state.step(
                "agent_counter_recheck",
                "Re-checked the answer's claims against contradicting evidence",
                f"{len(claims)} answer claim(s) against {len(rows)} passage pair(s): {len(flagged)} contradiction(s)",
                agent="Counter-evidence searcher",
            )
        return None


COUNTER_EVIDENCE_RECHECK_PHASE = CounterEvidenceRecheckPhase()
register_phase(COUNTER_EVIDENCE_RECHECK_PHASE)
