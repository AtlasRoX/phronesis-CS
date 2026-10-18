"""
Centralized LLM Factory for Phronesis.
Supports NVIDIA NIM (Nemotron 3 120B Super, Nemotron 3 550B Ultra, Nemotron 70B)
and OpenAI cloud models with unified configuration.
"""

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Optional, Sequence
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

logger = logging.getLogger(__name__)

# Supported model metadata catalog
NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"

DEFAULT_ORCHESTRATOR_MODEL = "nvidia/nemotron-3-super-120b-a12b"
DEFAULT_SYNTHESIS_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"

SUPPORTED_MODELS = [
    {
        "id": "nvidia/nemotron-3-super-120b-a12b",
        "name": "NVIDIA Nemotron 3 120B Super",
        "provider": "nvidia",
        "description": "120B MoE (12B active). High token velocity & low latency. Recommended default for multi-agent supervisor routing and tool-calling.",
        "recommended_for": "orchestrator",
        "is_default": True,
    },
    {
        "id": "nvidia/nemotron-3-ultra-550b-a55b",
        "name": "NVIDIA Nemotron 3 550B Ultra",
        "provider": "nvidia",
        "description": "550B MoE (55B active). Frontier deep reasoning, complex biological hypothesis deduction, and cross-evidence clinical trial synthesis.",
        "recommended_for": "deep_synthesis",
        "is_default": False,
    },
    {
        "id": "nvidia/llama-3.1-nemotron-70b-instruct",
        "name": "NVIDIA Llama-3.1 Nemotron 70B",
        "provider": "nvidia",
        "description": "70B dense model aligned by NVIDIA. Top performance on general biomedical instruction following and knowledge graph extraction.",
        "recommended_for": "general",
        "is_default": False,
    },
    {
        "id": "gpt-4o",
        "name": "OpenAI GPT-4o",
        "provider": "openai",
        "description": "Omni-modal cloud LLM with strong generalized multi-step reasoning capabilities.",
        "recommended_for": "general",
        "is_default": False,
    },
    {
        "id": "gpt-4o-mini",
        "name": "OpenAI GPT-4o-mini",
        "provider": "openai",
        "description": "Lightweight, low-latency cloud model for standard tasks.",
        "recommended_for": "fast",
        "is_default": False,
    },
]


def get_supported_models() -> list[dict[str, Any]]:
    """Return catalog of supported models with descriptions and recommendations."""
    return SUPPORTED_MODELS


def allowed_model_ids() -> set[str]:
    """Models a caller may select with the server's own keys: the catalog plus operator-configured ids."""
    ids: set[str] = {str(m["id"]) for m in SUPPORTED_MODELS} | {DEFAULT_ORCHESTRATOR_MODEL}
    for var in ("AIAGENTS4PHARMA_LLM_MODEL", "PHRONESIS_SYNTHESIS_MODEL", "PHRONESIS_AGENT_MODEL", "PHRONESIS_EVAL_JUDGE_MODEL"):
        value = (os.getenv(var) or "").strip()
        if value:
            ids.add(value)
    ids.update(m.strip() for m in os.getenv("PHRONESIS_ALLOWED_MODELS", "").split(",") if m.strip())
    return ids


def is_allowed_model(model_id: Optional[str]) -> bool:
    return not model_id or model_id in allowed_model_ids()


class LLMUnavailableError(RuntimeError):
    """No usable API key is configured for the requested model; raised before any network call."""


class ModelNotAllowedError(LLMUnavailableError):
    """The model is outside the allow-list and no caller key was supplied, so the server's key would pay (SEC-003)."""


_PLACEHOLDER_MARKERS = ("placeholder", "your-", "your_", "changeme", "change-me", "xxxx", "<", "...")


# Per-request client timeout. Reasoning models on NVIDIA NIM routinely take 20-60 s for a planning or
# critique reply, so the old 10 s default (x2 with one retry) killed every non-streaming agent phase at ~21 s.
DEFAULT_LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT", "60.0"))

# The model that answers an NVIDIA request when no NVIDIA key is configured but an OpenAI key is.
FALLBACK_MODEL = "gpt-4o"


def _usable_key(key: Optional[str]) -> Optional[str]:
    """The key, or None when it is empty or an obvious template value such as 'sk-placeholder'."""
    if not key or not key.strip():
        return None
    lowered = key.strip().lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS) or lowered in ("sk-", "nvapi-", "none", "null"):
        return None
    return key.strip()


def usable_key(key: Optional[str]) -> Optional[str]:
    """Public form of the placeholder check (request validation uses it to refuse template keys with 422)."""
    return _usable_key(key)


def fallback_enabled() -> bool:
    """PHRONESIS_LLM_FALLBACK (default on): an NVIDIA model may be answered by gpt-4o when only OPENAI_API_KEY is set.
    The substitution is always logged as a warning and recorded in the run manifest (PRV-002)."""
    return (os.getenv("PHRONESIS_LLM_FALLBACK") or "1").strip().lower() not in ("0", "false", "no", "off")


def _record_client(
    *, requested: str, served: str, provider: str, base_url: Optional[str], key_source: str, temperature: float,
    kwargs: dict[str, Any], fallback: bool = False, reason: Optional[str] = None,
) -> None:
    """Tell the active run recorder which model actually serves this client (PRV-002). Never raises."""
    try:
        from ...provenance.run_manifest import record_llm_client

        record_llm_client(
            requested_model=requested, served_model=served, provider=provider, base_url=base_url,
            fallback=fallback, fallback_reason=reason, key_source=key_source, temperature=temperature,
            max_tokens=kwargs.get("max_tokens"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("LLM client provenance not recorded: %s", exc)


def get_llm(
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.0,
    **kwargs: Any,
) -> BaseChatModel:
    """
    Factory function returning a configured LangChain ChatModel instance.

    Args:
        model_name: Model identifier (e.g., 'nvidia/nemotron-3-super-120b-a12b', 'gpt-4o').
                    Defaults to AIAGENTS4PHARMA_LLM_MODEL or 'nvidia/nemotron-3-super-120b-a12b'.
        api_key: Optional explicit API key. If not provided, resolves from NVIDIA_API_KEY
                 or OPENAI_API_KEY based on model provider.
        temperature: Model sampling temperature (default 0.0 for deterministic tool usage).
        **kwargs: Additional parameters forwarded to the model constructor.

    Key policy (SEC-003), enforced here where the key is resolved rather than only at request validation:
    a caller key that looks like a template value is refused (LLMUnavailableError) instead of being replaced by
    the server's key, and a model outside the allow-list (catalog + operator-configured ids) is refused
    (ModelNotAllowedError) whenever the server's key would pay for it. Every client is reported to the active
    run recorder with the model that actually serves it (PRV-002).

    Returns:
        BaseChatModel: Initialized LangChain chat model.
    """
    selected_model = (
        model_name
        or os.getenv("AIAGENTS4PHARMA_LLM_MODEL")
        or DEFAULT_ORCHESTRATOR_MODEL
    )
    caller_key = _usable_key(api_key)
    if api_key and api_key.strip() and caller_key is None:
        raise LLMUnavailableError(
            "The supplied API key looks like a placeholder; supply a real key or leave it empty to use the server's key."
        )
    if caller_key is None and not is_allowed_model(selected_model):
        raise ModelNotAllowedError(
            f"Model '{selected_model[:80]}' is not in the allowed model list; the server's API keys only pay for "
            "models listed by /api/models (or PHRONESIS_ALLOWED_MODELS). Supply your own API key to use it."
        )
    key_source = "caller" if caller_key else "server"

    reasoning_effort = kwargs.pop("reasoning_effort", None)
    is_nvidia = (
        selected_model.startswith("nvidia/")
        or selected_model.startswith("meta/")
        or "nemotron" in selected_model.lower()
    )

    if is_nvidia:
        resolved_key = (
            caller_key
            or _usable_key(os.getenv("NVIDIA_API_KEY"))
            or _usable_key(os.getenv("NVIDIA_NIM_API_KEY"))
        )
        if not resolved_key:
            # No NVIDIA key but an OpenAI key: answer with gpt-4o, loudly, and record the substitution.
            openai_key = _usable_key(os.getenv("OPENAI_API_KEY"))
            if openai_key and not caller_key and fallback_enabled():
                reason = "no NVIDIA API key configured; OPENAI_API_KEY used instead"
                logger.warning(
                    "Model '%s' requested but no NVIDIA API key is configured: answering with %s via OPENAI_API_KEY "
                    "(PHRONESIS_LLM_FALLBACK=0 disables this substitution)", selected_model, FALLBACK_MODEL,
                )
                kwargs.pop("extra_body", None)  # NIM-only options (e.g. Nemotron's enable_thinking) would be rejected
                client = ChatOpenAI(
                    model=FALLBACK_MODEL,
                    api_key=SecretStr(openai_key),
                    temperature=temperature,
                    **kwargs,
                )
                _record_client(requested=selected_model, served=FALLBACK_MODEL, provider="openai", base_url=None,
                               key_source="server", temperature=temperature, kwargs=kwargs, fallback=True, reason=reason)
                return client
            raise LLMUnavailableError(
                f"No API key configured for model '{selected_model}': set NVIDIA_API_KEY (or OPENAI_API_KEY) "
                "or supply an API key in Settings."
            )

        logger.info(
            "Initializing NVIDIA NIM ChatOpenAI client with model: %s at %s",
            selected_model,
            NVIDIA_NIM_BASE_URL,
        )
        if reasoning_effort:
            extra_body = kwargs.setdefault("extra_body", {})
            if isinstance(extra_body, dict):
                ctk = extra_body.setdefault("chat_template_kwargs", {})
                if isinstance(ctk, dict):
                    ctk.setdefault("enable_thinking", str(reasoning_effort).lower() != "low")

        timeout_sec = float(kwargs.pop("timeout", None) or DEFAULT_LLM_TIMEOUT_S)
        max_retries = int(kwargs.pop("max_retries", None) or 1)
        client = ChatOpenAI(
            model=selected_model,
            base_url=NVIDIA_NIM_BASE_URL,
            api_key=SecretStr(resolved_key),
            temperature=temperature,
            timeout=timeout_sec,
            max_retries=max_retries,
            **kwargs,
        )
        _record_client(requested=selected_model, served=selected_model, provider="nvidia_nim", base_url=NVIDIA_NIM_BASE_URL,
                       key_source=key_source, temperature=temperature, kwargs=kwargs)
        return client

    # Standard OpenAI cloud model
    resolved_key = caller_key or _usable_key(os.getenv("OPENAI_API_KEY"))
    if not resolved_key:
        raise LLMUnavailableError(
            f"No API key configured for model '{selected_model}': set OPENAI_API_KEY or supply an API key in Settings."
        )
    logger.info("Initializing OpenAI client with model: %s", selected_model)
    if reasoning_effort and selected_model.startswith(("o1", "o3", "o4")):
        kwargs["reasoning_effort"] = reasoning_effort

    timeout_sec = float(kwargs.pop("timeout", None) or DEFAULT_LLM_TIMEOUT_S)
    max_retries = int(kwargs.pop("max_retries", None) or 1)
    client = ChatOpenAI(
        model=selected_model,
        api_key=SecretStr(resolved_key),
        temperature=temperature,
        timeout=timeout_sec,
        max_retries=max_retries,
        **kwargs,
    )
    _record_client(requested=selected_model, served=selected_model, provider="openai", base_url=None,
                   key_source=key_source, temperature=temperature, kwargs=kwargs)
    return client


# -----------------------------------------------------------------------------
# Streaming with the model's own reasoning
# -----------------------------------------------------------------------------

DeltaCallback = Callable[[str], Awaitable[None]]

# Reasoning models stream their chain of thought separately from the answer. NVIDIA NIM
# (Nemotron 3) and most OpenAI-compatible servers use `reasoning_content`; some use `reasoning`.
_REASONING_FIELDS = ("reasoning_content", "reasoning")

# Read timeout between streamed chunks. Generous because reasoning models can pause before answering.
STREAM_READ_TIMEOUT = float(os.getenv("LLM_STREAM_TIMEOUT", "120.0"))
# Wall-clock cap on one whole streamed completion; the read timeout alone never ends a slow, steady stream.
STREAM_TOTAL_TIMEOUT = float(os.getenv("LLM_STREAM_TOTAL_TIMEOUT", "1800.0"))  # runs have no time limit; 30 min guards a hung stream


class LLMStreamTimeoutError(TimeoutError):
    """A streamed completion exceeded its total timeout (STREAM_TOTAL_TIMEOUT, or the run's remaining time)."""


def stream_timeout(remaining_s: Optional[float]) -> float:
    """Total wall-clock limit for one stream: STREAM_TOTAL_TIMEOUT, bounded by the run's remaining time when the run
    has an absolute deadline (audit F4); `None` means the run has no deadline."""
    if remaining_s is None:
        return STREAM_TOTAL_TIMEOUT
    return max(0.0, min(STREAM_TOTAL_TIMEOUT, float(remaining_s)))


class _ThinkTagSplitter:
    """Route `<think>...</think>` spans in a text stream to reasoning, for models that inline it."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        self._buf += chunk
        out: list[tuple[str, str]] = []
        while self._buf:
            tag = self.CLOSE if self._in_think else self.OPEN
            idx = self._buf.find(tag)
            if idx >= 0:
                if idx:
                    out.append(("reasoning" if self._in_think else "text", self._buf[:idx]))
                self._buf = self._buf[idx + len(tag):]
                self._in_think = not self._in_think
                continue
            # Hold back a possible partial tag at the end of the buffer.
            keep = next((k for k in range(len(tag) - 1, 0, -1) if self._buf.endswith(tag[:k])), 0)
            emit, self._buf = self._buf[: len(self._buf) - keep], self._buf[len(self._buf) - keep:]
            if emit:
                out.append(("reasoning" if self._in_think else "text", emit))
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        rest, self._buf = self._buf, ""
        return [("reasoning" if self._in_think else "text", rest)] if rest else []


def _to_openai_messages(messages: Sequence[BaseMessage]) -> list[dict[str, str]]:
    roles = {SystemMessage: "system", HumanMessage: "user", AIMessage: "assistant"}
    return [
        {"role": next((r for t, r in roles.items() if isinstance(m, t)), "user"), "content": str(m.content)}
        for m in messages
    ]


async def astream_with_reasoning(
    llm: Any,
    messages: Sequence[BaseMessage],
    on_reasoning: Optional[DeltaCallback] = None,
    on_text: Optional[DeltaCallback] = None,
    total_timeout: Optional[float] = None,
) -> tuple[str, str]:
    """
    Stream a completion, forwarding the model's real reasoning and answer text as they arrive.

    LangChain's ChatOpenAI drops `reasoning_content` from streamed deltas, so for ChatOpenAI
    instances this reads the raw OpenAI-compatible stream. Other chat models fall back to
    `astream`, then `ainvoke`. Returns (answer_text, reasoning_text). total_timeout (default
    STREAM_TOTAL_TIMEOUT) bounds the whole stream; pass stream_timeout(run remaining seconds).
    """
    limit = STREAM_TOTAL_TIMEOUT if total_timeout is None else max(0.0, float(total_timeout))
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    splitter = _ThinkTagSplitter()

    async def route(kind: str, delta: str) -> None:
        if not delta:
            return
        if kind == "reasoning":
            reasoning_parts.append(delta)
            if on_reasoning:
                await on_reasoning(delta)
        else:
            text_parts.append(delta)
            if on_text:
                await on_text(delta)

    async def route_text(delta: str) -> None:
        for kind, part in splitter.feed(delta):
            await route(kind, part)

    try:
        async with asyncio.timeout(limit):
            client = getattr(llm, "root_async_client", None)
            if isinstance(llm, ChatOpenAI) and client is not None:
                params: dict[str, Any] = {
                    "model": llm.model_name,
                    "messages": _to_openai_messages(messages),
                    "stream": True,
                }
                if llm.temperature is not None:
                    params["temperature"] = llm.temperature
                if llm.max_tokens:
                    params["max_tokens"] = llm.max_tokens
                if getattr(llm, "extra_body", None):
                    params["extra_body"] = llm.extra_body  # e.g. Nemotron's enable_thinking=false for edit rounds
                stream = await client.with_options(timeout=STREAM_READ_TIMEOUT).chat.completions.create(**params)
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    extra = getattr(delta, "model_extra", None) or {}
                    reasoning = next((extra[f] for f in _REASONING_FIELDS if extra.get(f)), None)
                    if reasoning:
                        await route("reasoning", reasoning)
                    if delta.content:
                        await route_text(delta.content)
            elif hasattr(llm, "astream"):
                async for chunk in llm.astream(list(messages)):
                    extra = getattr(chunk, "additional_kwargs", None) or {}
                    reasoning = next((extra[f] for f in _REASONING_FIELDS if extra.get(f)), None)
                    if reasoning:
                        await route("reasoning", str(reasoning))
                    content = chunk.content if isinstance(chunk.content, str) else ""
                    await route_text(content)
            else:
                res = await llm.ainvoke(list(messages))
                res_content = getattr(res, "content", res)
                content_str = res_content if isinstance(res_content, str) else str(res_content)
                await route_text(content_str)

    except TimeoutError as exc:
        raise LLMStreamTimeoutError(f"Model stream exceeded {limit:g}s") from exc

    for kind, part in splitter.flush():
        await route(kind, part)
    return "".join(text_parts), "".join(reasoning_parts)
