"""
Unit tests for NVIDIA NIM Nemotron model support, LLM Factory,
and 4-Agent Clinical R&D Orchestrator LangGraph compilation.
"""

from unittest.mock import MagicMock, patch
import pytest

from phronesis.utils.llm import (
    DEFAULT_ORCHESTRATOR_MODEL,
    DEFAULT_SYNTHESIS_MODEL,
    NVIDIA_NIM_BASE_URL,
    get_llm,
    get_supported_models,
)
from phronesis.phronesis.agents.main_agent import get_app
from phronesis.phronesis.states.state_phronesis import (
    Phronesis,
)
from phronesis.vivarix.states.state_vivarix import Vivarix
from phronesis.lumicell.states.state_lumicell import Lumicell
from phronesis.synapta.states.state_synapta import (
    Synapta,
)
from phronesis.lithoscribe.state.state_lithoscribe import Lithoscribe


def test_supported_models_catalog():
    """Verify catalog includes Nemotron 120B Super and 550B Ultra."""
    models = get_supported_models()
    model_ids = [m["id"] for m in models]

    assert DEFAULT_ORCHESTRATOR_MODEL in model_ids
    assert DEFAULT_SYNTHESIS_MODEL in model_ids
    assert "nvidia/nemotron-3-super-120b-a12b" in model_ids
    assert "nvidia/nemotron-3-ultra-550b-a55b" in model_ids

    # Check default orchestrator
    default_model = next(m for m in models if m["is_default"])
    assert default_model["id"] == "nvidia/nemotron-3-super-120b-a12b"
    assert default_model["recommended_for"] == "orchestrator"


def test_get_llm_nvidia_nim_config(monkeypatch):
    """Verify get_llm configures base_url for NVIDIA NIM API."""
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key-123")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    model = get_llm("nvidia/nemotron-3-super-120b-a12b")
    assert model.model_name == "nvidia/nemotron-3-super-120b-a12b"
    assert model.openai_api_base == NVIDIA_NIM_BASE_URL
    assert model.openai_api_key.get_secret_value() == "nvapi-test-key-123"


def test_get_llm_nemotron_550b_ultra(monkeypatch):
    """Verify get_llm handles Nemotron 550B Ultra for deep synthesis."""
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-ultra-key")

    model = get_llm("nvidia/nemotron-3-ultra-550b-a55b")
    assert model.model_name == "nvidia/nemotron-3-ultra-550b-a55b"
    assert model.openai_api_base == NVIDIA_NIM_BASE_URL


def test_state_inheritance_all_4_agents():
    """Verify Phronesis inherits from all 4 domain states."""
    assert Vivarix in Phronesis.__orig_bases__
    assert Synapta in Phronesis.__orig_bases__
    assert Lumicell in Phronesis.__orig_bases__
    assert Lithoscribe in Phronesis.__orig_bases__


def test_main_agent_supervisor_compilation_all_4_agents():
    """Verify supervisor compiles with all 4 domain agents (T2B, T2KG, SCP, S2)."""
    fake_llm = MagicMock()
    fake_llm.model_name = "nvidia/nemotron-3-super-120b-a12b"
    fake_llm.model_kwargs = {}
    fake_llm.bind_tools = MagicMock(return_value=fake_llm)

    app = get_app("test_supervisor_thread", fake_llm)
    assert app is not None
    assert app.name == "Phronesis_Agent"
