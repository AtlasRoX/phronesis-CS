"""
Disease-level target evidence from the Open Targets Platform: the targets most strongly associated with a disease
(overall and per evidence type, with tractability), and the drugs and clinical candidates studied for it with
their trial outcomes.

This answers "what are the targets for disease X" questions, where no gene is named. Association evidence types:
- genetic_association / genetic_literature: human genetics (GWAS, rare variants, curated gene-disease reports);
- clinical: a drug acting on the target reached clinical trials for the disease (not proof of efficacy);
- animal_model, affected_pathway, somatic_mutation, rna_expression: supporting, weaker evidence;
- literature: text-mined co-mentions, the weakest.

Targets are presented in tiers rather than by association score, which orders evidence of involvement, not value
as a drug target: validated (targets of drugs approved for the disease), clinically tested (with trial outcomes),
genetic risk mechanisms (strong human genetics, often loss of function: mechanisms to restore, not to inhibit),
and other associated genes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

import httpx

from ..utils.http_limits import TTLCache

logger = logging.getLogger(__name__)

OPENTARGETS_GRAPHQL = "https://api.platform.opentargets.org/api/v4/graphql"
TIMEOUT_S = 12.0  # per attempt; two attempts fit the 30 s registry tool timeout
USER_AGENT = "Phronesis-DiseaseTargets/1.0"
CACHE_TTL_S = 3600.0
_CACHE = TTLCache(maxsize=128, ttl_s=CACHE_TTL_S)


class OpenTargetsError(Exception):
    """An Open Targets failure with a typed kind (timeout|upstream|query); the message never carries raw upstream text."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def clear_cache() -> None:
    _CACHE.clear()

# Most advanced first. Open Targets reports stages as strings ("APPROVAL", "PHASE_2_3", "EARLY_PHASE_1", ...).
STAGE_RANK = {
    "APPROVAL": 0, "PHASE_4": 0, "PREAPPROVAL": 1, "PHASE_3": 1, "PHASE_2_3": 2, "PHASE_2": 3, "PHASE_1_2": 4, "PHASE_1": 5,
    "EARLY_PHASE_1": 6, "PRECLINICAL": 7, "UNKNOWN": 8,
}
STAGE_LABEL = {
    "APPROVAL": "approved (some indication)", "PHASE_4": "approved (some indication)",
    "PREAPPROVAL": "filed for approval", "PHASE_3": "phase 3",
    "PHASE_2_3": "phase 2/3", "PHASE_2": "phase 2", "PHASE_1_2": "phase 1/2", "PHASE_1": "phase 1",
    "EARLY_PHASE_1": "early phase 1", "PRECLINICAL": "preclinical",
}
# Order of the clinically tested tier: filed for approval first; a drug approved for another disease ranks after
# phase 3 here, since that approval says nothing about this disease.
_TESTED_ORDER = {"PREAPPROVAL": 0, "PHASE_3": 1, "APPROVAL": 2, "PHASE_4": 2, "PHASE_2_3": 3, "PHASE_2": 4}
_ONGOING = {"RECRUITING", "ACTIVE_NOT_RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION"}
# KG-009: stop-reason text is classified by polarity. A failure needs negative context ("did not meet", "futility",
# "lack of efficacy", "benefit-risk ... no longer supports"); a bare "primary endpoint", "benefit-risk" or
# "mortality" says nothing about direction ("primary endpoint met", "reduction in mortality" are successes).
_FAILURE_TEXT = re.compile(
    r"futil|lack of (?:efficacy|benefit|effect|activity|response)|insufficient (?:efficacy|benefit|effect|activity)|"
    r"\b(?:not|never|unlikely to|failed to|fail to|did not|does not|could not|cannot|would not)\s+(?:\w+\s+){0,3}?"
    r"(?:meet|met|achieve|achieved|reach|reached|demonstrate|demonstrated|show|shown|showed)\b|"
    r"\b(?:primary )?(?:end ?points?|objectives?) (?:was |were )?not (?:met|achieved|reached)|"
    r"\bno (?:significant |clinical(?:ly)? |meaningful |statistically significant )?"
    r"(?:benefit|efficacy|effect|improvement|difference|activity|response)\b|"
    r"\bineffective|\binefficacy|\bnegative (?:results?|outcomes?|interim|data|efficacy|findings?|study|trial)|"
    r"benefit[- ]risk (?:\w+\s+){0,4}?(?:no longer|not|unfavou?rable|negative|insufficient)|"
    r"(?:unfavou?rable|negative|insufficient) benefit[- ]risk",
    re.I,
)
_POSITIVE_TEXT = re.compile(
    r"\b(?:primary )?(?:end ?points?|objectives?) (?:was |were |has been |have been )?(?:met|achieved|reached)\b|"
    r"\b(?:met|achieved|reached) (?:its|the|their|all) (?:\w+\s+){0,2}(?:end ?points?|objectives?)|"
    r"overwhelming (?:efficacy|benefit|evidence)|(?:early|clear|demonstrated|proven|established|significant) "
    r"(?:evidence of )?(?:efficacy|benefit|superiority)|stopped early for (?:efficacy|benefit|success)|"
    r"\bpositive (?:benefit[- ]risk|results?|outcomes?|interim|efficacy|data|findings?)|"
    r"efficacy (?:was |has been )?(?:demonstrated|established|shown|proven)|"
    r"(?:reduction|reduced|decrease|decreased|lower|improvement|improved) (?:\w+\s+){0,2}?(?:in )?(?:mortality|survival|deaths?)",
    re.I,
)
_SAFETY_TEXT = re.compile(
    r"\bsafety\b|adverse|toxicit|\bharm|side[- ]effects?|\bfatal|"
    r"(?:excess|increased?|higher|imbalance)\s+(?:\w+\s+){0,3}?(?:mortality|deaths?)|"
    r"(?:mortality|deaths?)\s+(?:\w+\s+){0,3}?(?:excess|increased|higher)",
    re.I,
)
# "not due to safety", "no safety concerns": a negated safety mention is not a safety stop.
_NEGATED_SAFETY = re.compile(
    r"\b(?:not|no|without|unrelated to|non)[\s-]+(?:\w+\s+){0,3}?(?:safety|adverse|toxicit\w*|side[- ]effects?)"
    r"(?:\s+(?:concerns?|issues?|reasons?|signals?|findings?|events?))?",
    re.I,
)
# Registry stop-reason categories (ClinicalTrials.gov via Open Targets) that state the outcome.
_POSITIVE_CATEGORIES = frozenset({"Success", "Endpoint_Met", "Positive"})
_NEGATIVE_CATEGORIES = frozenset({"Negative"})
_SAFETY_CATEGORIES = frozenset({"Safety_Sideeffects"})


def stop_text_polarity(why: str) -> Optional[str]:
    """'negative' | 'positive' | 'safety' | None for a free-text trial stop reason (KG-009). Negative context wins
    over a positive phrase ("primary endpoint not met although benefit was seen"), a positive phrase over a safety
    word ("stopped for efficacy; no safety concerns" is a success)."""
    text = str(why or "")
    if not text.strip():
        return None
    if _FAILURE_TEXT.search(text):
        return "negative"
    if _POSITIVE_TEXT.search(text):
        return "positive"
    if _SAFETY_TEXT.search(_NEGATED_SAFETY.sub(" ", text)):
        return "safety"
    return None


def _report_polarity(report: Dict[str, Any]) -> Optional[str]:
    """One trial record's outcome polarity: the registry's own stop-reason category when it states an outcome,
    otherwise the stop text (registries file some failures under business reasons)."""
    cats = set(report.get("trialStopReasonCategories") or [])
    if cats & _POSITIVE_CATEGORIES:
        return "positive"
    if cats & _NEGATIVE_CATEGORIES:
        return "negative"
    if cats & _SAFETY_CATEGORIES:
        return "safety"
    return stop_text_polarity(str(report.get("trialWhyStopped") or ""))


def _trial_phase(value: Any) -> Optional[float]:
    """Highest phase number in a trial record's phase ("PHASE2/PHASE3", "Phase 2", "PHASE_3"), None if unreadable."""
    text = str(value or "").upper().replace(" ", "").replace("_", "")
    nums = [float(n) for n in re.findall(r"PHASE(\d)", text)]
    return max(nums) if nums else None

_QUERY = """
query DiseaseTargets($efo: String!, $size: Int!) {
  disease(efoId: $efo) {
    id
    name
    associatedTargets(page: {index: 0, size: $size}) {
      count
      rows {
        score
        datatypeScores { id score }
        target {
          id
          approvedSymbol
          approvedName
          tractability { label modality value }
        }
      }
    }
    drugAndClinicalCandidates {
      count
      rows {
        maxClinicalStage
        drug {
          id
          name
          drugType
          mechanismsOfAction { rows { mechanismOfAction actionType targets { id approvedSymbol } } }
        }
        clinicalReports { trialPhase trialOverallStatus trialWhyStopped trialStopReasonCategories }
      }
    }
  }
}
"""

_MODALITY = {"SM": "small molecule", "AB": "antibody", "PR": "PROTAC", "OC": "other modality"}
# Tractability buckets that say a modality is feasible.
_STRONG_TRACTABILITY = ("Approved Drug", "Advanced Clinical", "Phase 1 Clinical", "Structure with Ligand",
                        "High-Quality Ligand", "High-Quality Pocket", "UniProt loc high conf", "GO CC high conf")


def _tractability(rows: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = defaultdict(list)
    for r in rows or []:
        if r.get("value") and r.get("label") in _STRONG_TRACTABILITY:
            out[_MODALITY.get(str(r.get("modality")), str(r.get("modality")))].append(str(r["label"]))
    return {k: v[:3] for k, v in out.items()}


APPROVAL_BASIS = ("a ClinicalTrials.gov record for this disease in status APPROVED_FOR_MARKETING (an expanded-access "
                  "status), not a regulatory label; confirm the indication with the FDA/EMA label")
POSITIVE_STOP = "positive (stopped early: efficacy or endpoint met)"


def trial_outcome(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What the disease's trials of a drug show: approved for it, failed (efficacy or safety), stopped early for
    benefit, ongoing, or completed. A drug can carry an 'approval' stage from another indication; only an
    APPROVED_FOR_MARKETING trial record for this disease counts as approval here, and that record is an
    expanded-access status, so the basis is reported with it (KG-010). Stop reasons are read by polarity, preferring
    the registry's stop-reason category when it states an outcome (KG-009)."""
    statuses = {str(r.get("trialOverallStatus") or "") for r in reports}
    polarity = [(_report_polarity(r), r) for r in reports]
    found = {p for p, _ in polarity if p}

    def reasons(kinds: set) -> List[str]:
        return [str(r.get("trialWhyStopped")).strip() for p, r in polarity if p in kinds and r.get("trialWhyStopped")]

    stop: List[str] = reasons({"negative", "safety"})
    basis = None
    if "APPROVED_FOR_MARKETING" in statuses:
        outcome, basis = "approved for this disease", APPROVAL_BASIS
    elif "negative" in found:
        outcome = "failed (efficacy or futility)"
    elif "safety" in found:
        outcome = "stopped for safety"
    elif "positive" in found:
        outcome, stop = POSITIVE_STOP, reasons({"positive"})
    elif statuses & _ONGOING:
        outcome = "trials ongoing"
    elif "COMPLETED" in statuses:
        outcome = "trials completed, outcome not recorded here"
    else:
        outcome = "outcome unknown"
    phases = [p for p in (_trial_phase(r.get("trialPhase")) for r in reports) if p is not None]
    return {"outcome": outcome, "stop_reason": stop[0][:140] if stop else None, "n_trials": len(reports),
            "max_disease_trial_phase": max(phases) if phases else None, "approval_basis": basis}


def evidence_class(evidence: Dict[str, float], drugs: List[Dict[str, Any]]) -> str:
    """How far a target is validated for this disease (strongest first):
    clinically_validated - a drug acting on it is approved for this disease;
    human_genetics - strong human genetic evidence (genetic_association or genetic_literature >= 0.5);
    clinically_tested - a drug acting on it is in Open Targets' candidates for the disease at phase 2 or later (its
        most advanced stage, which for an approved drug may come from another indication: see approval_scope);
    exploratory - association from animal models, pathways, expression or literature only."""
    if any(d.get("outcome") == "approved for this disease" for d in drugs):
        return "clinically_validated"
    if max(evidence.get("genetic_association", 0), evidence.get("genetic_literature", 0)) >= 0.5:
        return "human_genetics"
    if any(STAGE_RANK.get(d["stage"], 9) <= 3 for d in drugs):
        return "clinically_tested"
    return "exploratory"


# WHO INN stems that name a mechanism class, for new drugs Open Targets has not annotated yet (nerandomilast).
# Deliberately short: only stems whose class is unambiguous. A drug with an unknown stem stays unannotated.
_INN_STEM_CLASS = {
    "milast": "phosphodiesterase-4 (PDE4) inhibitor",
    "tegrast": "integrin antagonist",
    "lisib": "phosphoinositide 3-kinase (PI3K) inhibitor",
    "ciclib": "cyclin-dependent kinase inhibitor",
    "parib": "poly(ADP-ribose) polymerase (PARP) inhibitor",
}


def _stem_class(name: str) -> Optional[str]:
    low = (name or "").lower()
    for stem, cls in _INN_STEM_CLASS.items():
        if low.endswith(stem):
            return f"{cls} (class inferred from the INN stem -{stem}; target not annotated)"
    return None


# Salt and hydrate words: "IMATINIB MESYLATE" and "IMATINIB" are one drug, "TRASTUZUMAB DERUXTECAN" is not
# "TRASTUZUMAB".
_SALT_WORDS = frozenset({
    "mesylate", "mesilate", "hydrochloride", "dihydrochloride", "hydrobromide", "sodium", "disodium", "potassium",
    "calcium", "magnesium", "maleate", "tartrate", "bitartrate", "sulfate", "sulphate", "phosphate", "citrate",
    "acetate", "besylate", "besilate", "tosylate", "fumarate", "hemifumarate", "succinate", "bromide", "chloride",
    "lactate", "malate", "mononitrate", "nitrate", "monohydrate", "dihydrate", "hemihydrate", "trihydrate",
    "anhydrous", "hyclate", "dimesylate", "camsylate", "edisylate", "esylate", "hydrate",
})


def drug_identity(name: str) -> str:
    """The drug name without salt or hydrate words, for collapsing salt forms of one drug."""
    words = [w for w in re.split(r"\s+", str(name or "").strip().lower()) if w]
    kept = [w for w in words if w not in _SALT_WORDS]
    return " ".join(kept or words)


def _candidates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows or []:
        drug = row.get("drug") or {}
        stage = str(row.get("maxClinicalStage") or "UNKNOWN")
        moas = ((drug.get("mechanismsOfAction") or {}).get("rows") or [])
        mechanism = "; ".join(dict.fromkeys(str(m.get("mechanismOfAction")) for m in moas if m.get("mechanismOfAction")))
        symbols = list(dict.fromkeys(t["approvedSymbol"] for m in moas for t in m.get("targets") or [] if t.get("approvedSymbol")))
        if not mechanism:
            mechanism = _stem_class(str(drug.get("name") or "")) or ""
        out.append({
            "chembl_id": drug.get("id"), "name": drug.get("name"), "type": drug.get("drugType"),
            "stage": stage, "stage_label": STAGE_LABEL.get(stage, stage.lower()), "mechanism": mechanism or None,
            "targets": symbols[:8], "all_targets": symbols, "n_targets": len(symbols),
            **trial_outcome(row.get("clinicalReports") or []),
        })
        c = out[-1]
        if stage in ("APPROVAL", "PHASE_4") and c["outcome"] != "approved for this disease":
            # KG-010: the stage may come from another indication; say so instead of implying approval or a phase
            # 2+ programme for this disease.
            phase = c.get("max_disease_trial_phase")
            c["approval_scope"] = ("approved for some indication; no record here confirms approval for this disease"
                                   + ("" if phase is not None and phase >= 2 else
                                      "; no phase 2 or later trial for this disease is recorded here"))
    # One entry per drug: by ChEMBL id, and salt forms of one drug by name without the salt word. Approved first,
    # then by stage.
    seen_ids, seen_names, unique = set(), set(), []
    for c in sorted(out, key=lambda c: (c["outcome"] != "approved for this disease", STAGE_RANK.get(c["stage"], 9), str(c["name"]))):
        cid = str(c.get("chembl_id") or "").upper()
        name = drug_identity(str(c["name"]))
        if (cid and cid in seen_ids) or (name and name in seen_names):
            continue
        if cid:
            seen_ids.add(cid)
        if name:
            seen_names.add(name)
        unique.append(c)
    return unique


def summarize(raw: Dict[str, Any], *, target_limit: int, candidate_limit: int) -> Dict[str, Any]:
    disease = raw or {}
    candidates = _candidates((disease.get("drugAndClinicalCandidates") or {}).get("rows") or [])
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        if c["n_targets"] <= 12:  # a drug hitting dozens of proteins (complex I, ...) says little about any one
            for s in c["all_targets"]:
                by_symbol[s].append(c)
    targets = []
    for row in ((disease.get("associatedTargets") or {}).get("rows") or [])[:target_limit]:
        t = row.get("target") or {}
        symbol = str(t.get("approvedSymbol") or "")
        drugs = sorted(by_symbol.get(symbol, []), key=lambda c: STAGE_RANK.get(c["stage"], 9))
        evidence = {d["id"]: round(float(d["score"]), 3) for d in row.get("datatypeScores") or [] if d.get("score")}
        targets.append({
            "symbol": symbol,
            "ensembl_id": t.get("id"),
            "name": t.get("approvedName"),
            "overall_score": round(float(row.get("score") or 0), 3),
            "evidence": evidence,
            "evidence_class": evidence_class(evidence, drugs),
            "tractability": _tractability(t.get("tractability") or []),
            "drugs_for_disease": [{"name": c["name"], "chembl_id": c["chembl_id"], "stage": c["stage_label"], "outcome": c["outcome"]}
                                  for c in list({c["name"]: c for c in drugs}.values())[:4]],
        })
    approved = [c for c in candidates if c["outcome"] == "approved for this disease"]
    # Phase 2 or later, not approved here: promising (not failed) and most advanced first, failures after them as
    # cautionary evidence. Unannotated drugs stay in: the newest candidates often have no curated mechanism yet.
    pool = [c for c in candidates if c["outcome"] != "approved for this disease" and STAGE_RANK.get(c["stage"], 9) <= 3
            and c["n_targets"] <= 12]
    failed = [c for c in pool if c["outcome"].startswith(("failed", "stopped"))]
    promising = sorted((c for c in pool if c not in failed),
                       key=lambda c: (_TESTED_ORDER.get(c["stage"], 9), c["outcome"] != "trials ongoing", str(c["name"])))
    tested = promising[:candidate_limit] + sorted(failed, key=lambda c: _TESTED_ORDER.get(c["stage"], 9))[:12]
    return {
        "disease": {"id": disease.get("id"), "name": disease.get("name")},
        "n_associated_targets": (disease.get("associatedTargets") or {}).get("count"),
        "tiers": {
            "validated": [{"drug": c["name"], "chembl_id": c["chembl_id"], "mechanism": c["mechanism"], "targets": c["targets"],
                           "approval_basis": c.get("approval_basis")}
                          for c in approved],
            "clinically_tested": [{"drug": c["name"], "chembl_id": c["chembl_id"], "stage": c["stage_label"],
                                   "mechanism": c["mechanism"], "targets": c["targets"], "outcome": c["outcome"],
                                   "stop_reason": c["stop_reason"],
                                   "disease_trial_phase": c.get("max_disease_trial_phase"),
                                   "approval_scope": c.get("approval_scope")} for c in tested],
            "genetic_risk_mechanisms": [t["symbol"] for t in targets if t["evidence_class"] == "human_genetics"],
        },
        "targets": targets,
        "clinical_candidates": candidates[:candidate_limit],
        "n_clinical_candidates": len(candidates),
        "candidate_limit": candidate_limit,
        "source": "Open Targets Platform",
    }


async def _fetch(efo_id: str, size: int, transport: Optional[httpx.AsyncBaseTransport]) -> Dict[str, Any]:
    """The GraphQL payload, with one retry on 5xx, timeout or connection error."""
    last = ""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport, headers={"User-Agent": USER_AGENT}) as client:
                resp = await client.post(OPENTARGETS_GRAPHQL, json={"query": _QUERY, "variables": {"efo": efo_id, "size": size}})
        except httpx.TimeoutException:
            last = "timeout"
        except httpx.TransportError as exc:
            last = f"connection error ({type(exc).__name__})"
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise OpenTargetsError("upstream", "Open Targets returned a non-JSON body") from exc
            if resp.status_code < 500:
                raise OpenTargetsError("query", f"Open Targets rejected the query (HTTP {resp.status_code})")
            last = f"HTTP {resp.status_code}"
        if attempt == 0:
            await asyncio.sleep(0.5)
    kind = "timeout" if last == "timeout" else "upstream"
    raise OpenTargetsError(kind, f"Open Targets unavailable after one retry ({last})")


async def disease_targets(
    efo_id: str, target_limit: int = 15, candidate_limit: int = 40, transport: Optional[httpx.AsyncBaseTransport] = None
) -> Dict[str, Any]:
    """Raises OpenTargetsError on transport or GraphQL errors; returns {"disease": None} when Open Targets has no
    such disease. Payloads are cached for an hour per (disease, page size) when talking to the real API."""
    size = max(target_limit, 5)
    key = (efo_id, size)
    payload = _CACHE.get(key) if transport is None else None
    if payload is None:
        payload = await _fetch(efo_id, size, transport)
        if payload.get("errors"):
            logger.warning("Open Targets GraphQL errors for %s: %s", efo_id, str(payload["errors"])[:300])
            raise OpenTargetsError("query", "Open Targets returned GraphQL errors for this disease query")
        if transport is None:
            _CACHE.set(key, payload)
    raw: Optional[Dict[str, Any]] = (payload.get("data") or {}).get("disease")
    if not raw:
        return {"disease": None, "targets": [], "clinical_candidates": [], "tiers": {}, "source": "Open Targets Platform"}
    return summarize(raw, target_limit=target_limit, candidate_limit=candidate_limit)


__all__ = [
    "OPENTARGETS_GRAPHQL", "STAGE_LABEL", "STAGE_RANK", "OpenTargetsError", "clear_cache", "disease_targets",
    "drug_identity", "evidence_class", "summarize", "trial_outcome",
]
