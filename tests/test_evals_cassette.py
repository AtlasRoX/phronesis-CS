"""Record/replay cassettes for answer-level evals (phronesis/evals/cassette.py): keys, redaction, hooks, determinism."""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import socket
from typing import Any, Dict, List

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from phronesis.evals import cassette as cas
from phronesis.evals.cli import main as cli_main
from phronesis.evals.runner import Evaluator, RunConfig
from phronesis.evals.schema import EvalCase
from phronesis.tools import reliability
from phronesis.utils.llm import llm_factory
from tests.fakes import build_recording_registry

TODAY = dt.date.today().isoformat()


# ---------------------------------------------------------------------------------------------------------------
# Normalisation, redaction, keys
# ---------------------------------------------------------------------------------------------------------------

def test_normalize_replaces_volatile_substrings_only():
    text = (f"retrieval_date {TODAY}; at 2026-03-01T10:22:33.123Z run run_0123456789abcdef job job_deadbeefcafe "
            "id 123e4567-e89b-12d3-a456-426614174000 thread eval-1a2b3c4d-golden-kras ts 1760712345.5 "
            "approved 2021-05-28, PMID 34096690, NCT03600883")
    out = cas.normalize(text)
    assert TODAY not in out and "<today>" in out
    assert "<datetime>" in out and "<uuid>" in out and "run_<id>" in out and "job_<id>" in out and "<ts>" in out
    assert "eval-<run>-golden-kras" in out
    # Evidence is kept: historical dates, PMIDs, NCT ids.
    assert "2021-05-28" in out and "34096690" in out and "NCT03600883" in out


def test_normalize_handles_the_recorded_date_and_long_forms():
    day = dt.date(2026, 10, 17)
    assert cas.normalize("as of October 17, 2026 (2026-10-17)", [day]) == "as of <today> (<today>)"


def test_redact_removes_keys_tokens_and_env_secrets(monkeypatch):
    monkeypatch.setenv("SOME_SERVICE_API_KEY", "s3cr3t-value-123")
    value = {
        "api_key": "abc", "headers": {"Authorization": "Bearer abcdefghijklmnop123"}, "max_tokens": 200,
        "text": "key nvapi-ABCDEFGHIJKLMNOP and sk-abcdefghijklmnopqrstu, url https://x.org/q?api_key=zzz&b=1",
        "note": "uses s3cr3t-value-123 inline", "auth": "Bearer abcdefghijklmnopqrstuvwxyz",
    }
    out = cas.redact(value)
    blob = json.dumps(out)
    for secret in ("abcdefghijklmnop123", "nvapi-ABCDEFGHIJKLMNOP", "sk-abcdefghijklmnopqrstu", "zzz", "s3cr3t-value-123",
                   "abcdefghijklmnopqrstuvwxyz"):
        assert secret not in blob
    assert out["api_key"] == cas.REDACTED and out["headers"]["Authorization"] == cas.REDACTED
    assert out["max_tokens"] == 200  # a count, not a secret


def test_tool_key_ignores_volatile_keys_and_dates():
    c = cas.Cassette("x.json", "record")
    a = c.tool_key("t", {"query": f"KRAS as of {TODAY}", "request_id": "r1", "api_key": "k1", "n": 3})
    b = c.tool_key("t", {"n": 3, "query": f"KRAS as of {TODAY}", "request_id": "r2", "api_key": "k2"})
    assert a == b
    assert a != c.tool_key("t", {"query": "NRAS", "n": 3})
    assert a != c.tool_key("u", {"query": f"KRAS as of {TODAY}", "n": 3})


# ---------------------------------------------------------------------------------------------------------------
# A fake orchestrator on the real registry and the real model hooks
# ---------------------------------------------------------------------------------------------------------------

class FakeChat:
    """A chat model with astream (reasoning in additional_kwargs) and ainvoke; counts calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.temperature = 0.0

    async def ainvoke(self, messages: Any, *a: Any, **k: Any) -> AIMessage:
        self.calls += 1
        return AIMessage(content=f'{{"verdict": "ok", "n": {len(messages)}}}')

    async def astream(self, messages: Any, *a: Any, **k: Any):
        from langchain_core.messages import AIMessageChunk

        self.calls += 1
        yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": "thinking about KRAS"})
        for part in ("Sotorasib NDA 214665 was approved on 2021-05-28. ", "CodeBreaK 200 PFS HR 0.66 (95% CI 0.51-0.86)."):
            yield AIMessageChunk(content=part)


class CassetteFakeOrchestrator:
    """Calls a tool through the real registry, then the model through the module attributes the hooks patch."""

    def __init__(self, registry: Any) -> None:
        from tests.test_evals_harness import FakeMemory

        self._registry = registry
        self.memory = FakeMemory()
        self.reasoning: List[str] = []

    @property
    def registry(self) -> Any:
        return self._registry

    async def execute_inquiry(self, query: str, **kw: Any) -> Dict[str, Any]:

        res = await self._registry.execute("lithoscribe_literature_synthesis", query=query, max_articles=8)
        llm = llm_factory.get_llm(model_name=kw["model_name"], streaming=True)
        messages = [SystemMessage(content=f"<retrieval_date>{dt.date.today().isoformat()}</retrieval_date> Answer."),
                    HumanMessage(content=f"{query}\nEvidence: {json.dumps(res.data)}")]

        async def on_reasoning(delta: str) -> None:
            self.reasoning.append(delta)

        text, _ = await llm_factory.astream_with_reasoning(llm, messages, on_reasoning=on_reasoning)
        check = await llm_factory.get_llm(model_name=kw["model_name"]).ainvoke([HumanMessage(content=f"check: {text}")])
        pmid = (res.data or {}).get("ranked_passages", [{}])[0].get("pmid")
        return {
            "response": f"{text} (PMID: {pmid})",
            "framework_used": kw.get("framework") or "authoritative-react-compiler",
            "verification_report": {"passed": True, "claims": [{"status": "supported"}]},
            "run_manifest": {"tool_calls": [{"tool_name": res.tool_name, "success": res.success,
                                             "canonical_ids": {"pmid": str(pmid), "nda": "NDA214665"}, "output_digest": {"pmids": [str(pmid)]}}]},
            "execution_artifacts": {"check": check.content},
            "query_context": {"scope": "research"},
        }


def golden_case(**over: Any) -> EvalCase:
    data: Dict[str, Any] = {
        "id": "golden-fake", "suite": "golden", "query": "Summarise sotorasib's approval and CodeBreaK 200.",
        "framework": "agentic-plan-execute",
        "answer": {"verified": None, "facts": [
            {"id": "nda", "pattern": r"NDA\s*214665"}, {"id": "date", "date": "2021-05-28"},
            {"id": "hr", "value": 0.66, "tolerance": 0.01, "ci": [0.51, 0.86], "context": "CodeBreaK"}]},
    }
    data.update(over)
    return EvalCase.model_validate(data)


def _fake_env(monkeypatch):
    chat = FakeChat()
    monkeypatch.setattr(llm_factory, "get_llm", lambda *a, **k: chat)
    reg, calls = build_recording_registry()
    return chat, reg, calls


def _run(ev: Evaluator, cases: List[EvalCase]):
    return asyncio.run(ev.run(cases))


def _scored(res) -> Dict[str, Any]:
    """What a replay must reproduce exactly: pass, checks, metrics and the scored observations."""
    obs = {k: v for k, v in res.observed.items() if k != "replay_latency_s"}
    return {"passed": res.passed, "checks": [c.model_dump() for c in res.checks], "metrics": res.metrics,
            "observed": obs, "latency_s": res.latency_s}


def test_record_then_replay_twice_is_identical_and_offline(monkeypatch, tmp_path):
    chat, reg, calls = _fake_env(monkeypatch)
    orch = CassetteFakeOrchestrator(reg)
    case = golden_case()
    [rec] = _run(Evaluator(RunConfig(levels=["answer"], cassette="record", cassette_dir=str(tmp_path)), orchestrator=orch), [case])
    assert rec.error is None and rec.passed, rec.checks
    path = cas.find_cassette(tmp_path, "golden", "golden-fake")
    assert path is not None and path.name == "golden-fake.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["meta"]["query"] == case.query and data["meta"]["run_options"]["framework"] == "agentic-plan-execute"
    assert sum(len(v) for v in data["tools"].values()) == 1 and sum(len(v) for v in data["llm"].values()) == 2
    entry = next(iter(data["llm"].values()))[0]
    assert "prompt" in entry and "text" in entry
    assert chat.calls == 2 and len(calls) == 1

    # Replay: neither the model nor the tool may run, and the network is closed.
    def no_model(*_a, **_k):
        raise AssertionError("the live model factory must not be called during replay")

    monkeypatch.setattr(llm_factory, "get_llm", no_model)
    results = []
    for _ in range(2):
        orch.reasoning.clear()
        [rep] = _run(Evaluator(RunConfig(levels=["answer"], cassette="replay", cassette_dir=str(tmp_path)), orchestrator=orch), [case])
        assert rep.error is None, rep.error
        assert orch.reasoning == ["thinking about KRAS"]  # reasoning is replayed through the callback
        results.append(_scored(rep))
    assert chat.calls == 2 and len(calls) == 1
    assert results[0] == results[1]
    cassette_check = next(c for c in results[0]["checks"] if c["name"] == "answer.cassette")
    assert cassette_check["passed"] and results[0]["metrics"]["cassette_misses"] == 0
    # The replay reproduces the recorded run's answer, metrics and latency.
    assert results[0]["observed"]["response"] == rec.observed["response"]
    assert results[0]["metrics"]["fact_recall"] == rec.metrics["fact_recall"] == 1.0
    assert results[0]["latency_s"] == data["meta"]["latency_s"]
    # The hooks are gone afterwards.
    assert llm_factory.get_llm is no_model
    assert reliability.execute_with_policies.__name__ == "execute_with_policies"


def test_replay_miss_fails_the_case_and_lenient_reports_it(monkeypatch, tmp_path):
    chat, reg, _calls = _fake_env(monkeypatch)
    orch = CassetteFakeOrchestrator(reg)
    _run(Evaluator(RunConfig(levels=["answer"], cassette="record", cassette_dir=str(tmp_path)), orchestrator=orch), [golden_case()])

    # The system changed: the tool now returns different evidence, so the prompt no longer matches the recording,
    # and the tool is called with different arguments.
    real_execute = reg.execute

    async def changed_execute(tool_name: str, **kwargs: Any) -> Any:
        return await real_execute(tool_name, **{**kwargs, "max_articles": 5})

    monkeypatch.setattr(reg, "execute", changed_execute)
    [strict] = _run(Evaluator(RunConfig(levels=["answer"], cassette="replay", cassette_dir=str(tmp_path)), orchestrator=orch), [golden_case()])
    check = next(c for c in strict.checks if c.name == "answer.cassette")
    assert not check.passed and "tool lithoscribe_literature_synthesis" in check.detail
    assert not strict.passed

    [lenient] = _run(Evaluator(RunConfig(levels=["answer"], cassette="replay", cassette_dir=str(tmp_path), replay_lenient=True),
                               orchestrator=orch), [golden_case()])
    check = next(c for c in lenient.checks if c.name == "answer.cassette")
    assert check.passed and "lenient" in check.detail and lenient.metrics["cassette_misses"] >= 1


def test_replay_skips_cases_without_a_cassette_and_flags_stale_ones(monkeypatch, tmp_path):
    _chat, reg, _calls = _fake_env(monkeypatch)
    orch = CassetteFakeOrchestrator(reg)
    [skipped] = _run(Evaluator(RunConfig(levels=["answer"], cassette="replay", cassette_dir=str(tmp_path)), orchestrator=orch), [golden_case()])
    assert skipped.skipped and "no cassette" in skipped.skipped
    _run(Evaluator(RunConfig(levels=["answer"], cassette="record", cassette_dir=str(tmp_path)), orchestrator=orch), [golden_case()])
    [stale] = _run(Evaluator(RunConfig(levels=["answer"], cassette="replay", cassette_dir=str(tmp_path)), orchestrator=orch),
                   [golden_case(query="A different question about sotorasib.")])
    assert not stale.passed and "query changed" in stale.checks[0].detail


def test_large_cassettes_are_gzipped_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(cas, "GZIP_ABOVE_BYTES", 100)
    c = cas.new_recording(cas.cassette_path(tmp_path, "golden", "big"), {"case_id": "big"})
    c.data["calls"]["k"] = [{"result": "x" * 500}]
    path = c.save()
    assert path.suffix == ".gz" and json.loads(gzip.decompress(path.read_bytes()))["meta"]["case_id"] == "big"
    assert cas.find_cassette(tmp_path, "golden", "big") == path
    assert cas.Cassette.load(path).data["calls"]["k"][0]["result"] == "x" * 500


def test_network_is_refused_during_replay_and_restored_after(tmp_path):
    c = cas.Cassette(tmp_path / "c.json", "replay")
    with c.activate():
        with pytest.raises(OSError, match="cassette replay"):
            socket.getaddrinfo("www.ebi.ac.uk", 443)
        socket.getaddrinfo("127.0.0.1", 80)  # loopback stays open
    assert c.failures and "www.ebi.ac.uk" in c.failures[0]
    assert socket.getaddrinfo.__module__ == "socket"


def test_recorded_errors_replay_with_the_same_name(tmp_path):
    c = cas.Cassette(tmp_path / "c.json", "record")
    c._record_llm("m", [HumanMessage(content="hi")], "stream", exc=type("ReadError", (Exception,), {})("dropped"))
    r = cas.Cassette(tmp_path / "c.json", "replay", data=c.data)
    with pytest.raises(cas.CassetteRecordedError) as info:
        r._replay_llm("m", [HumanMessage(content="hi")])
    assert type(info.value).__name__ == "ReadError"  # the orchestrator retries transient stream errors by name


def test_cli_rejects_record_with_via_or_without_answer(capsys):
    assert cli_main(["run", "--suite", "golden", "--level", "answer", "--record", "--via", "http://x"]) == 2
    assert cli_main(["run", "--suite", "golden", "--level", "plan", "--replay"]) == 2
    assert cli_main(["run", "--suite", "golden", "--level", "answer", "--record", "--replay"]) == 2


def test_cli_replay_with_no_cassettes_skips_every_case(tmp_path):
    out = tmp_path / "runs"
    code = cli_main(["run", "--suite", "golden", "--level", "answer", "--replay", "--cassette-dir", str(tmp_path / "none"),
                     "--out", str(out)])
    assert code == 0
    [run_dir] = list(out.iterdir())
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and all(r["skipped"] and "no cassette" in r["skipped"] for r in rows)


# ---------------------------------------------------------------------------------------------------------------
# The real AuthoritativeOrchestrator, offline (scripted model, recording registry)
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("framework", [None, "agentic-plan-execute"])
def test_real_orchestrator_record_then_replay_is_deterministic(monkeypatch, tmp_path, framework):
    from tests.agentic_fakes import make_agentic_orchestrator

    orch, calls, llm = make_agentic_orchestrator(monkeypatch, tmp_path / "orch")
    case = EvalCase.model_validate({
        "id": f"fake-{framework or 'classic'}", "suite": "golden",
        "query": "What diseases is KRAS associated with, and which pathways is it part of?",
        **({"framework": framework} if framework else {}),
        "answer": {"verified": None, "must_mention": ["pancreatic"]},
    })
    cdir = str(tmp_path / "cassettes")
    [rec] = _run(Evaluator(RunConfig(levels=["answer"], cassette="record", cassette_dir=cdir), orchestrator=orch), [case])
    assert rec.error is None, rec.error
    n_tools, n_llm = len(calls), len(llm.calls)
    assert n_tools >= 1 and n_llm >= 1
    data = json.loads(cas.find_cassette(cdir, "golden", case.id).read_text(encoding="utf-8"))
    assert sum(len(v) for v in data["tools"].values()) >= 1 and sum(len(v) for v in data["llm"].values()) >= 1

    replays = []
    for _ in range(2):
        [rep] = _run(Evaluator(RunConfig(levels=["answer"], cassette="replay", cassette_dir=cdir), orchestrator=orch), [case])
        assert rep.error is None, rep.error
        replays.append(_scored(rep))
    # Nothing ran live during replay.
    assert len(calls) == n_tools and len(llm.calls) == n_llm
    first = replays[0]
    cassette_check = next(c for c in first["checks"] if c["name"] == "answer.cassette")
    assert cassette_check["passed"], cassette_check["detail"]
    assert first["observed"]["cassette"]["tool_hits"] >= 1 and first["observed"]["cassette"]["llm_hits"] >= 1
    assert replays[0] == replays[1]
    # The replay reproduces the recorded answer and its manifest-derived observations.
    assert first["observed"]["response"] == rec.observed["response"]
    for key in ("tool_calls_n", "evidence_ids", "agents", "framework", "claim_counts", "llm_calls"):
        assert first["observed"][key] == rec.observed[key], key
    assert first["passed"] == rec.passed


def test_provenance_probes_are_recorded_and_replayed(monkeypatch, tmp_path):
    from phronesis.provenance import source_versions

    probes = []

    async def fake_versions(transport=None):
        probes.append(1)
        return {"kegg": {"value": "2026/10/01"}}

    monkeypatch.setattr(source_versions, "get_source_versions", fake_versions)
    rec = cas.Cassette(tmp_path / "c.json", "record")
    with rec.activate():
        assert asyncio.run(source_versions.get_source_versions()) == {"kegg": {"value": "2026/10/01"}}
    rep = cas.Cassette(tmp_path / "c.json", "replay", data=rec.data)
    with rep.activate():
        assert asyncio.run(source_versions.get_source_versions()) == {"kegg": {"value": "2026/10/01"}}
    assert probes == [1] and rep.stats["call_hits"] == 1
    assert source_versions.get_source_versions is fake_versions
