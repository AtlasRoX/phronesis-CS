"""
Runs evaluation cases at the plan, retrieval and answer levels against the real system.

- plan: build_query_context exactly as the orchestrator calls it, plus the evidence-source routing. Offline
  (default for the plan level) uses no live resolver and no planner model, so it is fast and deterministic;
  --live adds both.
- retrieval: the plan, the final search plan and the Lithoscribe call that production makes
  (authoritative_orchestrator.literature_request), scored against gold PMIDs/DOIs in rank order.
- answer: the full orchestrator, in an isolated thread that is deleted afterwards; setup turns run first. The
  case's framework, agent_options, dataset_ids and project_id are passed through. With RunConfig.via (CLI --via
  http://host:port) the question goes to the running API's POST /api/chat instead, and the returned JSON (the
  orchestrator result's to_dict()) is scored the same way, so the API path is evaluated too.
- repeat: RunConfig.repeat > 1 runs each case that many times and reports mean, SD and a flaky flag.
- cassettes: RunConfig.cassette = "record" runs answers live and stores every tool and model call per case
  (phronesis/evals/cassettes/<suite>/<case_id>.json); "replay" runs them offline from those recordings, so the answer
  suite is deterministic and needs no keys or network (see cassette.py). In-process only, not with via.

Results are appended to results.jsonl as each case finishes, so an interrupted run can be resumed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import statistics
import sys
import time
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import grading, scoring
from .schema import CaseResult, Check, EvalCase, RetrievalExpect

logger = logging.getLogger(__name__)

LITERATURE_TOOL = "lithoscribe_literature_synthesis"
EVAL_USER = "phronesis-eval"


class CassetteRunError(RuntimeError):
    """A replayed answer run that raised; carries the cassette's counts (misses explain most replay crashes)."""

    def __init__(self, message: str, summary: Dict[str, Any]) -> None:
        super().__init__(message)
        self.summary = summary


@dataclass
class RunConfig:
    levels: List[str] = field(default_factory=lambda: ["plan"])
    model_name: Optional[str] = None  # orchestrator / planner model (default: the configured one)
    live: bool = False  # plan level: use the live entity resolver and planner model
    concurrency: int = 2  # answer-level cases at once (plan/retrieval run up to 4x this)
    timeout_s: float = 420.0  # per case and level
    judge: bool = False
    judge_model: Optional[str] = None
    judge_min: Optional[float] = None  # when set, a judge overall score below this fails the case
    grade_with_model: bool = True  # benchmark labels: ask a model when the rule cannot decide
    web_search: Optional[bool] = None  # override every case's enable_web_search
    retries: int = 1  # re-run a failed live plan/retrieval case; a pass on retry is reported as flaky
    answer_retries: int = 0  # the same for answers (each retry is a full, slow run)
    framework: Optional[str] = None  # override every case's framework
    via: Optional[str] = None  # base URL of a running API (http://127.0.0.1:8000): answers go through POST /api/chat
    repeat: int = 1  # runs per case; > 1 reports mean/SD per metric and flags flaky cases
    flaky_sd: float = 0.15  # a repeated case is flaky when fact_recall or faithfulness varies more than this (SD)
    cassette: Optional[str] = None  # "record" | "replay": answer-level calls are recorded to / replayed from cassettes
    cassette_dir: Optional[str] = None  # default phronesis/evals/cassettes
    replay_lenient: bool = False  # replay: a missed call gets the nearest recorded reply instead of failing the case


def source_fingerprint() -> str:
    """Hash of the code and data that decide behaviour, so two runs can be compared knowingly."""
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    paths = sorted([*(root / "agents").glob("*.py"), root / "lithoscribe" / "synthesis_engine.py",
                    root / "utils" / "biocontext" / "entity_resolver.py", *(Path(__file__).parent / "data").glob("*.jsonl")])
    for p in paths:
        if p.exists():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _safe_env() -> Dict[str, str]:
    return {k: v for k, v in sorted(os.environ.items())
            if (k.startswith("PHRONESIS_") or k == "AIAGENTS4PHARMA_LLM_MODEL")
            and not any(s in k for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}


class Evaluator:
    """Runs cases; the registry, orchestrator and model factory can be injected (tests use fakes)."""

    def __init__(
        self,
        config: RunConfig,
        *,
        registry: Any = None,
        orchestrator: Any = None,
        llm_factory: Optional[Callable[..., Any]] = None,
        run_id: Optional[str] = None,
        http_client: Any = None,
        http_token: Optional[str] = None,
    ) -> None:
        self.cfg = config
        self._registry = registry
        self._orchestrator = orchestrator
        self._llm_factory = llm_factory
        self.run_id = run_id or uuid.uuid4().hex[:8]
        # --via: an httpx.AsyncClient can be injected (tests use httpx.MockTransport); the token is never stored in meta.
        self._http_client = http_client
        self._owns_http = False
        self._http_token = http_token if http_token is not None else os.getenv("PHRONESIS_EVAL_TOKEN")

    # -- dependencies --------------------------------------------------------------------------------------------
    @property
    def registry(self) -> Any:
        if self._registry is None:
            from ..extensions import load_extensions
            from ..tools.registry import get_tool_registry

            load_extensions()
            self._registry = get_tool_registry()
        return self._registry

    @property
    def orchestrator(self) -> Any:
        if self._orchestrator is None:
            from ..agents.authoritative_orchestrator import AuthoritativeOrchestrator

            self._orchestrator = AuthoritativeOrchestrator()
        return self._orchestrator

    @property
    def llm_factory(self) -> Optional[Callable[..., Any]]:
        if self._llm_factory is None:
            from ..utils.llm.llm_factory import get_llm

            self._llm_factory = lambda **kw: get_llm(**kw)
        return self._llm_factory

    def _model(self) -> str:
        from ..utils.llm.llm_factory import DEFAULT_ORCHESTRATOR_MODEL

        return self.cfg.model_name or os.getenv("AIAGENTS4PHARMA_LLM_MODEL") or DEFAULT_ORCHESTRATOR_MODEL

    def _web(self, case: EvalCase) -> bool:
        return case.enable_web_search if self.cfg.web_search is None else self.cfg.web_search

    # -- plan ----------------------------------------------------------------------------------------------------
    async def _context(self, case: EvalCase, *, live: bool) -> Tuple[Dict[str, Any], Any]:
        """(observed plan, QueryContext or None for the greeting fast path), routed as the orchestrator routes."""
        from ..agents import query_planner
        from ..agents.authoritative_orchestrator import _CASUAL_RES

        query = case.query
        if any(p.match(query.strip()) for p in _CASUAL_RES):
            return {"scope": "conversational", "fast_path": True, "entity_names": [], "sources_run": []}, None
        clean = query.strip().strip("\"'")
        ctx = await query_planner.build_query_context(
            query=query, clean_query=clean, registry=self.registry if live else None, enable_web_search=self._web(case),
            attachments=[], attachment_errors=[], user_id=EVAL_USER, thread_id=f"eval-{self.run_id}-{case.id}",
            model_name=self._model(), api_key=None, llm_factory=self.llm_factory if live else None,
        )
        ran = [k for k, d in ctx.decisions.items() if d.run]
        if ctx.scope == "research":
            ran.extend(self._evidence_sources(ctx))
        names: List[str] = []
        for e in ctx.entities:
            names.extend([e.label, e.mention])
        observed = {
            "scope": ctx.scope,
            "question_type": ctx.search_plan.question_type if ctx.search_plan is not None else None,
            "primary_target": ctx.primary_target,
            "entities": [f"{e.type}:{e.label}" for e in ctx.entities],
            "entity_names": [n for n in names if n],
            "intents": sorted(ctx.intents),
            "sources_run": ran,
            "planner": ctx.planner,
            "normalization": ctx.normalization,
            "simulation_plan": _dump(getattr(ctx, "simulation_plan", None)),
            "simulation_constraints": _dump(getattr(ctx, "simulation_constraints", None)) or {},
        }
        return observed, ctx

    def _evidence_sources(self, ctx: Any) -> List[str]:
        from ..sources import get_sources
        from ..agents.query_context import SourceDecision

        out = []
        for src in get_sources():
            if src.stage != 1 or any(self.registry.get_entry(t) is None for t in src.required_tools):
                continue
            try:
                decision = src.should_run(ctx)
            except Exception:  # noqa: BLE001
                continue
            if (decision.run if isinstance(decision, SourceDecision) else bool(decision)):
                out.append(src.key)
        return out

    async def run_plan(self, case: EvalCase) -> CaseResult:
        from ..agents import query_planner

        result = self._result(case, "plan")
        observed, ctx = await self._context(case, live=self.cfg.live)
        if ctx is not None:
            query_planner.cancel_planner(ctx)
        result.observed = observed
        result.checks = scoring.plan_checks(case.plan, observed, live=self.cfg.live) if case.plan else []
        result.metrics = {"live": self.cfg.live}
        if not result.checks:
            result.skipped = "no plan expectations apply" + ("" if self.cfg.live else " offline (use --live)")
        return result

    # -- retrieval -----------------------------------------------------------------------------------------------
    async def run_retrieval(self, case: EvalCase) -> CaseResult:
        from ..agents import query_planner
        from ..agents.authoritative_orchestrator import literature_request

        result = self._result(case, "retrieval")
        expect = case.retrieval
        plan_obs, ctx = await self._context(case, live=True)
        result.observed = {"plan": plan_obs}
        if ctx is None or not ctx.should_run("lithoscribe"):
            if ctx is not None:
                query_planner.cancel_planner(ctx)
            result.checks = [Check(name="retrieval.ran", passed=False, required=bool(expect and expect.required),
                                   detail=f"literature search not run (scope {plan_obs.get('scope')})")]
            return result
        plan = await query_planner.finalize_search_plan(ctx)
        query_planner.cancel_planner(ctx)
        request = literature_request(ctx, ctx.clean_query, plan)
        res = await self.registry.execute(LITERATURE_TOOL, **request)
        data = res.data if getattr(res, "success", False) and isinstance(res.data, dict) else {}
        # One entry per article, in the order its first passage ranks: the scoring lists stay parallel.
        articles: Dict[str, Dict[str, Any]] = {}
        for p in data.get("ranked_passages") or []:
            if isinstance(p, dict):
                articles.setdefault(str(p.get("pmid") or p.get("doi") or p.get("title")), p)
        passages = list(articles.values())
        pmids = [p.get("pmid") for p in passages]
        dois = [p.get("doi") for p in passages]
        titles = [str(p.get("title") or "") for p in passages]
        result.observed.update({
            "search_plan": plan.to_dict(),
            "queries": [q.get("label") for q in request.get("search_queries", [])],
            # Parallel, rank-ordered, one per article: enough to rescore without searching again.
            "pmids": [str(p or "") for p in pmids],
            "dois": [str(d or "") for d in dois],
            "titles": titles,
            "error": None if getattr(res, "success", False) else getattr(res, "error", "failed"),
        })
        if expect is not None:
            result.checks, result.metrics = scoring.retrieval_scores(expect, pmids, dois, titles)
        else:
            result.metrics = {"retrieved": len(result.observed["pmids"])}
            result.skipped = "no gold papers for this case"
        result.metrics["plan_source"] = plan.source
        return result

    # -- answer --------------------------------------------------------------------------------------------------
    def _run_options(self, case: EvalCase) -> Dict[str, Any]:
        """framework / agent_options / dataset_ids / project_id for execute_inquiry, only those that are set."""
        opts: Dict[str, Any] = {}
        framework = self.cfg.framework or case.framework
        if framework:
            opts["framework"] = framework
        if case.agent_options:
            opts["agent_options"] = dict(case.agent_options)
        if case.dataset_ids:
            opts["dataset_ids"] = list(case.dataset_ids)
        if case.project_id:
            opts["project_id"] = case.project_id
        return opts

    def _timeout(self, case: EvalCase) -> float:
        return float(case.timeout_s or self.cfg.timeout_s)

    async def _ask_local(self, case: EvalCase, query: str, thread: str, *, setup_turn: bool,
                         model: Optional[str] = None, web: Optional[bool] = None) -> Dict[str, Any]:
        memory = setup_turn or bool(case.setup)
        res = await self.orchestrator.execute_inquiry(
            query, thread_id=thread, model_name=model or self._model(),
            enable_web_search=self._web(case) if web is None else web, user_id=EVAL_USER,
            persist_memory=memory, recall_memory=memory, **self._run_options(case),
        )
        return result_payload(res)

    def _http(self) -> Any:
        if self._http_client is None:
            import httpx

            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(self.cfg.timeout_s + 60, connect=15.0))
            self._owns_http = True
        return self._http_client

    async def aclose(self) -> None:
        """Close the HTTP client this evaluator created (an injected client is the caller's to close)."""
        if self._http_client is not None and self._owns_http:
            await self._http_client.aclose()
            self._http_client = None

    def _http_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._http_token}"} if self._http_token else {}

    async def _ask_http(self, case: EvalCase, query: str, thread: str, *, setup_turn: bool) -> Dict[str, Any]:
        """POST /api/chat with the body the web client sends; the JSON reply is the orchestrator result's to_dict()."""
        body: Dict[str, Any] = {"message": query, "thread_id": thread, "enable_web_search": self._web(case), **self._run_options(case)}
        if self.cfg.model_name:
            body["model"] = self.cfg.model_name
        url = f"{str(self.cfg.via).rstrip('/')}/api/chat"
        resp = await self._http().post(url, json=body, headers=self._http_headers(), timeout=self._timeout(case) + 60)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} from /api/chat: {resp.text[:300]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"/api/chat returned non-JSON: {resp.text[:200]}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("response"), str):
            raise RuntimeError(f"/api/chat reply has no 'response' string (keys: {sorted(payload)[:12] if isinstance(payload, dict) else type(payload).__name__})")
        payload["_http_status"] = resp.status_code
        return payload

    async def _clear_thread(self, thread: str) -> None:
        try:
            if self.cfg.via:
                await self._http().delete(f"{str(self.cfg.via).rstrip('/')}/api/memory/thread/{thread}", headers=self._http_headers())
            else:
                self.orchestrator.memory.clear_thread_memory(thread, user_id=EVAL_USER)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not clear eval thread %s: %s", thread, exc)

    # -- cassettes (record / replay) -----------------------------------------------------------------------------
    def _cassette_dir(self) -> Path:
        from .cassette import DEFAULT_DIR

        return Path(self.cfg.cassette_dir) if self.cfg.cassette_dir else DEFAULT_DIR

    def _cassette_identity(self, case: EvalCase) -> Dict[str, Any]:
        """What a recording must have been made with for its replay to stand for the case."""
        return {"query": case.query, "setup": list(case.setup), "run_options": self._run_options(case)}

    def _open_cassette(self, case: EvalCase) -> Tuple[Any, Optional[str], Optional[str]]:
        """(cassette, skip reason, stale reason) for an answer case in record or replay mode."""
        from . import cassette as cas_mod

        directory = self._cassette_dir()
        if self.cfg.cassette == "record":
            meta = {"case_id": case.id, "suite": case.suite, **self._cassette_identity(case), "model": self._model(),
                    "web_search": self._web(case)}
            return cas_mod.new_recording(cas_mod.cassette_path(directory, case.suite, case.id), meta), None, None
        path = cas_mod.find_cassette(directory, case.suite, case.id)
        if path is None:
            return None, f"no cassette at {cas_mod.cassette_path(directory, case.suite, case.id)} (record it with --record)", None
        cassette = cas_mod.Cassette.load(path, lenient=self.cfg.replay_lenient)
        current = json_roundtrip(self._cassette_identity(case))
        stale = [k for k in current if json_roundtrip(cassette.meta.get(k)) != current[k]]
        reason = (f"the case's {', '.join(stale)} changed since the cassette was recorded; re-record with --record"
                  if stale else None)
        return cassette, None, reason

    async def run_answer(self, case: EvalCase) -> CaseResult:
        result = self._result(case, "answer")
        expect = case.answer
        cassette = None
        ask_kwargs: Dict[str, Any] = {}
        if self.cfg.cassette and not self.cfg.via:
            cassette, skip, stale = self._open_cassette(case)
            if skip:
                result.skipped = skip
                return result
            if stale:
                result.observed = {"query": case.query, "cassette": {"mode": "replay", "file": cassette.path.name}}
                result.checks = [Check(name="answer.cassette", passed=False, detail=stale)]
                return result
            if cassette.mode == "replay":
                # The recording's model and web setting, unless overridden: they decide every prompt and tool call.
                ask_kwargs = {"model": self.cfg.model_name or cassette.meta.get("model"),
                              "web": self.cfg.web_search if self.cfg.web_search is not None else cassette.meta.get("web_search")}
            _ = self.orchestrator  # built (and its modules imported) before the hooks scan the loaded modules
        ask = self._ask_http if self.cfg.via else self._ask_local
        thread = f"eval-{self.run_id}-{case.id}"
        try:
            with cassette.activate() if cassette is not None else nullcontext():
                for turn in case.setup:
                    await ask(case, turn, thread, setup_turn=True, **ask_kwargs)
                t0 = time.perf_counter()
                payload = await ask(case, case.query, thread, setup_turn=False, **ask_kwargs)
                latency = time.perf_counter() - t0
        except Exception as exc:
            if cassette is not None and cassette.mode == "replay":
                raise CassetteRunError(f"{type(exc).__name__}: {exc}", cassette.summary()) from exc
            raise
        finally:
            await self._clear_thread(thread)
        observed = observe(payload, latency, query=case.query)
        observed["framework_requested"] = self._run_options(case).get("framework")
        if self.cfg.via:
            observed["transport"] = "http"
            observed["http_status"] = payload.get("_http_status")
        if cassette is not None:
            self._finish_cassette(cassette, observed, latency)
        result.observed = observed
        result.latency_s = observed["latency_s"]
        if expect is None:
            result.skipped = "no answer expectations"
            return result
        result.checks, result.metrics = await self._score_answer(case, observed, judge=self.cfg.judge)
        return result

    def _finish_cassette(self, cassette: Any, observed: Dict[str, Any], latency: float) -> None:
        """Save a recording; for a replay, report the recorded latency (replay time measures nothing) and the stats."""
        from .cassette import registry_tool_names

        tools = registry_tool_names(getattr(self.orchestrator, "registry", None))
        if cassette.mode == "record":
            cassette.meta.update({"latency_s": round(latency, 2), "registry_tools": tools})
            cassette.save()
        else:
            observed["replay_latency_s"] = round(latency, 2)
            if isinstance(cassette.meta.get("latency_s"), (int, float)):
                observed["latency_s"] = cassette.meta["latency_s"]
        info = cassette.summary()
        missing = sorted(set(cassette.meta.get("registry_tools") or []) - set(tools)) if cassette.mode == "replay" else []
        if missing:
            info["tools_not_registered"] = missing
        observed["cassette"] = info

    async def _score_answer(
        self, case: EvalCase, observed: Dict[str, Any], *, judge: bool, previous: Optional[Dict[str, Any]] = None
    ) -> Tuple[List[Check], Dict[str, Any]]:
        """Checks and metrics for an answer's observations, used by a live run and by rescore (no system call).
        judge: ask the judge model now. previous: earlier metrics; without judge, their judge scores and model-graded
        label are reused (a label the rules now decide is re-graded by rule)."""
        expect = case.answer
        assert expect is not None
        previous = previous or {}
        observed.setdefault("query", case.query)  # results stored before the query was recorded (citation gate)
        response = observed.get("response") or ""
        gold = case.retrieval.gold_pmids if case.retrieval else []
        checks, metrics = scoring.answer_checks(expect, observed, gold)
        if case.retrieval is not None and (case.retrieval.gold_pmids or case.retrieval.gold_dois):
            # Retrieved-set recall from the run manifest (unordered), informational at this level.
            unranked = RetrievalExpect(gold_pmids=case.retrieval.gold_pmids, gold_dois=case.retrieval.gold_dois, k=10**6, required=False)
            r_checks, r_metrics = scoring.retrieval_scores(unranked, observed.get("lit_pmids") or [], observed.get("lit_dois") or [])
            checks += r_checks
            metrics.update({f"retrieval_{k}": v for k, v in r_metrics.items() if k in ("hit", "recall")})
        if expect.label is not None:
            predicted = self._heuristic_label(expect, response)
            source = "rule" if predicted is not None else None
            if predicted is None and previous.get("label_source") == "model" and not judge:
                predicted, source = previous.get("predicted"), "model"
            elif predicted is None and self.cfg.grade_with_model:
                predicted = await grading.extract_label(case.query, response, expect.options or [expect.label],
                                                        llm_factory=self.llm_factory, model_name=self.cfg.judge_model)
                source = "model"
            l_checks, l_metrics = scoring.label_checks(expect, predicted)
            checks += l_checks
            metrics.update(l_metrics, label_source=source)
        e_checks, e_metrics = scoring.exact_checks(expect, response)
        checks += e_checks
        metrics.update(e_metrics)
        cassette = observed.get("cassette")
        if isinstance(cassette, dict) and cassette.get("mode") == "replay":
            failures = [*(cassette.get("misses") or []), *(f"network: {h}" for h in cassette.get("network_blocked") or [])]
            lenient = cassette.get("lenient_misses") or []
            detail = (f"{len(failures)} unrecorded call(s): " + " | ".join(failures)[:600] if failures else
                      f"{cassette.get('tool_hits', 0)} tool and {cassette.get('llm_hits', 0)} model calls replayed"
                      + (f"; {len(lenient)} lenient miss(es)" if lenient else ""))
            checks.append(Check(name="answer.cassette", passed=not failures, detail=detail))
            metrics["cassette_misses"] = len(failures) + len(lenient)
        scores = previous.get("judge") if not judge else None
        if judge and observed.get("framework") != "conversational-direct":
            scores = await grading.judge(case.query, response, case.reference, llm_factory=self.llm_factory,
                                         model_name=self.cfg.judge_model, sources=observed.get("sources") or [])
        if scores:
            metrics["judge"] = scores
            checks.append(Check(
                name="answer.judge", passed=self.cfg.judge_min is None or scores["overall"] >= self.cfg.judge_min,
                required=self.cfg.judge_min is not None,
                detail=f"overall {scores['overall']:.1f}/5: {scores.get('rationale', '')[:200]}",
            ))
        return checks, metrics

    async def rescore(self, case: EvalCase, old: CaseResult, *, rejudge: bool = False) -> CaseResult:
        """Re-apply the current case expectations and scorers to a stored result, without calling the system.
        Model-graded labels and judge scores are kept unless rejudge is set (then the model is asked again)."""
        new = old.model_copy(deep=True)
        new.checks, new.skipped = [], None
        obs = new.observed
        if new.error:
            return new.finalize()
        if old.level == "plan" and case.plan is not None:
            new.checks = scoring.plan_checks(case.plan, obs, live=bool(old.metrics.get("live")))
            if not new.checks:
                new.skipped = "no plan expectations apply"
        elif old.level == "retrieval" and case.retrieval is not None:
            if "pmids" not in obs:
                new.checks = [c for c in old.checks if c.name == "retrieval.ran"]
            else:
                keep = {k: v for k, v in old.metrics.items() if k in ("plan_source", "attempts", "flaky")}
                new.checks, new.metrics = scoring.retrieval_scores(case.retrieval, obs.get("pmids") or [], obs.get("dois") or [], obs.get("titles") or [])
                new.metrics.update(keep)
        elif old.level == "answer" and case.answer is not None and "response" in obs:
            keep = {k: v for k, v in old.metrics.items() if k in ("attempts", "flaky")}
            new.checks, new.metrics = await self._score_answer(case, obs, judge=rejudge, previous=old.metrics)
            new.metrics.update(keep)
        else:
            new.skipped = f"no {old.level} expectations"
        return new.finalize()

    @staticmethod
    def _heuristic_label(expect: Any, response: str) -> Optional[str]:
        if expect.answer_type == "yesno":
            return scoring.heuristic_yesno(response, expect.options or ("yes", "no", "maybe"))
        if expect.options:
            return scoring.heuristic_choice(response, expect.options)
        return None

    # -- orchestration -------------------------------------------------------------------------------------------
    @staticmethod
    def _result(case: EvalCase, level: str) -> CaseResult:
        return CaseResult(case_id=case.id, suite=case.suite, category=case.category, level=level, query=case.query)

    async def run_one(self, case: EvalCase, level: str) -> CaseResult:
        runner = {"plan": self.run_plan, "retrieval": self.run_retrieval, "answer": self.run_answer}[level]
        t0 = time.perf_counter()
        timeout = self._timeout(case)
        try:
            result = await asyncio.wait_for(runner(case), timeout=timeout)
        except asyncio.TimeoutError:
            result = self._result(case, level)
            result.error = f"timed out after {timeout:.0f}s"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Case %s (%s) failed", case.id, level)
            result = self._result(case, level)
            result.error = f"{type(exc).__name__}: {exc}"[:500]
            if level == "answer":
                # A crash is a hard-gate failure, counted in the summary's gate failures.
                result.checks = [Check(name="answer.no_crash", passed=False, detail=result.error[:300])]
                if isinstance(exc, CassetteRunError):
                    result.observed = {"query": case.query, "cassette": exc.summary}
                    misses = [*exc.summary.get("misses", []), *(f"network: {h}" for h in exc.summary.get("network_blocked", []))]
                    result.checks.append(Check(name="answer.cassette", passed=not misses,
                                               detail=(" | ".join(misses) or "the replay crashed without a cassette miss")[:600]))
        if result.latency_s is None:
            result.latency_s = round(time.perf_counter() - t0, 2)
        return result.finalize()

    async def run(
        self,
        cases: Sequence[EvalCase],
        *,
        results_path: Optional[Path] = None,
        done: Optional[set] = None,
        on_result: Optional[Callable[[CaseResult, int, int], None]] = None,
    ) -> List[CaseResult]:
        """Run every (case, level) the case has expectations for, skipping pairs in `done`; append as they finish."""
        jobs = [(c, lvl) for c in cases for lvl in self.cfg.levels
                if getattr(c, lvl) is not None and (c.id, lvl) not in (done or set())]
        heavy = asyncio.Semaphore(max(1, self.cfg.concurrency))
        light = asyncio.Semaphore(max(1, self.cfg.concurrency * 4))
        lock = asyncio.Lock()
        out: List[CaseResult] = []
        total = len(jobs)

        async def attempt_once(case: EvalCase, level: str) -> CaseResult:
            live = level != "plan" or self.cfg.live
            retries = (self.cfg.answer_retries if level == "answer" else self.cfg.retries) if live else 0
            attempt = 0
            while True:
                async with (heavy if level == "answer" else light):
                    res = await self.run_one(case, level)
                if res.passed or res.skipped or attempt >= retries:
                    break
                attempt += 1
            if attempt:
                res.metrics["attempts"] = attempt + 1
                res.metrics["flaky"] = res.passed  # failed first, passed on a retry: live services, not a regression
            return res

        async def job(case: EvalCase, level: str) -> None:
            if self.cfg.repeat > 1:
                # Sequential: the repeats share the case's eval thread, which is cleared after each run.
                runs = [await attempt_once(case, level) for _ in range(self.cfg.repeat)]
                res = aggregate_repeats(runs, flaky_sd=self.cfg.flaky_sd)
            else:
                res = await attempt_once(case, level)
            async with lock:
                out.append(res)
                if results_path is not None:
                    with open(results_path, "a", encoding="utf-8") as f:
                        f.write(res.model_dump_json() + "\n")
                if on_result:
                    on_result(res, len(out), total)

        if self.cfg.cassette and not self.cfg.via and any(lvl == "answer" for _, lvl in jobs):
            from .cassette import pinned_env

            with pinned_env():  # the same NLI / embedding backends when recording and replaying
                await asyncio.gather(*(job(c, lvl) for c, lvl in jobs))
        else:
            await asyncio.gather(*(job(c, lvl) for c, lvl in jobs))
        return out

    def meta(self, suites: Sequence[str], n_cases: int) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "suites": list(suites),
            "n_cases": n_cases,
            "config": asdict(self.cfg),
            "model": self._model(),
            "source_fingerprint": source_fingerprint(),
            "env": _safe_env(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        }


# ---------------------------------------------------------------------------------------------------------------
# Observations: the same for an in-process result and an /api/chat reply
# ---------------------------------------------------------------------------------------------------------------

_RESULT_FIELDS = ("response", "thoughts", "selected_agents", "framework_used", "verification_report", "execution_artifacts",
                  "citations", "run_manifest", "query_context", "task_status", "duration_seconds", "model_used")


def result_payload(res: Any) -> Dict[str, Any]:
    """An orchestrator result as the dict /api/chat returns (to_dict()); plain objects are read field by field."""
    if isinstance(res, dict):
        return dict(res)
    for name in ("to_dict", "model_dump"):
        fn = getattr(res, name, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, dict):
                    return out
            except Exception:  # noqa: BLE001
                pass
    return {k: getattr(res, k, None) for k in _RESULT_FIELDS}


def _budget_used(artifacts: Dict[str, Any]) -> Dict[str, Any]:
    run = artifacts.get("agent_run") if isinstance(artifacts, dict) else None
    budget = (run or {}).get("budget") if isinstance(run, dict) else None
    return dict((budget or {}).get("used") or {}) if isinstance(budget, dict) else {}


def observe(payload: Dict[str, Any], latency: float, *, query: str = "") -> Dict[str, Any]:
    """What the answer level stores and scores (so rescore never calls the system)."""
    report = payload.get("verification_report") or {}
    manifest = payload.get("run_manifest") or {}
    calls = [c for c in manifest.get("tool_calls") or [] if isinstance(c, dict)]
    lit = [c for c in calls if c.get("tool_name") == LITERATURE_TOOL]
    lit_pmids = [p for c in lit for p in (c.get("output_digest") or {}).get("pmids", [])]
    lit_dois = [d for c in lit for d in (c.get("output_digest") or {}).get("dois", [])]
    qc = payload.get("query_context") or {}
    entries = (payload.get("citations") or {}).get("entries", {}) or {}
    framework = payload.get("framework_used")
    artifacts = payload.get("execution_artifacts") or {}
    claim_counts: Dict[str, int] = {}
    for c in report.get("claims") or []:
        status = str(c.get("status") if isinstance(c, dict) else getattr(c, "status", "")) or "unknown"
        claim_counts[status] = claim_counts.get(status, 0) + 1
    thoughts = [t for t in payload.get("thoughts") or [] if isinstance(t, dict)]
    used = _budget_used(artifacts)
    llm_calls = used.get("llm_calls")
    tokens = used.get("tokens_estimated")
    return {
        "query": query,
        "response": payload.get("response"),
        "framework": framework,
        "agents": list(payload.get("selected_agents") or []),
        "verified": report.get("passed"),
        "verification_score": report.get("verification_score"),
        "defects": [str(d.get("description") or d.get("message") or d) if isinstance(d, dict) else str(d)
                    for d in report.get("defects") or []],
        "citation_keys": list(entries.keys()),
        # The answer's verified citations, for the judge (and a rescore that re-judges).
        "sources": [" | ".join(str(x) for x in (key, e.get("title"), e.get("journal"), e.get("year")) if x)
                    for key, e in entries.items() if isinstance(e, dict)],
        "scope": qc.get("scope", "conversational" if framework == "conversational-direct" else None),
        "search_plan": qc.get("search_plan"),
        "lit_pmids": lit_pmids,
        "lit_dois": lit_dois,
        "latency_s": round(latency, 2),
        # Execution contract: what ran with which arguments, and whether the requested operation completed.
        "task_status": dict(payload.get("task_status") or {}),
        "tool_calls": [{k: c.get(k) for k in ("tool_name", "success", "error_type", "inputs", "raw_inputs")} for c in calls],
        "artifact_keys": sorted(artifacts.keys()) if isinstance(artifacts, dict) else [],
        # Every identifier in the run's evidence (citation gate), plus the older canonical-id list.
        "evidence_ids": sorted(set(_evidence_ids(manifest, entries)) | set(scoring.collect_evidence_ids(payload))),
        # Run metrics (faithfulness, failed steps, calls, tokens).
        "claim_counts": claim_counts,
        "steps_failed": sum(1 for t in thoughts if str(t.get("status")) == "error"),
        "steps_total": len(thoughts),
        "llm_calls": int(llm_calls) if isinstance(llm_calls, (int, float)) else None,
        "tool_calls_n": len(calls) if manifest else (int(used["tool_calls"]) if isinstance(used.get("tool_calls"), (int, float)) else None),
        "tokens_est": int(tokens) if isinstance(tokens, (int, float)) else None,
    }


# ---------------------------------------------------------------------------------------------------------------
# Repeats
# ---------------------------------------------------------------------------------------------------------------

REPEAT_METRICS = ("fact_recall", "faithfulness", "latency_s", "llm_calls", "tool_calls", "tokens_est", "steps_failed")


def _sd(values: List[float]) -> float:
    return round(statistics.stdev(values), 3) if len(values) > 1 else 0.0


def aggregate_repeats(runs: Sequence[CaseResult], *, flaky_sd: float = 0.15) -> CaseResult:
    """One result for N runs of a case: the first run's observations and checks, metrics replaced by their means,
    metrics["repeat"] with per-run values, mean and SD, and flaky when pass status differs between runs or
    fact_recall/faithfulness vary by more than flaky_sd. The case passes when at least half the runs pass."""
    # The representative run: the first that did not error (its observations can be rescored).
    first = next((r for r in runs if r.error is None), runs[0]).model_copy(deep=True)
    scored = [r for r in runs if r.skipped is None]
    if not scored:
        return first
    passes = [bool(r.passed) for r in scored]
    per_run: List[Dict[str, Any]] = []
    for r in scored:
        row: Dict[str, Any] = {"passed": r.passed, "error": r.error}
        for k in REPEAT_METRICS:
            v = r.latency_s if k == "latency_s" else r.metrics.get(k)
            row[k] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        per_run.append(row)
    mean: Dict[str, Optional[float]] = {}
    sd: Dict[str, Optional[float]] = {}
    for k in REPEAT_METRICS:
        vals = [float(row[k]) for row in per_run if row[k] is not None]
        mean[k] = round(statistics.mean(vals), 3) if vals else None
        sd[k] = _sd(vals) if vals else None
    mean["pass"] = round(sum(passes) / len(passes), 3)
    sd["pass"] = _sd([float(p) for p in passes])
    noisy = [k for k in ("fact_recall", "faithfulness") if (sd.get(k) or 0.0) > flaky_sd]
    flaky = len(set(passes)) > 1 or bool(noisy)
    out = first
    for k in REPEAT_METRICS:
        if k != "latency_s" and mean[k] is not None:
            out.metrics[k] = mean[k]
    out.latency_s = mean["latency_s"] if mean["latency_s"] is not None else out.latency_s
    out.metrics["repeat"] = {"n": len(runs), "scored": len(scored), "pass_rate": mean["pass"], "mean": mean, "sd": sd,
                             "runs": per_run, "noisy": noisy}
    out.metrics["flaky"] = flaky
    out.error = None if any(r.error is None for r in scored) else first.error
    out.passed = sum(passes) * 2 >= len(passes)
    return out


def json_roundtrip(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _dump(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    try:
        return value.model_dump() if hasattr(value, "model_dump") else dict(value)
    except Exception:  # noqa: BLE001
        return None


def _evidence_ids(manifest: Dict[str, Any], citation_entries: Any) -> List[str]:
    """Identifiers the run's tools returned (canonical ids) and the citation keys matched to evidence."""
    ids: List[str] = []
    for c in manifest.get("tool_calls") or []:
        if not isinstance(c, dict) or not c.get("success"):
            continue
        for value in (c.get("canonical_ids") or {}).values():
            ids.extend(part.strip() for part in str(value).split(",") if part.strip())
    for key in (citation_entries or {}) if isinstance(citation_entries, dict) else []:
        ids.append(str(key))
        if ":" in str(key):
            ids.append(str(key).split(":", 1)[1])
    return sorted(set(ids))


def load_results(path: Path) -> List[CaseResult]:
    """Results of a run, one per (case, level): the last line wins, so a re-run replaces an earlier result."""
    if not path.exists():
        return []
    latest: Dict[Tuple[str, str], CaseResult] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = CaseResult.model_validate_json(line)
            latest[(r.case_id, r.level)] = r
    return list(latest.values())


__all__ = ["EVAL_USER", "Evaluator", "REPEAT_METRICS", "RunConfig", "aggregate_repeats", "load_results", "observe", "result_payload",
           "source_fingerprint"]
