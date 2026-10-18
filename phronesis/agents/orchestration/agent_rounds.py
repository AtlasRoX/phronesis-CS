"""Agentic phases around the pipeline: follow-up work on the tool results (augment), the bounded draft loop
(gap re-retrieval, critique/debate, steering, rewrites) and the finalize phases. Classic runs skip all of them."""

from __future__ import annotations

import difflib
import os
import re
import time
from typing import Any, List, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage

from ...agentic.budget import Allocation, BudgetExhausted, RunDeadlineExceeded, estimate_tokens
from ...agentic.control import RunCancelled
from . import draft as draft_stage
from . import section_repair
from .context import RunContext, logger, orchestrator_module

_GAP_FEEDBACK_PREFIX = "Gap re-retrieval"
_SECTION_EVIDENCE_CHARS = 8000


def _configured_rewrite_cap() -> int:
    try:
        return max(1, int(os.getenv("PHRONESIS_REWRITE_MAX_OUTPUT_TOKENS", "") or 8192))
    except ValueError:
        return 8192


def rewrite_output_cap(budget: object, prompt_chars: int) -> int:
    """Most output tokens one rewrite may produce: PHRONESIS_REWRITE_MAX_OUTPUT_TOKENS (default 8192), never more than
    the run budget has left after the prompt (0 when nothing is left; there is no floor above what fits, audit F3)."""
    left = int(getattr(budget, "remaining_tokens")()) - max(1, estimate_tokens(prompt_chars))
    return max(0, min(_configured_rewrite_cap(), left))


def generation_allocation(
    budget: Any, phase: str, prompt_chars: int, draft_chars: int
) -> Tuple[Optional[Allocation], str]:
    """Admit one whole-answer generation (a rewrite or the verifier's revision) or say why not.

    The model is sent exactly the admitted output allocation. A rewrite reproduces the whole draft, so an allocation
    smaller than the draft (plus room for its edits) would only buy a predictably truncated answer: then nothing is
    charged, the previous draft stays, and the reason is returned. "synthesis" rewrites are optional work and cannot
    use the finalization reserve; the "revision" may."""
    configured = _configured_rewrite_cap()
    needed = min(configured, max(512, int(estimate_tokens(draft_chars) * 1.1) + 128))
    try:
        return budget.admit(phase, prompt_chars, configured, min_output=needed), ""
    except BudgetExhausted as exc:
        return None, (
            f"{exc}; a whole-answer rewrite needs about {needed} output tokens, so a shorter one would be cut off"
        )


def change_ratio(old: str, new: str) -> float:
    """How much of the text a rewrite changed (0 = identical, 1 = nothing in common), by difflib on words."""
    a, b = (old or "").split(), (new or "").split()
    if not a and not b:
        return 0.0
    return round(1.0 - difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 3)


_PLACE_STOP = frozenset(
    "about after also answer before between could determine does each from have into more must only other paper "
    "report should study than that their them there these this those what when where whether which while with "
    "would retrieve retrieved exact".split()
)


def _place_terms(text: str) -> set:
    return {w for w in re.findall(r"[a-z][a-z0-9\-]{3,}", (text or "").lower()) if w not in _PLACE_STOP}


def _section_for(item_text: str, sections: List[Any]) -> Optional[Any]:
    """The one section a sub-question's new evidence belongs in: the best match on heading and body terms, with at
    least one heading term and two terms in all; None when no section, or more than one, fits that well."""
    want = _place_terms(item_text)
    scored = []
    for sec in sections:
        if sec.is_summary or not sec.heading:
            continue
        title_hits = len(want & _place_terms(sec.title))
        hits = len(want & _place_terms(f"{sec.title} {sec.body}"))
        if title_hits >= 1 and hits >= 2:
            scored.append((title_hits, hits, sec))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    if not scored or (len(scored) > 1 and scored[0][:2] == scored[1][:2]):
        return None
    return scored[0][2]


def _repair_targets(agent_state: Any, feedback: List[str], sections: List[Any]) -> Tuple[List[str], List[str], List[str]]:
    """(feedback the section matcher can place, draft sentences to repair, new evidence keys) for this round.

    A gap round's feedback names no section, but the gaps it filled do: a hedged draft sentence is itself the text
    that changes, and a filled sub-question belongs in the section whose heading and body match it (its first
    sentence marks it). When any filled gap cannot be placed, the gap feedback is kept and the matcher falls back to
    a whole-answer rewrite."""
    reports = agent_state.bag.get("gap_report") or []
    report = reports[-1] if reports else {}
    new_keys = list(report.get("new_evidence_keys") or [])
    items = {it.get("id"): it for it in report.get("items") or []}
    filled = [items[i] for i, o in (report.get("outcomes") or {}).items() if o == "filled" and i in items]
    sentences: List[str] = []
    placeable = bool(filled)
    for it in filled:
        if it.get("hedged"):
            sentences.append(str(it.get("text") or ""))
            continue
        sec = _section_for(str(it.get("text") or ""), sections)
        first = re.split(r"(?<=[.!?])\s", sec.body.strip(), maxsplit=1)[0] if sec is not None else ""
        if not first.strip():
            placeable = False
            continue
        sentences.append(first.strip())
    kept = [f for f in feedback if not (placeable and str(f).startswith(_GAP_FEEDBACK_PREFIX))]
    return kept, sentences, new_keys


async def _try_section_repair(run: RunContext, feedback: List[str], draft: str, round_id: str) -> Optional[Tuple[str, Any]]:
    """Repair only the sections this round's evidence or critique touches. None: fall back to a whole rewrite."""
    ao = orchestrator_module()
    agent_state = run.agent_state
    budget = agent_state.budget
    sections = section_repair.split_sections(draft)
    kept, sentences, new_keys = _repair_targets(agent_state, feedback, sections)
    decision = section_repair.affected_sections(sections, kept, new_keys, sentences)
    if decision.mode != "targeted":
        return None
    try:
        # The agent's own evidence (gap searches, planned calls) last: the newest documents are what changed.
        rendered = ao.render_agent_evidence(run.tool_results, agent_state.evidence_labels, agent_state.evidence_sections)
        excerpt = "\n".join(rendered)[-_SECTION_EVIDENCE_CHARS:]
    except Exception:  # noqa: BLE001
        excerpt = ""
    allowed = (
        section_repair.citation_keys(run.synthesis_prompt or "")
        | section_repair.citation_keys(draft)
        | section_repair.citation_keys(excerpt)
        | {section_repair.normalize_key(k) for k in new_keys}
    )
    version = agent_state.bag.get("answer_version")
    if version is None:
        version = agent_state.bag["answer_version"] = section_repair.AnswerVersion.start(draft, allowed)

    async def generate(system: str, user: str, max_tokens: int) -> str:
        # Each section is its own admitted call (optional pool), sent exactly its allocation and watched for the
        # run's cancel and deadline.
        chars = len(system) + len(user)
        alloc = budget.admit("synthesis", chars, max_tokens, min_output=min(max_tokens, 256))
        parts: List[str] = []
        try:
            llm = ao.get_llm(model_name=run.synthesis_model, api_key=run.api_key, max_tokens=alloc.output_cap,
                             **ao._edit_llm_kwargs(run.synthesis_model))
            text, reasoning = await run.watched(
                round_id,
                ao.stream_buffered_with_retry(
                    llm, [SystemMessage(content=system), HumanMessage(content=user)], parts, attempts=1,
                    timeout_s=budget.remaining_seconds() if budget.time_limited else None,
                ),
            )
        except BaseException:
            budget.settle(alloc, output_chars=len("".join(parts)), failed=True)
            raise
        budget.settle(alloc, output_chars=len(text or "".join(parts)) + len(reasoning or ""))
        return run.orch._sanitize_answer(text or "".join(parts))

    per_section = min(1200, _configured_rewrite_cap())
    new_md, report = await section_repair.repair_sections(
        draft, kept, generate, new_evidence_keys=new_keys, unsupported_claims=sentences, allowed_keys=allowed,
        evidence_excerpt=excerpt, max_parallel=2, per_section_max_tokens=per_section, version=version,
    )
    if report.mode != "targeted" or not report.changed:
        return None
    return new_md, report


async def augment(run: RunContext) -> None:
    """Agentic follow-up work on the pipeline's results (planned tool DAG / specialists, hypotheses,
    counter-evidence)."""
    agent_rt, agent_state = run.agent_rt, run.agent_state
    if agent_rt is not None and agent_state is not None:
        agent_state.control.checkpoint()
        await agent_rt.run_stage("augment")
        agent_state.control.checkpoint()


async def draft_rounds(run: RunContext) -> None:
    """The agentic draft loop: rewrite the answer while a phase asks for it, within the run budget."""
    ao = orchestrator_module()
    orch = run.orch
    agent_rt, agent_state, api_key = run.agent_rt, run.agent_state, run.api_key
    emit, send, on_reasoning = run.emit, run.send, run.on_reasoning
    llm_ok, synthesis_model, system_prompt = run.llm_ok, run.synthesis_model, run.system_prompt
    synthesis_prompt, candidate_response, streamed_draft = run.synthesis_prompt, run.candidate_response, run.streamed_draft

    if agent_rt is not None and agent_state is not None and llm_ok:
        budget = agent_state.budget
        while agent_state.draft_round < budget.max_draft_rounds:
            agent_state.control.checkpoint()
            # Optional checks stop at the finalization reserve: what is left writes and verifies the answer.
            if budget.optional_exhausted():
                agent_state.bag["draft_loop_stopped"] = budget.stop_reason()
                break
            agent_state.draft = candidate_response
            post = await agent_rt.run_stage("after_draft")
            steering = agent_state.control.drain_steering()
            if steering:
                agent_state.bag.setdefault("steering", []).extend(steering)
                post.resynthesize = True
            if not post.resynthesize or budget.exhausted():
                break
            agent_state.draft_round += 1
            round_id = f"synthesis_round_{agent_state.draft_round}"
            t0 = time.perf_counter()
            stage_status = "ok"
            # Targeted repair first: only the sections the new evidence or critique touches are regenerated.
            if post.feedback and not steering:
                await emit(round_id, "Repairing the affected sections", status="running", extra={"model": synthesis_model})
                try:
                    repaired = await _try_section_repair(run, list(post.feedback), candidate_response, round_id)
                except RunCancelled:
                    run.note_pipeline_stage(f"rewrite_{agent_state.draft_round}", time.perf_counter() - t0,
                                            "cancelled", at=t0 - run.start_time)
                    raise
                except Exception as repair_err:  # noqa: BLE001
                    logger.warning("Section repair failed; falling back to a whole rewrite: %s", repair_err)
                    repaired = None
                if repaired is not None:
                    new_md, rep = repaired
                    ratio = change_ratio(candidate_response, new_md)
                    candidate_response = streamed_draft = new_md
                    await send({"type": "content_reset", "reason": "agent_round"})
                    await send({"type": "content_delta", "delta": candidate_response})
                    await emit(
                        round_id,
                        f"Repaired {len(rep.applied)} section(s) with the new evidence",
                        f"{', '.join(rep.applied)[:200]} · {round(ratio * 100)}% of the text changed"
                        + (f" · {len(rep.rejected)} replacement(s) rejected" if rep.rejected else ""),
                        extra={"repair": rep.to_dict(), "change_ratio": ratio, "unchanged": False},
                    )
                    run.note_pipeline_stage(f"rewrite_{agent_state.draft_round}", time.perf_counter() - t0, "ok",
                                            at=t0 - run.start_time)
                    continue
            round_prompt = draft_stage.build_prompt(run, post.feedback, candidate_response if post.feedback else "")
            prompt_chars = len(system_prompt) + len(round_prompt)
            # Admitted before anything is shown: a rewrite the budget cannot afford in full is not started (audit F3).
            alloc, skip_reason = generation_allocation(budget, "synthesis", prompt_chars, len(candidate_response))
            if alloc is None:
                await emit(round_id, "Skipped the rewrite: not enough budget for a complete one", skip_reason[:300])
                agent_state.bag["draft_loop_stopped"] = skip_reason
                run.note_pipeline_stage(f"rewrite_{agent_state.draft_round}", 0.0, "skipped", at=t0 - run.start_time)
                break
            synthesis_prompt = round_prompt
            await emit(round_id, "Rewriting the answer with the new evidence", status="running", extra={"model": synthesis_model})
            unchanged = False
            round_parts: List[str] = []
            holder = {"alloc": alloc}
            cap = alloc.output_cap
            try:
                from ...agentic.config import get_effort_profile
                effort_prof = get_effort_profile(agent_state.effort_level)
                round_llm = ao.get_llm(
                    model_name=synthesis_model,
                    api_key=api_key,
                    reasoning_effort=effort_prof.get("reasoning_effort"),
                    max_tokens=cap,
                    **ao._edit_llm_kwargs(synthesis_model),
                )

                def before_retry(_attempt: int) -> None:
                    # A retried stream is a second provider call: settle the dropped one, admit the retry.
                    budget.settle(holder["alloc"], output_chars=len("".join(round_parts)), failed=True)
                    retry, why = generation_allocation(budget, "synthesis", prompt_chars, len(candidate_response))
                    if retry is None or retry.output_cap < cap:
                        if retry is not None:
                            budget.settle(retry, failed=True)
                        raise BudgetExhausted(why or "no budget left to retry the rewrite")
                    holder["alloc"] = retry

                round_text, round_reasoning = await run.watched(
                    round_id,
                    ao.stream_buffered_with_retry(
                        round_llm,
                        [SystemMessage(content=system_prompt), HumanMessage(content=synthesis_prompt)],
                        round_parts,
                        on_reasoning=on_reasoning,
                        before_retry=before_retry,
                        timeout_s=budget.remaining_seconds() if budget.time_limited else None,
                    ),
                )
                # AGT-010: the rewrite's output (and its reasoning) counts against the token budget; its admitted
                # allocation is replaced by what it actually wrote.
                budget.settle(holder["alloc"], output_chars=len(round_text or "".join(round_parts)) + len(round_reasoning or ""))
                rewritten = orch._sanitize_answer(round_text or "".join(round_parts))
                ratio = change_ratio(candidate_response, rewritten) if rewritten.strip() else 0.0
                # Any edit is kept (a minimal edit may touch one sentence); only an identical text is "unchanged".
                unchanged = bool(rewritten.strip()) and rewritten.strip() == candidate_response.strip()
                if rewritten.strip():
                    candidate_response = streamed_draft = rewritten
                    await send({"type": "content_reset", "reason": "agent_round"})
                    await send({"type": "content_delta", "delta": candidate_response})
                await emit(
                    round_id,
                    "Rewrite changed nothing; keeping the previous draft" if unchanged
                    else "Rewrote the answer with the new evidence",
                    f"{len(candidate_response.split())} words · {round(ratio * 100)}% of the text changed",
                    extra={"change_ratio": ratio, "unchanged": unchanged, "max_output_tokens": cap},
                )
            except RunCancelled:
                stage_status = "cancelled"
                budget.settle(holder["alloc"], output_chars=len("".join(round_parts)), failed=True)
                raise
            except BaseException as round_err:  # noqa: BLE001
                # Failed, deadline or lost client: keep the prompt charge, release the unused allocation.
                budget.settle(holder["alloc"], output_chars=len("".join(round_parts)), failed=True)
                if not isinstance(round_err, Exception):
                    stage_status = "cancelled"
                    raise
                stage_status = "deadline" if isinstance(round_err, RunDeadlineExceeded) else "error"
                logger.warning("Agent synthesis round failed: %s", round_err)
                await emit(
                    round_id,
                    "Rewrite failed; keeping the previous draft",
                    f"{type(round_err).__name__}: {str(round_err)[:200]}",
                    status="error",
                )
                break
            finally:
                run.note_pipeline_stage(
                    f"rewrite_{agent_state.draft_round}", time.perf_counter() - t0, stage_status, at=t0 - run.start_time
                )
            if unchanged:
                break  # a round that changes nothing will not be improved by another one
    run.synthesis_prompt, run.candidate_response, run.streamed_draft = synthesis_prompt, candidate_response, streamed_draft


async def finalize_phases(run: RunContext) -> None:
    """The agentic finalize phases over the checked answer; their artifacts join the run's."""
    agent_rt, agent_state, framework = run.agent_rt, run.agent_state, run.framework
    verification_report, candidate_response, citation_map = run.verification_report, run.candidate_response, run.citation_map
    merge_artifact, record = run.merge_artifact, run.record

    if agent_rt is not None and agent_state is not None:
        agent_state.verification = verification_report
        agent_state.draft = candidate_response
        agent_state.bag["citation_map"] = citation_map
        try:
            await agent_rt.run_stage("finalize")
        except RunCancelled:
            pass  # the answer is already written; cancelling now only skips the remaining finalize phases
        version = agent_state.bag.get("answer_version")
        if version is not None:
            # The answer was verified after its last repair: the versions now describe the checked text.
            version.mark_verified()
            merge_artifact("answer_version", version.to_dict())
        for key, value in agent_state.artifacts.items():
            merge_artifact(key, value)
        merge_artifact("agent_run", agent_state.summary())
        if run.routing is not None:
            merge_artifact("framework_routing", run.routing)
        record("note_step", "agent_run", {"framework": framework}, agent_state.summary())
