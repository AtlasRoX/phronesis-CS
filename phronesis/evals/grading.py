"""
Model-based grading, used only where a rule cannot decide:
- extract_label: which option (yes/no/maybe, or a multiple-choice option) a free-text answer commits to. The
  deterministic heuristics in scoring.py run first; the model is asked only when they cannot tell.
- judge: a rubric score (1-5) of an answer against the question and, when the case has one, a reference answer.

Both return None when no model is available, so a run never fails because grading could not be done.
The judge sees only the question, the reference and the answer, never the expected labels of other checks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

LLMFactory = Callable[..., Any]

_EXTRACT_SYSTEM = (
    "You grade a research assistant's answer to a question with fixed options. Decide which single option the answer "
    "commits to. If it does not commit to any option, or says the evidence is insufficient, choose the "
    'insufficient option if one is listed, otherwise "none". Reply with JSON only: {"choice": "<option text exactly>"}'
)

JUDGE_DIMENSIONS = ("correctness", "grounding", "completeness", "calibration", "clarity")
_JUDGE_SYSTEM = (
    "You are a senior clinician-scientist grading an AI research assistant's answer. Score each dimension from 1 "
    "(poor) to 5 (excellent):\n"
    "- correctness: agreement with established knowledge and with the reference answer when one is given\n"
    "- grounding: specific claims are tied to cited sources; no invented studies, numbers or identifiers\n"
    "- completeness: covers what an expert would consider essential for this question\n"
    "- calibration: certainty matches the evidence; gaps and limitations are acknowledged without over-hedging\n"
    "- clarity: direct, well organised, appropriately concise\n"
    "Every identifier in <verified_sources> was checked against the retrieved literature and exists, even if it is "
    "newer than your knowledge; never call it fabricated. Judge grounding by whether claims are tied to those "
    "sources and accurately reflect them, and treat an identifier cited in the answer but absent from "
    "<verified_sources> as unsupported.\n"
    "Judge the answer as written; do not reward length. Reply with JSON only: "
    '{"correctness": n, "grounding": n, "completeness": n, "calibration": n, "clarity": n, "overall": n, '
    '"rationale": "<two sentences>"}'
)


def _strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    return text[text.lower().rfind("</think>") + 8:] if "</think>" in text.lower() else text


def _first_json(text: str) -> Optional[Dict[str, Any]]:
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text or ""):
        try:
            obj, _ = decoder.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


async def _ask(llm_factory: Optional[LLMFactory], model_name: Optional[str], system: str, user: str, timeout_s: float) -> Optional[Dict[str, Any]]:
    if llm_factory is None:
        return None
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        # The client's own request timeout (LLM_TIMEOUT, 60 s by default) would cut a slow judge off first.
        llm = llm_factory(model_name=model_name, streaming=False, timeout=timeout_s)
        reply = await asyncio.wait_for(llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user)]), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001
        logger.info("Grader model unavailable: %s", exc)
        return None
    content = getattr(reply, "content", reply)
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return _first_json(_strip_think(str(content)))


async def extract_label(
    question: str, answer: str, options: Sequence[str], *, llm_factory: Optional[LLMFactory], model_name: Optional[str] = None,
    timeout_s: float = 60.0,
) -> Optional[str]:
    """The option the answer commits to, matched back to the option list; None when undecidable."""
    listed = "\n".join(f"- {o}" for o in options)
    obj = await _ask(llm_factory, model_name, _EXTRACT_SYSTEM,
                     f"<question>{question}</question>\n<options>\n{listed}\n</options>\n<answer>\n{answer[:6000]}\n</answer>", timeout_s)
    choice = str((obj or {}).get("choice") or "").strip().lower()
    if not choice or choice == "none":
        return None
    for o in options:
        if choice == o.lower() or choice.strip(" .\"'") == o.lower():
            return o
    for o in options:  # tolerate a model that shortens or decorates the option
        if o.lower() in choice or choice in o.lower():
            return o
    return None


async def judge(
    question: str, answer: str, reference: Optional[str], *, llm_factory: Optional[LLMFactory], model_name: Optional[str] = None,
    sources: Sequence[str] = (), timeout_s: float = 180.0,
) -> Optional[Dict[str, Any]]:
    """Rubric scores {dimension: 1-5, overall, rationale}, or None when no model answered with valid scores.
    sources: the answer's verified citations ("PMID:123 | title | journal year"), so recent papers are not
    mistaken for fabrications."""
    ref = f"<reference_answer>\n{reference}\n</reference_answer>\n" if reference else ""
    listed = "\n".join(f"- {s}" for s in list(sources)[:40]) or "(none)"
    obj = await _ask(llm_factory, model_name, _JUDGE_SYSTEM,
                     f"<question>{question}</question>\n{ref}<verified_sources>\n{listed}\n</verified_sources>\n"
                     f"<answer>\n{answer[:8000]}\n</answer>", timeout_s)
    if not obj:
        return None
    scores: Dict[str, Any] = {}
    for dim in (*JUDGE_DIMENSIONS, "overall"):
        try:
            value = float(obj.get(dim))
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 5:
            scores[dim] = value
    if "overall" not in scores and all(d in scores for d in JUDGE_DIMENSIONS):
        scores["overall"] = round(sum(scores[d] for d in JUDGE_DIMENSIONS) / len(JUDGE_DIMENSIONS), 2)
    if "overall" not in scores:
        return None
    scores["rationale"] = str(obj.get("rationale") or "")[:600]
    return scores


__all__ = ["JUDGE_DIMENSIONS", "extract_label", "judge"]
