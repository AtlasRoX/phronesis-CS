"""The draft loop repairs only the sections a gap round's new evidence touches (repair item 12), each section call
admitted against the run budget and sent exactly its allocation; it falls back to a whole rewrite otherwise."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

from phronesis.agentic.budget import RunBudget
from phronesis.agents.orchestration import agent_rounds

DRAFT = (
    "**Bottom line**\nSotorasib has activity in KRAS G12C NSCLC [PMID:00000001].\n\n"
    "### Efficacy\nMedian OS was 10.2 months [PMID:00000001].\n\n"
    "### Correction status\nWhether a correction exists was not retrieved in this run.\n\n"
    "### Safety\nGrade 3 diarrhoea occurred in 5% [PMID:00000001].\n"
)
HEDGED = "Whether a correction exists was not retrieved in this run."


def _gap_report(hedged: bool) -> Dict[str, Any]:
    return {
        "round": 0,
        "items": [{"id": "h1" if hedged else "q2", "text": HEDGED if hedged else "Is there a correction?", "hedged": hedged}],
        "outcomes": {"h1" if hedged else "q2": "filled"},
        "new_evidence_keys": ["PMID:00000009"],
    }


class FakeLLM:
    def __init__(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens


def _run(hedged: bool, sent_caps: List[int], prompts: List[str]):
    budget = RunBudget(max_tokens=60_000)
    state = SimpleNamespace(budget=budget, bag={"gap_report": [_gap_report(hedged)]}, evidence_labels={}, evidence_sections=[])

    async def stream(llm, messages, parts, **_kw):
        sent_caps.append(llm.max_tokens)
        user = messages[-1].content
        prompts.append(user)
        heading = user.rsplit("exact heading line: ", 1)[-1].strip()
        body = ("An erratum exists [PMID:00000009]; its text was not retrieved." if "Correction" in heading
                else "Sotorasib has activity in KRAS G12C NSCLC; an erratum exists [PMID:00000001] [PMID:00000009].")
        return f"{heading}\n{body}\n", ""

    ao = SimpleNamespace(
        get_llm=lambda **kw: FakeLLM(kw["max_tokens"]),
        _edit_llm_kwargs=lambda _m: {},
        stream_buffered_with_retry=stream,
        render_agent_evidence=lambda *_a: ['<document pmid="00000009">Erratum to the sotorasib study.</document>'],
    )

    async def watched(_step: str, coro):
        return await coro

    run = SimpleNamespace(
        agent_state=state, synthesis_model="m", api_key=None, tool_results={}, synthesis_prompt="[PMID:00000001]",
        orch=SimpleNamespace(_sanitize_answer=lambda t: t), watched=watched,
    )
    return run, ao, budget


def test_a_filled_hedged_gap_repairs_only_its_section_and_the_summary(monkeypatch):
    caps: List[int] = []
    prompts: List[str] = []
    run, ao, budget = _run(hedged=True, sent_caps=caps, prompts=prompts)
    monkeypatch.setattr(agent_rounds, "orchestrator_module", lambda: ao)
    feedback = ["Gap re-retrieval (round 1) added new evidence for: ... Use the new gap-search documents ..."]
    out = asyncio.run(agent_rounds._try_section_repair(run, feedback, DRAFT, "synthesis_round_1"))
    assert out is not None
    new_md, rep = out
    assert set(rep.applied) == {"correction-status", "bottom-line"}
    # Untouched sections are byte-identical.
    assert "### Efficacy\nMedian OS was 10.2 months [PMID:00000001].\n\n" in new_md
    assert "### Safety\nGrade 3 diarrhoea occurred in 5% [PMID:00000001].\n" in new_md
    assert "An erratum exists [PMID:00000009]" in new_md
    # Two section calls, each sent its own admitted cap (never the global remainder) and settled afterwards.
    assert len(caps) == 2 and all(0 < c <= 1200 for c in caps)
    assert budget.llm_calls == 2
    assert run.agent_state.bag["answer_version"].to_dict()["answer_version"] >= 1


def test_a_filled_sub_question_gap_falls_back_to_a_whole_rewrite(monkeypatch):
    caps: List[int] = []
    run, ao, _budget = _run(hedged=False, sent_caps=caps, prompts=[])
    monkeypatch.setattr(agent_rounds, "orchestrator_module", lambda: ao)
    feedback = ["Gap re-retrieval (round 1) added new evidence for: Is there a correction? Use the new gap-search documents ..."]
    assert asyncio.run(agent_rounds._try_section_repair(run, feedback, DRAFT, "synthesis_round_1")) is None
    assert caps == []  # nothing generated: the caller runs its whole-answer rewrite instead


def test_a_filled_sub_question_is_placed_on_its_matching_section(monkeypatch):
    # The live run's correction sub-question: its new evidence belongs in "Correction status", not a whole rewrite.
    caps: List[int] = []
    run, ao, _budget = _run(hedged=False, sent_caps=caps, prompts=[])
    run.agent_state.bag["gap_report"][0]["items"][0]["text"] = (
        "Determine whether a correction exists for PMID 39369609 and whether corrected content was applied."
    )
    monkeypatch.setattr(agent_rounds, "orchestrator_module", lambda: ao)
    feedback = ["Gap re-retrieval (round 1) added new evidence for: Determine whether a correction exists ..."]
    out = asyncio.run(agent_rounds._try_section_repair(run, feedback, DRAFT, "synthesis_round_1"))
    assert out is not None
    _new_md, rep = out
    assert set(rep.applied) == {"correction-status", "bottom-line"} and len(caps) == 2
