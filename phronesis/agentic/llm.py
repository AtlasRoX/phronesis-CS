"""
Model access for agentic phases: one budget-charged call, JSON extraction and schema validation.

The factory is injected (the orchestrator passes its own get_llm), so offline tests that disable the model
disable every agentic model call too: AgentLLM.available turns False after the first factory failure and
phases fall back to their deterministic paths.
"""

from __future__ import annotations

import asyncio
import os
import json
import logging
import re
from typing import Any, Callable, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from .budget import Allocation, BudgetExhausted, RunBudget, RunDeadlineExceeded, estimate_tokens  # noqa: F401

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


class AgentLLMUnavailable(RuntimeError):
    pass


def strip_reasoning(text: str) -> str:
    text = _THINK_RE.sub("", text or "")
    if "</think>" in text:  # an unterminated opening tag was cut off by the provider
        text = text.split("</think>", 1)[1]
    return text.strip()


def _clean_and_load(s: str) -> Any:
    s = s.strip()
    if not s:
        raise ValueError("empty string")
    try:
        return json.loads(s, strict=False)
    except (TypeError, ValueError):
        pass
    cleaned = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        return json.loads(cleaned, strict=False)
    except (TypeError, ValueError):
        pass
    try:
        import ast

        val = ast.literal_eval(cleaned)
        if isinstance(val, (dict, list)):
            return val
    except Exception:
        pass
    raise ValueError("not valid json")


def extract_json(text: str) -> Any:
    """The first JSON object or array in a model reply (fenced or bare); raises ValueError if there is none."""
    text = strip_reasoning(text)
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text]
    for cand in candidates:
        cand = cand.strip()
        try:
            return _clean_and_load(cand)
        except ValueError:
            pass
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            start = cand.find(open_ch)
            if start < 0:
                continue
            depth, in_str, esc = 0, False, False
            for i in range(start, len(cand)):
                ch = cand[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == open_ch:
                    depth += 1
                elif ch == close_ch:
                    depth -= 1
                    if depth == 0:
                        try:
                            return _clean_and_load(cand[start : i + 1])
                        except ValueError:
                            # Continue looking instead of aborting the entire search
                            pass
    raise ValueError("no JSON value found in the model reply")


def agent_model_kwargs(model_name: str) -> dict:
    """Extra client options for the agent's short structured calls (plans, critiques, gap checks, JSON verdicts).

    Nemotron 3 reasons before answering by default; for these calls that roughly halves throughput for no gain in
    the JSON (measured 17.1 s -> 7.5 s and 11.0 s -> 8.1 s on planning/critique prompts), and a slow planner reply
    was hitting the 90 s call limit. The final answer is written elsewhere and keeps its reasoning.
    PHRONESIS_AGENT_THINKING=on keeps reasoning for agent calls too.
    """
    if os.getenv("PHRONESIS_AGENT_THINKING", "off").strip().lower() in ("1", "on", "true", "yes"):
        return {}
    name = (model_name or "").lower()
    if "nemotron" in name:
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    return {}


# A max_tokens at or above this is not sent: providers reject values above the model's output limit.
OUTPUT_CAP_BIND_LIMIT = 16_384


def output_reserve_tokens() -> int:
    """Output tokens allocated per agent call when it is admitted (PHRONESIS_AGENT_OUTPUT_RESERVE_TOKENS, default
    4096, never above PHRONESIS_AGENT_MAX_OUTPUT_TOKENS). It is also the call's max_tokens, so it must cover the
    longest JSON reply (plans, hypotheses) and, with PHRONESIS_AGENT_THINKING=on, the model's reasoning."""
    try:
        value = int(os.getenv("PHRONESIS_AGENT_OUTPUT_RESERVE_TOKENS", "") or 4096)
    except ValueError:
        value = 4096
    configured = _env_output_cap()
    return max(1, min(value, configured) if configured else value)


def _env_output_cap() -> int:
    """PHRONESIS_AGENT_MAX_OUTPUT_TOKENS: an explicit per-reply cap for agent calls (0/unset = budget-derived only)."""
    try:
        value = int(os.getenv("PHRONESIS_AGENT_MAX_OUTPUT_TOKENS", "") or 0)
    except ValueError:
        return 0
    return max(0, value)


def provider_usage(reply: Any) -> Optional[tuple]:
    """(input_tokens, output_tokens) the provider reported for one reply, or None.

    Reads LangChain's `usage_metadata` (input/output tokens; reasoning tokens are part of output_tokens) and
    falls back to the OpenAI-style `response_metadata["token_usage"]`."""
    meta = getattr(reply, "usage_metadata", None)
    if isinstance(meta, dict):
        i, o = meta.get("input_tokens"), meta.get("output_tokens")
        if isinstance(i, int) and isinstance(o, int) and (i or o):
            return i, o
    usage = (getattr(reply, "response_metadata", None) or {}).get("token_usage") if hasattr(reply, "response_metadata") else None
    if isinstance(usage, dict):
        i, o = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if isinstance(i, int) and isinstance(o, int) and (i or o):
            return i, o
    return None


def reasoning_chars(reply: Any) -> int:
    """Characters of reasoning text a reply carried outside its content (OpenAI-compatible reasoning_content)."""
    extra = getattr(reply, "additional_kwargs", None)
    if isinstance(extra, dict):
        r = extra.get("reasoning_content") or extra.get("reasoning")
        if isinstance(r, str):
            return len(r)
    return 0


def charge_reply(
    budget: RunBudget, phase: str, reply: Any, raw_text: str, estimated_prompt: int, reserved_output: int = 0
) -> None:
    """Charge one reply: the provider's reported usage when present, else the raw reply (reasoning included).

    For calls charged with charge_llm (no per-call allocation; see settle_reply for admitted calls).
    estimated_prompt is what charge_llm returned (prompt estimate plus the output it reserved); reserved_output is
    the reserved part, released when the reply is charged from its length."""
    usage = provider_usage(reply)
    if usage is not None:
        budget.record_usage(phase, estimated_prompt=estimated_prompt, input_tokens=usage[0], output_tokens=usage[1])
    else:
        budget.add_output_tokens(phase, len(raw_text or "") + reasoning_chars(reply), reserved=reserved_output)


def settle_reply(budget: RunBudget, alloc: Allocation, reply: Any, raw_text: str) -> None:
    """Settle an admitted call: the provider's reported usage when present, else the raw reply's length."""
    usage = provider_usage(reply)
    if usage is not None:
        budget.settle(alloc, input_tokens=usage[0], output_tokens=usage[1])
    else:
        budget.settle(alloc, output_chars=len(raw_text or "") + reasoning_chars(reply))


class AgentLLM:
    """Budget-charged access to the planning model for one run."""

    def __init__(
        self,
        factory: Optional[Callable[..., Any]],
        model_name: str,
        api_key: Optional[str],
        budget: RunBudget,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self._factory = factory
        self.model_name = model_name
        self._api_key = api_key
        self.budget = budget
        # One agent model call (a plan, critique or JSON verdict). 180 s by default: runs have no time limit, and a
        # long planning prompt was hitting the old 90 s cap.
        self.timeout_seconds = timeout_seconds or float(os.getenv("PHRONESIS_AGENT_CALL_TIMEOUT", "180"))
        self._llm: Any = None
        self._unavailable: Optional[str] = None if factory is not None else "no model factory"

    @property
    def available(self) -> bool:
        if self._unavailable:
            return False
        if self._llm is None:
            try:
                # The client timeout must not undercut this wrapper's own wait, or every slow reply dies early.
                self._llm = self._factory(  # type: ignore[misc]
                    model_name=self.model_name, api_key=self._api_key, timeout=self.timeout_seconds,
                    **agent_model_kwargs(self.model_name),
                )
            except Exception as exc:  # noqa: BLE001
                self._unavailable = f"{type(exc).__name__}: {str(exc)[:160]}"
                logger.info("Agent model unavailable: %s", self._unavailable)
                return False
        return True

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable or ""

    def _invoke_kwargs(self, cap: int) -> dict:
        """max_tokens for this reply: exactly the output allocation the budget admitted for this call (audit F2),
        never the global remainder. Passed only to LangChain chat models, which forward it to the provider."""
        try:
            from langchain_core.language_models import BaseChatModel
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(self._llm, BaseChatModel):
            return {}
        return {"max_tokens": max(1, int(cap))}

    async def complete(
        self,
        system: str,
        user: str,
        *,
        phase: str = "agent",
        timeout: Optional[float] = None,
        max_output: Optional[int] = None,
    ) -> str:
        if not self.available:
            raise AgentLLMUnavailable(self._unavailable or "model unavailable")
        prompt_chars = len(system) + len(user)
        want = max_output if max_output is not None else output_reserve_tokens()
        configured = _env_output_cap()
        if configured:
            want = min(want, configured)
        # The provider is sent this call's own allocation; a call that cannot get one is refused (BudgetExhausted).
        alloc = self.budget.admit(phase, prompt_chars, want, min_output=min(256, want))
        from langchain_core.messages import HumanMessage, SystemMessage

        wait = min(timeout or self.timeout_seconds, max(1.0, self.budget.remaining_seconds()))
        try:
            reply = await asyncio.wait_for(
                self._llm.ainvoke(
                    [SystemMessage(content=system), HumanMessage(content=user)], **self._invoke_kwargs(alloc.output_cap)
                ),
                timeout=wait,
            )
        except asyncio.TimeoutError as exc:
            self.budget.settle(alloc, failed=True)
            if self.budget.time_limited and self.budget.remaining_seconds() <= 0:
                self.budget.exhausted()  # records the time-budget reason
                raise RunDeadlineExceeded(self.budget.exhausted_reason or "time budget used") from exc
            raise AgentLLMUnavailable(f"model call timed out after {wait:.0f}s") from exc
        except BaseException as exc:
            # Failed, cancelled (lost client, run cancel) or interrupted: the prompt stays charged, the unused output
            # allocation is released, and no usage is invented.
            self.budget.settle(alloc, failed=True)
            if isinstance(exc, (BudgetExhausted, asyncio.CancelledError)) or not isinstance(exc, Exception):
                raise
            # Client-side failures (openai.APITimeoutError, connection resets, 5xx) must reach the phases as
            # AgentLLMUnavailable so their deterministic fallbacks run instead of the whole phase failing.
            raise AgentLLMUnavailable(f"{type(exc).__name__}: {str(exc)[:160]}") from exc
        content = getattr(reply, "content", reply)
        if isinstance(content, list):  # content blocks
            content = "".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content)
        raw = str(content or "")
        settle_reply(self.budget, alloc, reply, raw)
        return strip_reasoning(raw)

    async def json(
        self,
        system: str,
        user: str,
        *,
        schema: Optional[Type[T]] = None,
        phase: str = "agent",
        retries: int = 1,
    ) -> Any:
        """A parsed JSON reply, validated against schema when given; one repair retry on a malformed reply."""
        prompt = user
        last_err: Optional[Exception] = None
        for _ in range(retries + 1):
            text = await self.complete(system, prompt, phase=phase)
            try:
                value = extract_json(text)
                return schema.model_validate(value) if schema is not None else value
            except (ValueError, ValidationError) as exc:
                last_err = exc
                prompt = (
                    f"{user}\n\nYour previous reply could not be parsed ({str(exc)[:300]}). "
                    "Reply again with only the JSON value, no prose."
                )
        raise ValueError(f"model reply was not valid JSON after {retries + 1} attempts: {last_err}")


__all__ = ["AgentLLM", "AgentLLMUnavailable", "BudgetExhausted", "extract_json", "strip_reasoning"]
