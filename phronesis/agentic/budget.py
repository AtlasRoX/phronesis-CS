"""
Per-run budget for agentic work: tool calls, model calls, estimated tokens, wall clock and loop rounds.

Every agentic tool or model call charges the budget first; a phase that finds the budget exhausted stops and
the answer is written from the evidence gathered so far (FrugalGPT-style cost control, Chen et al. 2023).
In agentic runs this includes the pipeline's own model calls: the first draft, each answer rewrite (and its stream
retry) and the verifier's revision are admitted like agent calls. The classic framework has no run budget, so its
calls are not ledgered; the router's feature call (router.py) and the classic query planner's call are charged
without a per-call output allocation.

Token accounting. Before a call the prompt is charged at an estimate (characters / 4, CHARS_PER_TOKEN) and the
call is refused when that estimate would exceed max_tokens. After the call the estimate is replaced by the
provider's reported usage (input + output tokens, reasoning included) when the reply carries it
(`record_usage`); otherwise the reply's characters, including any reasoning text, are charged at the same
estimate (`add_output_tokens`). AgentLLM also caps each reply at the tokens left (`output_token_cap`), so one
long response cannot overrun the budget by more than the provider's own rounding. Callers that make model calls
outside AgentLLM must charge and record them themselves. `tokens_estimated` in to_dict() keeps its name for
compatibility; `token_accounting` says how many calls were reconciled with provider usage.

Finalization reserve. Optional agent work (gap checks, critics, hypotheses, claim scoring, planning, answer rewrites)
may not spend the last `final_reserve_tokens` tokens or `final_reserve_calls` model calls: those are kept for the
first draft and the verifier's revision (FINAL_PHASES), so a run that runs out of budget still ends with a written,
checked answer. An optional call that would cross into the reserve raises BudgetExhausted and sets
`optional_exhausted_reason` (optional work stops) without setting `exhausted_reason` (the run is not finished).

Per-call admission (source audit 2026-10-18, F2). `admit()` charges a call's prompt estimate plus an output
allocation that fits in its pool (optional calls: before the reserve; final calls: the whole budget) and returns an
`Allocation` whose `output_cap` is the max_tokens the provider must be sent, never the global remainder, so
concurrent calls cannot overshoot together even when each reply uses its whole cap. `settle()` then replaces the
allocation with the provider's reported usage, or the reply's length when the provider reports none, and releases
what was not used; a failed, timed-out or cancelled call keeps its prompt charge (the provider read it) and releases
its output allocation; usage that is unknown is never invented. The ledger keeps provider-reported tokens
(`tokens_provider`) and character estimates (`tokens_from_estimates`) apart. `charge_llm` remains for callers that
cannot bind a cap (it does not return an allocation and cannot be settled per call).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .config import env_float, env_int


# What remaining_seconds() reports when the run has no time limit: finite, so callers can still use it in min() and
# as a timeout, but far beyond any real run.
UNLIMITED_SECONDS = 24 * 3600.0
CHARS_PER_TOKEN = 4  # the pre-call estimate; replaced by provider usage when the reply reports it
# Phases whose model calls may use the finalization reserve: the first draft and the verifier's revision. Answer
# rewrites ("synthesis") are optional improvements: they may not drain what the final checks need (audit F3).
FINAL_PHASES = frozenset({"first_draft", "revision"})


def estimate_tokens(chars: int) -> int:
    """Rough token count for `chars` characters of English/biomedical text (no tokeniser is bundled)."""
    return max(0, int(chars) // CHARS_PER_TOKEN)


class BudgetExhausted(RuntimeError):
    """Raised by charge_* when a limit would be exceeded."""


class RunDeadlineExceeded(BudgetExhausted):
    """The run's absolute wall-clock budget ran out while a call was in flight (the call was cancelled)."""


@dataclass
class Allocation:
    """One admitted model call: what it was charged and the exact output cap the provider must be sent."""

    phase: str
    final: bool
    input_tokens: int  # the prompt estimate charged at admission
    output_cap: int  # max_tokens for the provider; never more than the pool had left
    charged: int  # input_tokens + output_cap, held in the ledger until settle()
    settled: bool = False


@dataclass
class RunBudget:
    max_tool_calls: int = 40
    max_llm_calls: int = 40
    max_tokens: int = 250_000
    max_seconds: float = 0.0  # 0 = no wall-clock limit (the default); a positive value caps the run
    max_draft_rounds: int = 2  # extra synthesis rounds after the first draft (gap loop, debate)
    max_plan_nodes: int = 12
    max_replans: int = 1
    tool_calls: int = 0
    llm_calls: int = 0
    tokens: int = 0
    started: float = field(default_factory=time.perf_counter)
    exhausted_reason: str = ""
    by_phase: Dict[str, Dict[str, int]] = field(default_factory=dict)
    provider_usage_calls: int = 0  # calls whose token charge came from the provider's reported usage
    estimated_calls: int = 0  # calls charged from the character estimate only
    # Kept for FINAL_PHASES (see the module docstring); 0 = no reserve (the dataclass default; from_env sets one).
    final_reserve_tokens: int = 0
    final_reserve_calls: int = 0
    optional_exhausted_reason: str = ""
    reserved_open: int = 0  # output tokens admitted to calls still in flight
    tokens_provider: int = 0  # tokens charged from provider-reported usage
    tokens_from_estimates: int = 0  # tokens charged from character estimates (no usage reported)
    unreconciled_calls: int = 0  # failed/cancelled calls whose real usage is unknown (prompt estimate kept)

    @classmethod
    def from_env(cls, overrides: Optional[Dict[str, Any]] = None, effort_level: Optional[str] = None) -> "RunBudget":
        from .config import get_effort_profile

        profile = get_effort_profile(effort_level) if effort_level else {}
        base_tool_calls = profile.get("max_tool_calls", 40)
        base_llm_calls = profile.get("max_llm_calls", 40)
        base_seconds = profile.get("max_seconds", 0.0)
        base_draft_rounds = profile.get("max_draft_rounds", 2)

        b = cls(
            max_tool_calls=env_int("PHRONESIS_AGENT_MAX_TOOL_CALLS", base_tool_calls, 1, 200),
            max_llm_calls=env_int("PHRONESIS_AGENT_MAX_LLM_CALLS", base_llm_calls, 1, 120),
            max_tokens=env_int("PHRONESIS_AGENT_MAX_TOKENS", 250_000, 1_000, 2_000_000),
            max_seconds=env_float("PHRONESIS_AGENT_MAX_SECONDS", base_seconds, 0.0, 7_200.0),
            max_draft_rounds=env_int("PHRONESIS_AGENT_MAX_DRAFT_ROUNDS", base_draft_rounds, 0, 5),
            max_plan_nodes=env_int("PHRONESIS_AGENT_MAX_PLAN_NODES", 12, 1, 40),
            max_replans=env_int("PHRONESIS_AGENT_MAX_REPLANS", 1, 0, 3),
        )
        # Request overrides can only tighten the server limits, never raise them.
        for key, value in (overrides or {}).items():
            if not hasattr(b, key) or not key.startswith("max_") or value is None:
                continue
            current = getattr(b, key)
            try:
                cast = type(current)(value)
            except (TypeError, ValueError):
                continue
            if key == "max_seconds" and current <= 0:
                # No server time limit: a limit the user chose in settings is the only one.
                if cast > 0:
                    b.max_seconds = cast
                continue
            setattr(b, key, max(type(current)(0), min(current, cast)))
        # The reserve follows the (possibly tightened) token limit: 12% of it, at least 20k, never above 30%.
        default_reserve = min(max(int(b.max_tokens * 0.12), 20_000), int(b.max_tokens * 0.3))
        b.final_reserve_tokens = env_int("PHRONESIS_AGENT_FINAL_RESERVE_TOKENS", default_reserve, 0, b.max_tokens // 2)
        b.final_reserve_calls = env_int(
            "PHRONESIS_AGENT_FINAL_RESERVE_CALLS", 2 if b.max_llm_calls >= 6 else 0, 0, max(0, b.max_llm_calls - 1)
        )
        return b

    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    @property
    def time_limited(self) -> bool:
        return self.max_seconds > 0

    def remaining_seconds(self) -> float:
        if not self.time_limited:
            return UNLIMITED_SECONDS
        return max(0.0, self.max_seconds - self.elapsed())

    def exhausted(self) -> bool:
        if self.exhausted_reason:
            return True
        if self.time_limited and self.elapsed() >= self.max_seconds:
            self.exhausted_reason = f"time budget of {self.max_seconds:.0f}s used"
        elif self.tool_calls >= self.max_tool_calls:
            self.exhausted_reason = f"tool-call budget of {self.max_tool_calls} used"
        elif self.llm_calls >= self.max_llm_calls:
            self.exhausted_reason = f"model-call budget of {self.max_llm_calls} used"
        elif self.tokens >= self.max_tokens:
            self.exhausted_reason = f"token budget of {self.max_tokens} used"
        return bool(self.exhausted_reason)

    def optional_exhausted(self) -> bool:
        """True when optional agent work must stop: the run is exhausted, or only the finalization reserve is left."""
        if self.exhausted():
            return True
        if self.optional_exhausted_reason:
            return True
        if self.final_reserve_tokens and self.tokens >= self.max_tokens - self.final_reserve_tokens:
            self.optional_exhausted_reason = self._reserve_reason()
        elif self.final_reserve_calls and self.llm_calls >= self.max_llm_calls - self.final_reserve_calls:
            self.optional_exhausted_reason = (
                f"optional agent work stopped: the last {self.final_reserve_calls} of {self.max_llm_calls} model calls "
                "are kept for the final answer and its verification"
            )
        return bool(self.optional_exhausted_reason)

    def stop_reason(self) -> str:
        """Why agent work stopped early ("" when it did not): the exhausted limit, else the reserve."""
        return self.exhausted_reason or self.optional_exhausted_reason

    def _reserve_reason(self, need: int = 0) -> str:
        left = max(0, self.max_tokens - self.final_reserve_tokens - self.tokens)
        what = f" (a {need}-token call does not fit in the {left} left before the reserve)" if need else ""
        return (
            f"optional agent work stopped: {self.final_reserve_tokens} of {self.max_tokens} tokens are kept for the final "
            f"answer and its verification{what}"
        )

    def optional_remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.final_reserve_tokens - self.tokens)

    def _phase(self, phase: str) -> Dict[str, int]:
        return self.by_phase.setdefault(phase or "agent", {"tool_calls": 0, "llm_calls": 0, "tokens": 0})

    def charge_tool(self, phase: str = "", n: int = 1) -> None:
        if self.exhausted() or self.tool_calls + n > self.max_tool_calls:
            self.exhausted_reason = self.exhausted_reason or f"tool-call budget of {self.max_tool_calls} used"
            raise BudgetExhausted(self.exhausted_reason)
        self.tool_calls += n
        self._phase(phase)["tool_calls"] += n

    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.tokens)

    def output_token_cap(self) -> int:
        """The most tokens one reply may still produce without exceeding max_tokens."""
        return self.remaining_tokens()

    def charge_llm(self, phase: str = "", prompt_chars: int = 0, output_tokens: int = 0, final: Optional[bool] = None) -> int:
        """Charge one model call: its estimated prompt tokens plus the output it may produce (`output_tokens`, reserved
        now so concurrent calls cannot overshoot together). Returns the tokens charged; pass that back as
        estimated_prompt to record_usage, or as reserved to add_output_tokens, once the reply is in.

        final (default: phase in FINAL_PHASES) lets the call use the finalization reserve. Raises BudgetExhausted
        naming the limit that was hit; an optional call refused by the reserve sets only optional_exhausted_reason."""
        est = max(1, estimate_tokens(prompt_chars))
        reserve_out = max(0, int(output_tokens or 0))
        total = est + reserve_out
        is_final = (phase in FINAL_PHASES) if final is None else bool(final)
        if self.exhausted():
            raise BudgetExhausted(self.exhausted_reason)
        if self.llm_calls + 1 > self.max_llm_calls:
            self.exhausted_reason = f"model-call budget of {self.max_llm_calls} used"
            raise BudgetExhausted(self.exhausted_reason)
        if not is_final:
            if self.optional_exhausted_reason:
                raise BudgetExhausted(self.optional_exhausted_reason)
            if self.final_reserve_calls and self.llm_calls + 1 > self.max_llm_calls - self.final_reserve_calls:
                self.optional_exhausted()
                raise BudgetExhausted(self.optional_exhausted_reason)
            if self.final_reserve_tokens and self.tokens + total > self.max_tokens - self.final_reserve_tokens:
                self.optional_exhausted_reason = self._reserve_reason(total)
                raise BudgetExhausted(self.optional_exhausted_reason)
        # An optional call must fit with its output; a final call only needs its prompt to fit (the answer is written
        # with whatever is left rather than not at all).
        need = est if is_final else total
        if self.tokens + need > self.max_tokens:
            # AGT-010: name the limit that was actually hit (this used to report the model-call budget).
            self.exhausted_reason = (
                f"token budget of {self.max_tokens} used (a {need}-token prompt does not fit in the {self.remaining_tokens()} left)"
            )
            raise BudgetExhausted(self.exhausted_reason)
        # A final call whose prompt fits keeps going: its output reservation is clipped to what is left.
        total = est + min(reserve_out, self.max_tokens - self.tokens - est)
        self.llm_calls += 1
        self.tokens += total
        self._phase(phase)["llm_calls"] += 1
        self._phase(phase)["tokens"] += total
        return total

    def admit(
        self, phase: str, prompt_chars: int, max_output: int, *, final: Optional[bool] = None, min_output: int = 1
    ) -> Allocation:
        """Admit one model call: charge its prompt estimate plus the largest output allocation (at most max_output) that
        fits in its pool, and return it. The provider must be sent `output_cap` as max_tokens, and the call settled
        with settle() once it ends (also when it fails).

        Optional calls (default: phase not in FINAL_PHASES) must fit before the finalization reserve; final calls may
        use the whole budget. Raises BudgetExhausted when the call cannot get at least `min_output` output tokens:
        an optional call refused this way only stops optional work (optional_exhausted_reason)."""
        est = max(1, estimate_tokens(prompt_chars))
        want = max(0, int(max_output or 0))
        floor = max(0, min(int(min_output or 0), want))
        is_final = (phase in FINAL_PHASES) if final is None else bool(final)
        if self.exhausted():
            raise BudgetExhausted(self.exhausted_reason)
        if self.llm_calls + 1 > self.max_llm_calls:
            self.exhausted_reason = f"model-call budget of {self.max_llm_calls} used"
            raise BudgetExhausted(self.exhausted_reason)
        if is_final:
            room = self.max_tokens - self.tokens - est
        else:
            if self.optional_exhausted_reason:
                raise BudgetExhausted(self.optional_exhausted_reason)
            if self.final_reserve_calls and self.llm_calls + 1 > self.max_llm_calls - self.final_reserve_calls:
                self.optional_exhausted()
                raise BudgetExhausted(self.optional_exhausted_reason)
            room = self.max_tokens - self.final_reserve_tokens - self.tokens - est
        if room < floor:
            need = est + floor
            if is_final:
                msg = (
                    f"token budget of {self.max_tokens} used (a {need}-token call does not fit in the "
                    f"{self.remaining_tokens()} left)"
                )
                if room < 0:  # not even the prompt fits: nothing more can run
                    self.exhausted_reason = msg
                raise BudgetExhausted(msg)
            self.optional_exhausted_reason = (
                self._reserve_reason(need) if self.final_reserve_tokens
                else f"optional agent work stopped: a {need}-token call does not fit in the {self.remaining_tokens()} tokens left"
            )
            raise BudgetExhausted(self.optional_exhausted_reason)
        cap = max(0, min(want, room))
        alloc = Allocation(phase=phase or "agent", final=is_final, input_tokens=est, output_cap=cap, charged=est + cap)
        self.llm_calls += 1
        self.tokens += alloc.charged
        self.reserved_open += cap
        bucket = self._phase(phase)
        bucket["llm_calls"] += 1
        bucket["tokens"] += alloc.charged
        return alloc

    def settle(
        self,
        alloc: Allocation,
        *,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        output_chars: Optional[int] = None,
        failed: bool = False,
    ) -> None:
        """Replace an admitted call's allocation with what it actually used and release the rest. Provider-reported
        usage wins; else the reply's length (characters, reasoning included) is estimated; a failed, timed-out or
        cancelled call with nothing known keeps its prompt charge (the provider read the prompt). Idempotent."""
        if alloc.settled:
            return
        alloc.settled = True
        if input_tokens is not None and output_tokens is not None and not failed:
            actual = max(0, int(input_tokens)) + max(0, int(output_tokens))
            self.tokens_provider += actual
            self.provider_usage_calls += 1
        elif output_chars is not None and not failed:
            actual = alloc.input_tokens + estimate_tokens(output_chars)
            self.tokens_from_estimates += actual
            self.estimated_calls += 1
        else:
            actual = alloc.input_tokens + (estimate_tokens(output_chars) if output_chars else 0)
            self.tokens_from_estimates += actual
            self.unreconciled_calls += 1
        delta = actual - alloc.charged
        self.tokens = max(0, self.tokens + delta)
        self.reserved_open = max(0, self.reserved_open - alloc.output_cap)
        bucket = self._phase(alloc.phase)
        bucket["tokens"] = max(0, bucket["tokens"] + delta)
        self._check_tokens()

    def add_output_tokens(self, phase: str, chars: int, reserved: int = 0) -> None:
        """Charge a reply from its length (estimate); pass the raw reply including any reasoning text, and the output
        tokens charge_llm reserved for it (replaced by this estimate)."""
        est = estimate_tokens(chars) - max(0, int(reserved or 0))
        self.tokens = max(0, self.tokens + est)
        bucket = self._phase(phase)
        bucket["tokens"] = max(0, bucket["tokens"] + est)
        self.estimated_calls += 1
        self.tokens_from_estimates += max(0, estimate_tokens(chars))
        self._check_tokens()

    def record_usage(self, phase: str, *, estimated_prompt: int, input_tokens: int, output_tokens: int) -> None:
        """Replace a call's prompt estimate with the provider's reported input tokens and add its output tokens."""
        delta = max(0, int(input_tokens)) - max(0, int(estimated_prompt)) + max(0, int(output_tokens))
        self.tokens = max(0, self.tokens + delta)
        bucket = self._phase(phase)
        bucket["tokens"] = max(0, bucket["tokens"] + delta)
        self.provider_usage_calls += 1
        self.tokens_provider += max(0, int(input_tokens)) + max(0, int(output_tokens))
        self._check_tokens()

    def _check_tokens(self) -> None:
        if self.tokens >= self.max_tokens and not self.exhausted_reason:
            self.exhausted_reason = f"token budget of {self.max_tokens} used"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "limits": {
                "tool_calls": self.max_tool_calls,
                "llm_calls": self.max_llm_calls,
                "tokens": self.max_tokens,
                "seconds": self.max_seconds,
                "draft_rounds": self.max_draft_rounds,
                "plan_nodes": self.max_plan_nodes,
            },
            "used": {
                "tool_calls": self.tool_calls,
                "llm_calls": self.llm_calls,
                "tokens_estimated": self.tokens,
                "tokens": self.tokens,
                "seconds": round(self.elapsed(), 2),
            },
            "token_accounting": {
                "provider_usage_calls": self.provider_usage_calls,
                "estimated_calls": self.estimated_calls,
                "unreconciled_calls": self.unreconciled_calls,
                "tokens_provider": self.tokens_provider,
                "tokens_from_estimates": self.tokens_from_estimates,
                "reserved_open": self.reserved_open,
                "method": "provider-reported usage where available, otherwise characters/4 (reasoning text included); "
                "failed or cancelled calls keep their prompt estimate",
            },
            "exhausted": bool(self.exhausted_reason),
            "exhausted_reason": self.exhausted_reason,
            "reserve": {
                "final_tokens": self.final_reserve_tokens,
                "final_calls": self.final_reserve_calls,
                "optional_stopped": bool(self.optional_exhausted_reason),
                "optional_stopped_reason": self.optional_exhausted_reason,
            },
            "by_phase": self.by_phase,
        }
