"""
Deterministic scoring: every function takes what a run produced and returns checks and metrics, with no I/O.
LLM-based grading (benchmark labels, rubric judge) lives in grading.py and only adds to these.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .schema import AnswerExpect, Check, ExecutionExpect, Fact, PlanExpect, RetrievalExpect, SimulationExpect

# ---------------------------------------------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------------------------------------------


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def plan_checks(expect: PlanExpect, observed: Dict[str, Any], *, live: bool) -> List[Check]:
    """Scope and question type are checked always; entity and routing checks need the live resolver unless the
    case says they hold offline (plan.offline)."""
    checks: List[Check] = []
    if expect.scope is not None:
        checks.append(Check(name="plan.scope", passed=observed.get("scope") == expect.scope,
                            detail=f"expected {expect.scope}, got {observed.get('scope')}"))
    if expect.simulation is not None:
        checks += simulation_checks(expect.simulation, observed)
    if expect.question_type is not None and observed.get("scope") == "research":
        checks.append(Check(name="plan.question_type", passed=observed.get("question_type") == expect.question_type,
                            detail=f"expected {expect.question_type}, got {observed.get('question_type')}"))
    entity_level = live or expect.offline
    if not entity_level:
        return checks
    names = {_norm(n) for n in observed.get("entity_names", [])}
    if expect.primary_target is not None:
        want = expect.primary_target or None
        got = observed.get("primary_target") or None
        checks.append(Check(name="plan.primary_target", passed=_norm(want or "") == _norm(got or ""),
                            detail=f"expected {want}, got {got}"))
    for ent in expect.entities:
        checks.append(Check(name="plan.entity", passed=_norm(ent) in names, detail=f"{ent} {'found' if _norm(ent) in names else 'missing'}"))
    for ent in expect.not_entities:
        checks.append(Check(name="plan.not_entity", passed=_norm(ent) not in names,
                            detail=f"{ent} {'wrongly recognized' if _norm(ent) in names else 'absent'}"))
    ran = set(observed.get("sources_run", []))
    for src in expect.run:
        checks.append(Check(name="plan.runs", passed=src in ran, detail=f"{src} {'runs' if src in ran else 'does not run'}"))
    for src in expect.skip:
        checks.append(Check(name="plan.skips", passed=src not in ran, detail=f"{src} {'wrongly runs' if src in ran else 'skipped'}"))
    return checks


def _close(a: Any, b: Optional[float], tol: float = 1e-9) -> bool:
    try:
        return b is not None and abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b)))
    except (TypeError, ValueError):
        return False


def simulation_checks(expect: SimulationExpect, observed: Dict[str, Any]) -> List[Check]:
    """The parsed simulation plan: action, scan arguments, overrides kept, durations, and validation errors."""
    plan = observed.get("simulation_plan") or None
    constraints = observed.get("simulation_constraints") or {}
    checks: List[Check] = []
    if expect.forbidden is not None:
        got = bool(constraints.get("simulation_forbidden"))
        checks.append(Check(name="plan.sim_forbidden", passed=got is expect.forbidden, detail=f"expected {expect.forbidden}, got {got}"))
    if expect.action is not None:
        got = (plan or {}).get("action") or ""
        checks.append(Check(name="plan.sim_action", passed=got == expect.action, detail=f"expected {expect.action or 'none'}, got {got or 'none'}"))
    if plan is None:
        wanted = [n for n, v in (("duration_seconds", expect.duration_seconds), ("scan_points", expect.scan_points),
                                 ("validation_error", expect.validation_error)) if v is not None] + (["overrides"] if expect.overrides else [])
        if wanted:
            checks.append(Check(name="plan.sim_plan", passed=False, detail=f"no simulation plan, expected {', '.join(wanted)}"))
        return checks
    for field in ("duration_seconds", "scan_start", "scan_end"):
        want = getattr(expect, field)
        if want is not None:
            checks.append(Check(name=f"plan.sim_{field}", passed=_close(plan.get(field), want), detail=f"expected {want}, got {plan.get(field)}"))
    if expect.scan_points is not None:
        checks.append(Check(name="plan.sim_scan_points", passed=plan.get("scan_points") == expect.scan_points,
                            detail=f"expected {expect.scan_points}, got {plan.get('scan_points')}"))
    if expect.scan_parameter is not None:
        checks.append(Check(name="plan.sim_scan_parameter", passed=plan.get("scan_parameter") == expect.scan_parameter,
                            detail=f"expected {expect.scan_parameter}, got {plan.get('scan_parameter')}"))
    kept = set((plan.get("parameter_overrides") or {}).keys())
    for name in expect.overrides:
        checks.append(Check(name="plan.sim_override_kept", passed=name in kept, detail=f"{name} {'kept' if name in kept else 'dropped'}"))
    codes = [str((e or {}).get("code")) for e in plan.get("validation_errors") or [] if isinstance(e, dict)]
    if expect.validation_error is not None:
        checks.append(Check(name="plan.sim_validation_error", passed=expect.validation_error in codes,
                            detail=f"expected {expect.validation_error}, got {codes or 'none'}"))
    if expect.valid is not None:
        checks.append(Check(name="plan.sim_valid", passed=(not codes) is expect.valid, detail=f"validation errors: {codes or 'none'}"))
    return checks


# ---------------------------------------------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------------------------------------------


def _ranked_unique(items: Iterable[Any]) -> List[str]:
    return list(dict.fromkeys(str(i).strip().lower() for i in items if i))


def retrieval_scores(
    expect: RetrievalExpect, pmids: Sequence[Any], dois: Sequence[Any] = (), titles: Sequence[Any] = ()
) -> Tuple[List[Check], Dict[str, Any]]:
    """hit@k (a gold PMID/DOI, or a title matching a gold pattern, in the top k), recall@k over the listed gold
    papers, and reciprocal rank of the first hit. Rank order is kept; the three lists are parallel when given."""
    ranked_p, ranked_d = _ranked_unique(pmids), _ranked_unique(dois)
    ranked_t = list(dict.fromkeys(str(t) for t in titles if t))
    gold_p = {str(p).strip().lower() for p in expect.gold_pmids}
    gold_d = {str(d).strip().lower() for d in expect.gold_dois}
    patterns = [re.compile(p, re.I) for p in expect.gold_title_patterns]
    n_gold = len(gold_p) + len(gold_d)
    if not n_gold and not patterns:
        return [], {"retrieved": len(ranked_p) + len(ranked_d)}
    k = expect.k
    top_p, top_d, top_t = ranked_p[:k], ranked_d[:k], ranked_t[:k]
    found = (gold_p & set(top_p)) | (gold_d & set(top_d))
    title_hits = [(i + 1, t) for i, t in enumerate(top_t) if any(p.search(t) for p in patterns)]
    ranks = ([i + 1 for i, p in enumerate(top_p) if p in gold_p] + [i + 1 for i, d in enumerate(top_d) if d in gold_d]
             + [r for r, _ in title_hits])
    first = min(ranks) if ranks else None
    hit = bool(found or title_hits)
    metrics = {
        "hit": hit,
        "recall": round(len(found) / n_gold, 3) if n_gold else None,
        "rr": round(1 / first, 3) if first else 0.0,
        "first_gold_rank": first,
        "gold_found": sorted(found),
        "title_matches": [t[:100] for _, t in title_hits][:3],
        "retrieved": len(ranked_p) + len(ranked_d),
        "k": k,
    }
    if found:
        detail = f"found {sorted(found)} at rank {first}"
    elif title_hits:
        detail = f"title match at rank {title_hits[0][0]}: {title_hits[0][1][:90]}"
    else:
        detail = f"none of {sorted(gold_p | gold_d)} or the title patterns in top {k} of {len(ranked_p)} PMIDs"
    return [Check(name="retrieval.gold_hit", passed=hit, required=expect.required, detail=detail)], metrics


# ---------------------------------------------------------------------------------------------------------------
# Answer
# ---------------------------------------------------------------------------------------------------------------

_ABSTAIN_RE = re.compile(
    # "No <anything within the sentence> was/were retrieved|found|available|reported|identified"
    r"(\bno\b[^.;]{0,160}?\b(was|were|is|are|has been|have been)\s+(found|retrieved|available|identified|reported|located)\b"
    r"|\bno (peer[- ]reviewed |direct |relevant |published |clinical |reliable |such )?(evidence|data|information|trials?|"
    r"publications?|studies|records?)\b[^.;]{0,60}\b(exists?|available|found|retrieved)"
    r"|\bnot (been )?(retrieved|found|identified|available|supported|be verified|be confirmed)"
    r"|could not (find|identify|locate|be (found|verified|confirmed))|couldn.t find"
    r"|cannot (confirm|verify|be (confirmed|verified|stated|determined|provided|answered|given|established|substantiated)|support)"
    r"|does not contain (any )?(information|data|evidence|mention)|\bno basis\b"
    r"|insufficient (evidence|information|data)|unable to (find|identify|confirm|verify|locate)"
    r"|does not (exist|correspond|appear)|no such|not aware of|is hypothetical|appears to be hypothetical|not a recogni[sz]ed)",
    re.I,
)
_PMID_RE = re.compile(r"PMID:?\s*(\d{1,9})", re.I)
# Models write "PD‑L1" with a non-breaking hyphen and "100 %" with a narrow no-break space; patterns use ASCII.
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−﹣－"), "-")
_SPACES = dict.fromkeys(map(ord, "      　"), " ")


def normalize(text: str) -> str:
    """Unicode dashes and spaces to ASCII, so regex expectations match what a model writes."""
    return (text or "").translate(_DASHES).translate(_SPACES)


def abstained(text: str) -> bool:
    """The answer says the asked-for evidence or entity does not exist or was not found (anywhere in the text)."""
    return bool(_ABSTAIN_RE.search(normalize(text)))


def opens_with_refusal(text: str) -> bool:
    """The opening sentence says nothing was found: the answer leads with a refusal rather than an answer."""
    # Not utils.text.sentence_spans on purpose: a newline is no break here, so a leading heading or label line
    # ("## Answer\nNo trials were found.") stays part of the opening sentence instead of becoming the whole head.
    head = re.split(r"(?<=[.!?])\s+", normalize(text).strip())[0]
    return abstained(head)


def cited_pmids(text: str) -> List[str]:
    return list(dict.fromkeys(_PMID_RE.findall(text or "")))


def answer_checks(expect: AnswerExpect, observed: Dict[str, Any], gold_pmids: Sequence[str] = ()) -> Tuple[List[Check], Dict[str, Any]]:
    text = normalize(observed.get("response") or "")
    agents = set(observed.get("agents") or [])
    checks: List[Check] = []
    for pat in expect.must_mention:
        hit = re.search(pat, text, re.I)
        checks.append(Check(name="answer.mentions", passed=bool(hit), detail=f"/{pat}/ {'found' if hit else 'missing'}"))
    for pat in expect.must_not_mention:
        hit = re.search(pat, text, re.I)
        checks.append(Check(name="answer.avoids", passed=not hit, detail=f"/{pat}/ {'present: ' + hit.group(0) if hit else 'absent'}"))
    if expect.abstain is True:
        checks.append(Check(name="answer.abstains", passed=abstained(text), detail="says the evidence is missing" if abstained(text) else "does not abstain"))
    elif expect.abstain is False:
        refused = opens_with_refusal(text)
        # Informational: a good answer may open by noting a gap and still answer from labelled background.
        checks.append(Check(name="answer.answers", passed=not refused, required=False,
                            detail="opens with a refusal" if refused else "answers directly"))
    for a in expect.agents_include:
        checks.append(Check(name="answer.agent_ran", passed=a in agents, detail=f"{a} {'ran' if a in agents else 'did not run'}"))
    for a in expect.agents_exclude:
        checks.append(Check(name="answer.agent_skipped", passed=a not in agents, detail=f"{a} {'wrongly ran' if a in agents else 'not run'}"))
    if expect.framework is not None:
        checks.append(Check(name="answer.framework", passed=observed.get("framework") == expect.framework,
                            detail=f"expected {expect.framework}, got {observed.get('framework')}"))
    if expect.verified is not None:
        ok = observed.get("verified") is expect.verified
        checks.append(Check(name="answer.verified", passed=ok,
                            detail=f"verifier passed={observed.get('verified')}; defects: {'; '.join(observed.get('defects', [])[:3])}"))
    cites = cited_pmids(text)
    resolved = set(observed.get("citation_keys") or [])
    unresolved = [p for p in cites if f"PMID:{p}" not in resolved]
    if expect.min_citations:
        checks.append(Check(name="answer.citations", passed=len(cites) >= expect.min_citations, detail=f"{len(cites)} PMIDs cited"))
    gold = {str(p) for p in gold_pmids}
    if expect.cite_gold and gold:
        hit = sorted(gold & set(cites))
        checks.append(Check(name="answer.cites_gold", passed=bool(hit), detail=f"cited gold {hit}" if hit else "no gold PMID cited"))
    if expect.execution is not None:
        checks += execution_checks(expect.execution, observed)
    if expect.max_latency_s is not None and observed.get("latency_s") is not None:
        checks.append(Check(name="answer.latency", passed=observed["latency_s"] <= expect.max_latency_s,
                            detail=f"{observed['latency_s']:.1f}s (limit {expect.max_latency_s}s)"))
    run = run_metrics(observed)
    for name, limit, key in (("answer.budget_tokens", expect.max_tokens, "tokens_est"),
                             ("answer.budget_llm_calls", expect.max_llm_calls, "llm_calls"),
                             ("answer.budget_tool_calls", expect.max_tool_calls, "tool_calls")):
        if limit is None:
            continue
        got = run.get(key)
        if got is None:
            checks.append(Check(name=name, passed=True, required=False, detail=f"{key} not recorded for this run"))
        else:
            checks.append(Check(name=name, passed=got <= limit, detail=f"{key} {got} (limit {limit})"))
    if expect.min_faithfulness is not None:
        faith = run.get("faithfulness")
        checks.append(Check(name="answer.faithfulness", passed=faith is not None and faith >= expect.min_faithfulness,
                            detail=f"faithfulness {faith} (minimum {expect.min_faithfulness})"))
    f_checks, f_metrics = fact_checks(expect.facts, observed.get("response") or "", min_recall=expect.min_fact_recall)
    checks += f_checks
    checks += gate_checks(observed, query=str(observed.get("query") or ""), skip=expect.skip_gates)
    metrics = {
        "cited_pmids": len(cites),
        "unresolved_citations": len(unresolved),
        "gold_cited": bool(gold & set(cites)) if gold else None,
        # An answer abstains when it leads with "not found / no evidence" (LAB-Bench coverage sense); a full answer
        # that honestly notes one missing piece ("G12C-specific affinities were not retrieved") has not abstained.
        "abstained": opens_with_refusal(text) or (len(text.split()) < 80 and abstained(text)),
        "verified": observed.get("verified"),
        "length_words": len(text.split()),
        **run,
        **f_metrics,
    }
    return checks, metrics


_SIM_TOOLS = ("vivarix_simulate_time_course", "vivarix_compute_steady_state", "vivarix_parameter_scan")


def execution_checks(expect: ExecutionExpect, observed: Dict[str, Any]) -> List[Check]:
    """The execution contract: what ran with which arguments, the task status, and the evidence ids. Wording that
    admits a failure does not pass these; the requested operation must actually have run (or been rejected)."""
    task = observed.get("task_status") or {}
    calls = [c for c in observed.get("tool_calls") or [] if isinstance(c, dict)]
    checks: List[Check] = []
    for field in ("task_status", "requested_operation", "executed_operation"):
        want = getattr(expect, field)
        if want is not None:
            got = task.get(field) or ""
            checks.append(Check(name=f"exec.{field}", passed=got == want, detail=f"expected {want or 'none'}, got {got or 'none'}"))
    if expect.request_match is not None:
        got = task.get("request_match")
        checks.append(Check(name="exec.request_match", passed=got is expect.request_match, detail=f"expected {expect.request_match}, got {got}"))
    called = {c.get("tool_name") for c in calls}
    for tool in expect.tools_called:
        checks.append(Check(name="exec.tool_called", passed=tool in called, detail=f"{tool} {'called' if tool in called else 'not called'}"))
    for tool in expect.tools_not_succeeded:
        ok = [c for c in calls if c.get("tool_name") == tool and c.get("success")]
        checks.append(Check(name="exec.no_substitute", passed=not ok,
                            detail=f"{tool} {'succeeded ' + str(len(ok)) + 'x (substituted result)' if ok else 'did not succeed'}"))
    reported = {str(c.get("error_type")) for c in calls if c.get("error_type")}
    reason = f"{task.get('reason') or ''} {task.get('error_type') or ''}"
    for code in expect.error_types:
        hit = code in reported or code in reason
        checks.append(Check(name="exec.error_type", passed=hit, detail=f"{code} {'reported' if hit else 'not reported'} ({sorted(reported) or 'none'})"))
    scans = [c for c in calls if c.get("tool_name") == "vivarix_parameter_scan"]
    if expect.scan_points is not None or expect.scan_bounds or expect.scan_duration_s is not None:
        if not scans:
            checks.append(Check(name="exec.scan_ran", passed=False, detail="vivarix_parameter_scan was not called"))
        for c in scans[:1]:
            inputs = c.get("inputs") or {}
            if expect.scan_points is not None:
                checks.append(Check(name="exec.scan_points", passed=inputs.get("num_steps") == expect.scan_points,
                                    detail=f"expected {expect.scan_points}, got {inputs.get('num_steps')}"))
            if expect.scan_bounds:
                lo, hi = (expect.scan_bounds + [None, None])[:2]
                ok = _close(inputs.get("start_val"), lo) and _close(inputs.get("end_val"), hi)
                checks.append(Check(name="exec.scan_bounds", passed=ok,
                                    detail=f"expected {expect.scan_bounds}, got [{inputs.get('start_val')}, {inputs.get('end_val')}]"))
            if expect.scan_duration_s is not None:
                checks.append(Check(name="exec.scan_duration", passed=_close(inputs.get("duration"), expect.scan_duration_s),
                                    detail=f"expected {expect.scan_duration_s}s, got {inputs.get('duration')}"))
    if expect.overrides_forwarded:
        sims = [c for c in calls if c.get("tool_name") in _SIM_TOOLS]
        for c in sims:
            got = (c.get("inputs") or c.get("raw_inputs") or {}).get("parameter_overrides") or {}
            missing = [n for n in expect.overrides_forwarded if n not in got]
            # A successful run without the requested override is an unchanged simulation, not the experiment asked for.
            ok = not missing or not c.get("success")
            checks.append(Check(name="exec.overrides_forwarded", passed=ok,
                                detail=f"{c.get('tool_name')}: " + (f"missing {missing} (overrides={got or None})" if missing else "forwarded")))
    ids = {str(i).upper() for i in observed.get("evidence_ids") or []}
    for want in expect.evidence_ids:
        checks.append(Check(name="exec.evidence_id", passed=want.upper() in ids, detail=f"{want} {'present' if want.upper() in ids else 'missing'}"))
    for bad in expect.prohibited_evidence_ids:
        checks.append(Check(name="exec.prohibited_evidence", passed=bad.upper() not in ids,
                            detail=f"{bad} {'wrongly present' if bad.upper() in ids else 'absent'}"))
    keys = set(observed.get("artifact_keys") or [])
    for key in expect.artifacts:
        checks.append(Check(name="exec.artifact", passed=key in keys, detail=f"{key} {'present' if key in keys else 'missing'}"))
    for key in expect.no_artifacts:
        checks.append(Check(name="exec.no_artifact", passed=key not in keys, detail=f"{key} {'wrongly present' if key in keys else 'absent'}"))
    return checks


# ---------------------------------------------------------------------------------------------------------------
# Golden facts (SAFE-style fact recall)
# ---------------------------------------------------------------------------------------------------------------

_DECIMAL_DOT = re.compile("(?<=\\d)[\u00b7\u22c5\u2219](?=\\d)")  # "0·30" (Lancet style) is 0.30
# A number not glued to a word or identifier ("G12C", "rs121434569", "NCT04303780" never match). A minus is a sign
# only when it does not follow a word or digit, so "0.23-0.41" is a range of two positive numbers.
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+"
_NUMBER_RE = re.compile(rf"(?<![A-Za-z0-9.])(?:(?<![A-Za-z0-9])(-))?({_NUM})(?![\d])")
_RANGE_RE = re.compile(rf"(?<![A-Za-z0-9.])(-?(?:{_NUM}))\s*(?:-|to|,|;)\s*(-?(?:{_NUM}))(?![\d])", re.I)
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def _fact_text(text: str) -> str:
    return _DECIMAL_DOT.sub(".", normalize(text or ""))


def _to_float(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def numbers(text: str) -> List[Tuple[float, int, int]]:
    """(value, start, end) of every number in already-normalized text."""
    out = []
    for m in _NUMBER_RE.finditer(text):
        v = _to_float(m.group(2))
        if v is not None:
            out.append((-v if m.group(1) else v, m.start(), m.end()))
    return out


def _date_regex(iso: str) -> str:
    y, mo, d = (int(x) for x in iso.split("-"))
    month = _MONTHS[mo - 1]
    names = rf"(?:{month}|{month[:3]}\.?)"
    return (rf"{iso}|{names}\s+0?{d}(?:st|nd|rd|th)?,?\s+{y}|0?{d}(?:st|nd|rd|th)?\s+{names},?\s+{y}"
            rf"|0?{mo}/0?{d}/{y}|{y}/0?{mo}/0?{d}")


def _near(pattern: Optional[str], text: str, start: int, end: int, window: int) -> bool:
    return pattern is None or bool(re.search(pattern, text[max(0, start - window): end + window], re.I))


def _within(x: float, want: float, tol: float) -> bool:
    return abs(x - want) <= tol + 1e-9


def match_fact(fact: Fact, text: str) -> Optional[str]:
    """The matched snippet when the (normalized) text states the fact, else None."""
    if fact.any_of:
        for alt in fact.any_of:
            hit = match_fact(alt, text)
            if hit is not None:
                return hit
        return None
    if fact.pattern is not None or fact.date is not None:
        regex = fact.pattern if fact.pattern is not None else _date_regex(fact.date or "")
        for m in re.finditer(regex, text, re.I):
            if _near(fact.context, text, m.start(), m.end(), fact.window):
                return m.group(0)[:80]
        return None
    want = float(fact.value or 0.0)
    ci_tol = fact.tolerance if fact.ci_tolerance is None else fact.ci_tolerance
    for v, start, end in numbers(text):
        if not _within(v, want, fact.tolerance) or not _near(fact.context, text, start, end, fact.window):
            continue
        if fact.ci is None:
            return text[max(0, start - 30): end + 10].strip()
        lo, hi = fact.ci
        for r in _RANGE_RE.finditer(text[end: end + fact.window]):
            a, b = _to_float(r.group(1)), _to_float(r.group(2))
            if a is not None and b is not None and _within(a, lo, ci_tol) and _within(b, hi, ci_tol):
                return text[max(0, start - 30): end + r.end() + 2].strip()
    return None


def _fact_label(fact: Fact) -> str:
    if fact.pattern is not None:
        return f"/{fact.pattern}/"
    if fact.date is not None:
        return fact.date
    if fact.value is not None:
        return f"{fact.value}±{fact.tolerance}" + (f" CI {fact.ci}" if fact.ci else "")
    return f"any of {[a.id for a in fact.any_of]}"


def fact_checks(facts: Sequence[Fact], response: str, *, min_recall: Optional[float] = None) -> Tuple[List[Check], Dict[str, Any]]:
    """One check per fact (informational when min_recall decides), fact_recall over the required facts and the
    ids of the required facts that are missing."""
    if not facts:
        return [], {}
    text = _fact_text(response)
    checks: List[Check] = []
    found: Dict[str, bool] = {}
    for fact in facts:
        hit = match_fact(fact, text)
        found[fact.id] = hit is not None
        checks.append(Check(name="answer.fact", passed=hit is not None, required=fact.required and min_recall is None,
                            detail=f"{fact.id}: " + (f"found ({hit})" if hit is not None else f"missing {_fact_label(fact)}")))
    required = [f for f in facts if f.required]
    recall = round(sum(found[f.id] for f in required) / len(required), 3) if required else None
    metrics = {
        "fact_recall": recall,
        "facts_found": sum(found.values()),
        "facts_total": len(facts),
        "facts_missing": [f.id for f in required if not found[f.id]],
        "facts_optional_missing": [f.id for f in facts if not f.required and not found[f.id]],
    }
    if min_recall is not None:
        checks.append(Check(name="answer.fact_recall", passed=recall is not None and recall >= min_recall,
                            detail=f"fact recall {recall} (minimum {min_recall}); missing {metrics['facts_missing'] or 'none'}"))
    return checks, metrics


# ---------------------------------------------------------------------------------------------------------------
# Identifiers, hard gates and run metrics
# ---------------------------------------------------------------------------------------------------------------

GATES = ("answer.citations_in_evidence", "answer.no_internal_leak", "answer.no_crash")

_ID_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("PMID", re.compile(r"\bPMIDs?\s*[:#]?\s*(\d{1,9}(?:\s*(?:,|;|and)\s*(?:PMID\s*:?\s*)?\d{1,9})*)", re.I)),
    ("PMID", re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{1,9})", re.I)),
    ("NCT", re.compile(r"\bNCT\s?(\d{8})\b", re.I)),
    ("CHEMBL", re.compile(r"\bCHEMBL\s?(\d{1,9})\b", re.I)),
    # FDA application numbers are written upper-case ("NDA 214665", "BLA125554", "NDA #021284").
    ("FDA", re.compile(r"\b(A?NDA|BLA)\s*(?:#|No\.?|number)?\s*:?\s*(\d{6})\b")),
)


def extract_ids(text: str) -> List[str]:
    """Canonical identifiers in text: PMID:<n>, NCT<8 digits>, CHEMBL<n>, NDA<6>/BLA<6>/ANDA<6>."""
    text = normalize(text or "")
    out: List[str] = []
    for kind, rx in _ID_PATTERNS:
        for m in rx.finditer(text):
            if kind == "PMID":
                out += [f"PMID:{int(n)}" for n in re.findall(r"\d{1,9}", m.group(1))]
            elif kind == "FDA":
                out.append(f"{m.group(1).upper()}{m.group(2)}")
            elif kind == "NCT":
                out.append(f"NCT{m.group(1)}")
            else:
                out.append(f"CHEMBL{int(m.group(1))}")
    return list(dict.fromkeys(out))


# execution_artifacts keys written by a model (the answer's claims, the plan and trace, specialist dialogue,
# hypotheses, earlier runs' project memory), not by an evidence source: never counted as evidence, or an
# identifier a model invented would vouch for itself.
_NON_EVIDENCE_ARTIFACTS = {"claim_confidence", "agent_run", "agent_plan", "agent_dialogue", "hypotheses", "model_calibration",
                           "plan_approval", "project_memory"}


def _walk_ids(node: Any, key: str, out: set, depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            out.update(extract_ids(str(k)))
            _walk_ids(v, str(k), out, depth + 1)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _walk_ids(v, key, out, depth + 1)
    elif node is not None and not isinstance(node, bool):
        value = str(node)
        out.update(extract_ids(value))
        if "pmid" in key.lower():
            out.update(f"PMID:{int(n)}" for n in re.findall(r"(?<![\w.])\d{1,9}(?![\w.])", value))


def collect_evidence_ids(payload: Dict[str, Any]) -> List[str]:
    """Every identifier the run's evidence holds, canonicalized as extract_ids does: tool canonical ids and output
    digests, the synthesis scope notes, citation entries and aliases, the verifier's verified identifiers and the
    evidence artifacts. Model-written artifacts (_NON_EVIDENCE_ARTIFACTS) are left out."""
    out: set = set()
    manifest = payload.get("run_manifest") or {}
    for call in manifest.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("success") is not False:
            _walk_ids(call.get("canonical_ids") or {}, "", out)
            _walk_ids(call.get("output_digest") or {}, "", out)
    _walk_ids((manifest.get("synthesis") or {}).get("scope_notes") or [], "", out)
    citations = payload.get("citations") or {}
    _walk_ids(citations.get("entries") or {}, "", out)
    _walk_ids(citations.get("aliases") or {}, "", out)
    _walk_ids((payload.get("verification_report") or {}).get("verified_identifiers") or [], "", out)
    artifacts = payload.get("execution_artifacts") or {}
    _walk_ids({k: v for k, v in artifacts.items() if k not in _NON_EVIDENCE_ARTIFACTS}, "", out)
    return sorted(out)


def _canonical_evidence(ids: Iterable[Any]) -> set:
    """Stored evidence ids in canonical form; bare numbers (tool canonical "pmids" values) count as PMIDs."""
    out: set = set()
    for raw in ids or []:
        s = str(raw).strip()
        if s.isdigit():
            out.add(f"PMID:{int(s)}")
        else:
            out.update(extract_ids(s))
    return out


# Internal component names and evidence-field keys that must never reach an answer (the orchestrator sanitizes
# them); tags like "[CLINICAL_CLAIM]" are prompt scaffolding. Code spans and URLs are ignored.
_LEAK_RE = re.compile(
    r"(?i:\b(?:Lithoscribe|Synapta|Vivarix|Lumicell|BioDiscovery(?:Agent)?)\b)"
    r"|\bagent\.[a-z_]+\.[\w.-]*\w"  # internal step ids ("agent.gaps.9-12", "agent.supervisor.5.dgidb")
    r"|\b(?:structure_intel|variant_intel|drug_intel|omics_intel|clinical_trials|total_reports|serious_reports|tool_status"
    r"|label_set_id|evidence_sufficiency|ranked_passages|canonical_ids|output_digest)\b"
    r"|\[(?:[A-Z]+_)+[A-Z]+\]"
)
_CODE_OR_URL_RE = re.compile(r"```.*?```|`[^`\n]*`|https?://[^\s)>\]]+|\]\([^)\s]*\)", re.S)


def internal_leaks(text: str) -> List[str]:
    return list(dict.fromkeys(m.group(0) for m in _LEAK_RE.finditer(_CODE_OR_URL_RE.sub(" ", normalize(text or "")))))


def gate_checks(observed: Dict[str, Any], *, query: str = "", skip: Sequence[str] = ()) -> List[Check]:
    """Deterministic hard gates (required checks), computed from stored observations only.

    - answer.no_crash: the run returned a non-empty answer (an exception or HTTP error fails it in the runner);
    - answer.citations_in_evidence: every PMID/NCT/CHEMBL/NDA/BLA id in the answer is in the run's evidence or in
      the question (an id in neither is hallucinated);
    - answer.no_internal_leak: no internal component name, evidence-field key or prompt tag in a research answer
      (the conversational introduction names the components on purpose and is not checked).
    """
    checks: List[Check] = []
    response = observed.get("response")
    if "answer.no_crash" not in skip:
        ok = isinstance(response, str) and bool(response.strip()) and not observed.get("transport_error")
        checks.append(Check(name="answer.no_crash", passed=ok,
                            detail="answer returned" if ok else f"no answer ({observed.get('transport_error') or 'empty response'})"))
    if "answer.citations_in_evidence" not in skip and "evidence_ids" in observed:
        cited = extract_ids(response or "")
        allowed = _canonical_evidence(observed.get("evidence_ids") or []) | set(extract_ids(query))
        missing = [i for i in cited if i not in allowed]
        checks.append(Check(name="answer.citations_in_evidence", passed=not missing,
                            detail=f"{len(cited)} ids cited, all in the evidence" if not missing
                            else f"not in the evidence: {', '.join(missing[:10])}"))
    if "answer.no_internal_leak" not in skip and observed.get("framework") != "conversational-direct":
        leaks = internal_leaks(response or "")
        checks.append(Check(name="answer.no_internal_leak", passed=not leaks,
                            detail="no internal names" if not leaks else f"leaked: {', '.join(leaks[:8])}"))
    return checks


def run_metrics(observed: Dict[str, Any]) -> Dict[str, Any]:
    """faithfulness (verifier supported / supported+unsupported+contradicted), failed steps, model and tool calls,
    estimated tokens and latency, from stored observations (None when the run did not record them)."""
    counts = observed.get("claim_counts") or {}
    checked = sum(int(counts.get(k, 0)) for k in ("supported", "unsupported", "contradicted"))
    return {
        "faithfulness": round(int(counts.get("supported", 0)) / checked, 3) if checked else None,
        "claims_checked": checked,
        "steps_failed": observed.get("steps_failed"),
        "llm_calls": observed.get("llm_calls"),
        "tool_calls": observed.get("tool_calls_n"),
        "tokens_est": observed.get("tokens_est"),
        "latency_s": observed.get("latency_s"),
    }


# ---------------------------------------------------------------------------------------------------------------
# Benchmark labels (deterministic parts; grading.py adds a model-based extractor)
# ---------------------------------------------------------------------------------------------------------------


def heuristic_yesno(text: str, options: Sequence[str] = ("yes", "no", "maybe")) -> Optional[str]:
    """An explicit opening verdict ("Yes. ...", "No, ...", "**No** - ..."), else None for the model grader to decide.
    "No retrieved evidence..." opens with the word "no" but is not a verdict, so punctuation must follow it."""
    head = re.sub(r"^[\s*_#>]+", "", normalize(text).strip())[:40].lower()
    m = re.match(r"^(yes|no)\**\s*[,.;:!—–-]", head)
    return m.group(1) if m and m.group(1) in options else None


def heuristic_choice(text: str, options: Sequence[str]) -> Optional[str]:
    """The single option the answer names (ignoring the insufficient-information option), else None."""
    low = normalize(text).lower()
    named = [o for o in options if not o.lower().startswith("insufficient") and re.search(rf"(?<!\w){re.escape(o.lower())}(?!\w)", low)]
    if len(named) == 1:
        return named[0]
    insufficient = [o for o in options if o.lower().startswith("insufficient")]
    if not named and insufficient and abstained(text):
        return insufficient[0]
    return None


def label_checks(expect: AnswerExpect, predicted: Optional[str]) -> Tuple[List[Check], Dict[str, Any]]:
    if expect.label is None:
        return [], {}
    gold = expect.label.strip().lower()
    pred = (predicted or "").strip().lower() or None
    insufficient = bool(pred and pred.startswith("insufficient"))
    correct = pred == gold
    metrics = {"label": gold, "predicted": pred, "correct": correct, "answered": pred is not None and not insufficient}
    return [Check(name="answer.label", passed=correct, detail=f"expected {gold}, got {pred}")], metrics


def exact_checks(expect: AnswerExpect, text: str) -> Tuple[List[Check], Dict[str, Any]]:
    """Factoid: any synonym of any group named (lenient accuracy). List: share of groups named (>= 50% passes)."""
    if not expect.exact:
        return [], {}
    low = normalize(text).lower()
    named = [any(re.search(rf"(?<!\w){re.escape(s.lower())}(?!\w)", low) for s in g) for g in expect.exact]
    if expect.answer_type == "list":
        recall = sum(named) / len(named)
        return [Check(name="answer.list_recall", passed=recall >= 0.5, detail=f"{sum(named)}/{len(named)} items named")], {"list_recall": round(recall, 3)}
    return [Check(name="answer.factoid", passed=any(named), detail="named" if any(named) else "not named")], {"factoid_correct": any(named)}


__all__ = [
    "GATES", "abstained", "answer_checks", "cited_pmids", "collect_evidence_ids", "exact_checks", "execution_checks",
    "extract_ids", "fact_checks", "gate_checks", "heuristic_choice", "heuristic_yesno", "internal_leaks", "label_checks",
    "match_fact", "normalize", "numbers", "opens_with_refusal", "plan_checks", "retrieval_scores", "run_metrics",
    "simulation_checks",
]
