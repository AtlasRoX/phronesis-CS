"""
Evaluation cases and results.

A case is one question plus what a correct run must show, at up to three levels:
- plan: how the question is read and routed (scope, question type, entities, sources run or skipped);
- retrieval: which papers the literature search must find (gold PMIDs or DOIs; any one is a hit);
- answer: what the final answer must and must not contain, whether it must abstain, and a label for
  benchmark questions (yes/no/maybe, multiple choice, factoid, list).
Execution contracts are checked separately from wording: the simulation plan the question was parsed into
(plan level), and the operation that actually ran, its arguments, the task status and the evidence ids (answer
level). Selecting an agent is not proof that the requested operation ran.
Every expectation is optional, so one case can be scored at any level it defines expectations for.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

Level = Literal["plan", "retrieval", "answer"]
LEVELS: tuple = ("plan", "retrieval", "answer")


class PlanExpect(BaseModel):
    scope: Optional[Literal["research", "conversational", "unclear"]] = None
    question_type: Optional[str] = None
    primary_target: Optional[str] = None  # "" means: no primary target
    entities: List[str] = Field(default_factory=list)  # labels or mentions that must be recognized ("Sepsis")
    not_entities: List[str] = Field(default_factory=list)  # must NOT be recognized ("dose", "CAR")
    run: List[str] = Field(default_factory=list)  # sources that must run ("lithoscribe", "clinical_trials")
    skip: List[str] = Field(default_factory=list)  # sources that must not run ("vivarix")
    offline: bool = False  # the expectations hold without the live resolver or planner model (CI-safe)
    simulation: Optional["SimulationExpect"] = None  # the parsed SimulationPlan (checked offline: it is deterministic)


class SimulationExpect(BaseModel):
    """What query_context.SimulationPlan / SimulationConstraints must say for the question."""

    action: Optional[str] = None  # time_course|steady_state|parameter_scan; "" means no simulation plan at all
    forbidden: Optional[bool] = None  # simulation_constraints.simulation_forbidden
    duration_seconds: Optional[float] = None
    scan_parameter: Optional[str] = None
    scan_start: Optional[float] = None
    scan_end: Optional[float] = None
    scan_points: Optional[int] = None
    overrides: List[str] = Field(default_factory=list)  # parameter names that must be kept in parameter_overrides
    validation_error: Optional[str] = None  # a code that must be in validation_errors (e.g. DURATION_OUT_OF_RANGE)
    valid: Optional[bool] = None  # True: no validation errors


class RetrievalExpect(BaseModel):
    gold_pmids: List[str] = Field(default_factory=list)  # any one retrieved counts as a hit
    gold_dois: List[str] = Field(default_factory=list)
    # Regexes on retrieved titles that also count as a hit: open retrieval has many right answers, so a case can
    # accept any paper of the right kind ("\\bevoke\\b" for the semaglutide Alzheimer's trials), not one PMID only.
    gold_title_patterns: List[str] = Field(default_factory=list)
    k: int = 8
    required: bool = True  # False: report recall, but a miss does not fail the case


class Fact(BaseModel):
    """A golden fact the answer must state (SAFE-style fact recall). Exactly one matcher is set:

    - pattern: a regex (case-insensitive), e.g. "NDA ?214665" (in JSON, escape backslashes: "NDA\\\\s*214665");
    - value (+ tolerance, optional ci [low, high]): a number, matched however it is written ("0.30", "0.3",
      "0·30", a unicode minus); with ci, an interval within `window` characters after the number must match too
      ("95% CI 0.23–0.41", "(0.23 to 0.41)", "0.23, 0.41");
    - date: an ISO date, matched as 2014-12-19, December 19, 2014, Dec. 19, 2014, 19 December 2014 or 12/19/2014;
    - any_of: alternative facts, any one of which counts.
    context: a regex that must occur within `window` characters of the match (e.g. "CodeBreaK|sotorasib").
    """

    id: str
    pattern: Optional[str] = None
    value: Optional[float] = None
    tolerance: float = 0.0
    ci: Optional[List[float]] = None
    ci_tolerance: Optional[float] = None  # default: tolerance
    date: Optional[str] = None
    any_of: List["Fact"] = Field(default_factory=list)
    context: Optional[str] = None
    window: int = 200
    required: bool = True  # an optional fact is reported but does not count toward fact_recall
    note: Optional[str] = None

    @model_validator(mode="after")
    def _one_matcher(self) -> "Fact":
        set_ = [n for n in ("pattern", "value", "date") if getattr(self, n) is not None] + (["any_of"] if self.any_of else [])
        if len(set_) != 1:
            raise ValueError(f"fact {self.id}: set exactly one of pattern, value, date, any_of (got {set_ or 'none'})")
        if self.ci is not None and (self.value is None or len(self.ci) != 2):
            raise ValueError(f"fact {self.id}: ci needs a value and two bounds")
        if self.date is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.date):
            raise ValueError(f"fact {self.id}: date must be YYYY-MM-DD")
        return self

    def regexes(self) -> List[str]:
        out = [p for p in (self.pattern, self.context) if p]
        for alt in self.any_of:
            out += alt.regexes()
        return out


class AnswerExpect(BaseModel):
    must_mention: List[str] = Field(default_factory=list)  # regexes, all must match (case-insensitive)
    must_not_mention: List[str] = Field(default_factory=list)  # regexes, none may match
    abstain: Optional[bool] = None  # True: must say the evidence is missing; False: must not refuse
    agents_include: List[str] = Field(default_factory=list)  # e.g. "Vivarix"
    agents_exclude: List[str] = Field(default_factory=list)
    framework: Optional[str] = None  # the framework that must have been USED, e.g. "conversational-direct"
    verified: Optional[bool] = True  # the verifier must pass (None: not checked)
    cite_gold: bool = False  # a gold PMID must be cited in the answer
    min_citations: int = 0
    # Benchmark labels: graded from the free-text answer.
    label: Optional[str] = None  # yes|no|maybe, or the correct option text
    options: List[str] = Field(default_factory=list)  # multiple choice: every option, correct one included
    exact: List[List[str]] = Field(default_factory=list)  # factoid/list: synonym groups that count as an answer
    answer_type: Optional[Literal["yesno", "choice", "factoid", "list", "summary"]] = None
    max_latency_s: Optional[float] = None
    # Budgets: the run's estimated tokens, model calls and tool calls (agent budget, or the run manifest's tool calls).
    max_tokens: Optional[int] = None
    max_llm_calls: Optional[int] = None
    max_tool_calls: Optional[int] = None  # 0: no tool may run (greetings)
    execution: Optional["ExecutionExpect"] = None
    # Golden facts (fact_recall = required facts found / required facts). With min_fact_recall, one required check
    # "answer.fact_recall" decides and each fact is informational; otherwise each fact is a check of its own.
    facts: List[Fact] = Field(default_factory=list)
    min_fact_recall: Optional[float] = None
    min_faithfulness: Optional[float] = None  # verifier: supported / (supported + unsupported + contradicted)
    # Hard gates run on every answer (answer.citations_in_evidence, answer.no_internal_leak, answer.no_crash);
    # list a gate here to switch it off for this case.
    skip_gates: List[str] = Field(default_factory=list)


class ExecutionExpect(BaseModel):
    """The run's execution contract, read from the result's task_status and the run manifest's tool calls."""

    task_status: Optional[str] = None  # completed|partial|needs_input|failed|cancelled
    requested_operation: Optional[str] = None
    executed_operation: Optional[str] = None  # "" means nothing may have been executed
    request_match: Optional[bool] = None
    tools_called: List[str] = Field(default_factory=list)  # tool names that must have been called
    tools_not_succeeded: List[str] = Field(default_factory=list)  # no successful call may exist (no substituted result)
    error_types: List[str] = Field(default_factory=list)  # typed errors that must be reported (tool call or task reason)
    scan_points: Optional[int] = None  # vivarix_parameter_scan num_steps
    scan_bounds: List[float] = Field(default_factory=list)  # [start, end]
    scan_duration_s: Optional[float] = None  # per point
    overrides_forwarded: List[str] = Field(default_factory=list)  # names every simulation call must receive
    evidence_ids: List[str] = Field(default_factory=list)  # ids that must be in the evidence (canonical ids, citations)
    prohibited_evidence_ids: List[str] = Field(default_factory=list)
    artifacts: List[str] = Field(default_factory=list)  # execution_artifacts keys that must exist
    no_artifacts: List[str] = Field(default_factory=list)  # keys that must not exist


class EvalCase(BaseModel):
    id: str
    suite: str
    query: str
    category: str = "general"
    tags: List[str] = Field(default_factory=list)
    setup: List[str] = Field(default_factory=list)  # earlier turns in the same thread (memory/follow-up cases)
    enable_web_search: bool = True
    # How the answer level runs the question (passed to execute_inquiry, or to POST /api/chat with --via).
    framework: Optional[str] = None  # requested orchestration framework (e.g. "multi-agent-supervisor"); --framework overrides
    agent_options: Dict[str, Any] = Field(default_factory=dict)  # e.g. {"budget": {"max_llm_calls": 30}}
    dataset_ids: List[str] = Field(default_factory=list)
    project_id: Optional[str] = None
    timeout_s: Optional[float] = None  # per-case timeout (slow agentic frameworks); default: --timeout
    plan: Optional[PlanExpect] = None
    retrieval: Optional[RetrievalExpect] = None
    answer: Optional[AnswerExpect] = None
    reference: Optional[str] = None  # an expert answer for the judge
    source: str = "curated"
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _patterns_compile(self) -> "EvalCase":
        """Every regex must compile and hold no control characters (a lost backslash turns \\b into a backspace)."""
        patterns = [*(self.answer.must_mention if self.answer else []), *(self.answer.must_not_mention if self.answer else []),
                    *(self.retrieval.gold_title_patterns if self.retrieval else []),
                    *(p for f in (self.answer.facts if self.answer else []) for p in f.regexes())]
        for p in patterns:
            if any(ord(ch) < 32 for ch in p):
                raise ValueError(f"{self.id}: pattern {p!r} contains a control character (check backslash escaping)")
            try:
                re.compile(p)
            except re.error as exc:
                raise ValueError(f"{self.id}: invalid regex {p!r}: {exc}") from exc
        return self

    def levels(self) -> List[str]:
        return [lvl for lvl in LEVELS if getattr(self, lvl) is not None]


class Check(BaseModel):
    name: str
    passed: bool
    detail: str = ""
    required: bool = True  # an optional check is reported but never fails the case


class CaseResult(BaseModel):
    case_id: str
    suite: str
    category: str
    level: str
    query: str
    passed: bool = False
    checks: List[Check] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    latency_s: Optional[float] = None
    error: Optional[str] = None
    skipped: Optional[str] = None  # why the case has nothing to score at this level
    observed: Dict[str, Any] = Field(default_factory=dict)  # what the run produced (plan, pmids, answer, ...)

    def finalize(self) -> "CaseResult":
        self.passed = self.error is None and self.skipped is None and all(c.passed for c in self.checks if c.required)
        return self


PlanExpect.model_rebuild()
Fact.model_rebuild()
AnswerExpect.model_rebuild()

__all__ = [
    "AnswerExpect", "CaseResult", "Check", "EvalCase", "ExecutionExpect", "Fact", "LEVELS", "Level", "PlanExpect", "RetrievalExpect",
    "SimulationExpect",
]
