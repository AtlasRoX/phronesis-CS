"""
Unit tests for the Multi-Tier Effort Level & Thinking Engine.
Verifies effort profiles, budget ceilings, literature retrieval depth,
decomposition scaling, and API endpoint metadata.
"""

import pytest
from fastapi.testclient import TestClient

from app.backend.main import app
from phronesis.agentic.budget import RunBudget
from phronesis.agentic.config import (
    ALL_EFFORT_LEVELS,
    EFFORT_HIGH,
    EFFORT_LOW,
    EFFORT_MEDIUM,
    EFFORT_ULTRA,
    get_effort_profile,
)
from phronesis.agentic.decompose import heuristic_split, parse_decomposition
from phronesis.agents.authoritative_orchestrator import literature_request
from phronesis.agents.query_planner import QueryContext


def test_effort_profiles_definitions():
    """Verify all 4 effort profiles exist with correct characteristics."""
    assert ALL_EFFORT_LEVELS == ("low", "medium", "high", "ultra")

    low = get_effort_profile(EFFORT_LOW)
    med = get_effort_profile(EFFORT_MEDIUM)
    high = get_effort_profile(EFFORT_HIGH)
    ultra = get_effort_profile(EFFORT_ULTRA)

    # Subqueries monotonic scaling
    assert low["max_subqueries"] < med["max_subqueries"] < high["max_subqueries"] < ultra["max_subqueries"]
    assert (low["max_subqueries"], med["max_subqueries"], high["max_subqueries"], ultra["max_subqueries"]) == (2, 3, 5, 8)

    # Literature passages monotonic scaling
    assert low["max_passages"] < med["max_passages"] < high["max_passages"] < ultra["max_passages"]
    assert (low["max_passages"], med["max_passages"], high["max_passages"], ultra["max_passages"]) == (4, 8, 14, 25)

    # Draft rounds monotonic scaling
    assert low["max_draft_rounds"] < med["max_draft_rounds"] < high["max_draft_rounds"] < ultra["max_draft_rounds"]
    assert (low["max_draft_rounds"], med["max_draft_rounds"], high["max_draft_rounds"], ultra["max_draft_rounds"]) == (1, 2, 3, 5)

    # Thinking budgets
    assert low["thinking_budget_tokens"] == 1024
    assert med["thinking_budget_tokens"] == 4096
    assert high["thinking_budget_tokens"] == 16384
    assert ultra["thinking_budget_tokens"] == 32768


def test_run_budget_scaling_by_effort():
    """Verify RunBudget ceilings scale automatically when effort_level is provided."""
    budget_low = RunBudget.from_env(effort_level=EFFORT_LOW)
    budget_med = RunBudget.from_env(effort_level=EFFORT_MEDIUM)
    budget_high = RunBudget.from_env(effort_level=EFFORT_HIGH)
    budget_ultra = RunBudget.from_env(effort_level=EFFORT_ULTRA)

    assert budget_low.max_tool_calls == 15
    assert budget_low.max_draft_rounds == 1

    assert budget_med.max_tool_calls == 40
    assert budget_med.max_draft_rounds == 2

    assert budget_high.max_tool_calls == 80
    assert budget_high.max_draft_rounds == 3

    assert budget_ultra.max_tool_calls == 150
    assert budget_ultra.max_draft_rounds == 5

    # Caller overrides can tighten limits
    tightened_budget = RunBudget.from_env(overrides={"max_tool_calls": 10}, effort_level=EFFORT_LOW)
    assert tightened_budget.max_tool_calls == 10


def test_literature_request_scales_with_effort():
    """Verify literature_request dynamically queries more articles for higher effort."""
    ctx_low = QueryContext(raw_query="PARP inhibitors in BRCA-mutant breast cancer", clean_query="PARP inhibitors in BRCA-mutant breast cancer", scope="research")
    ctx_low.effort_level = EFFORT_LOW

    ctx_med = QueryContext(raw_query="PARP inhibitors in BRCA-mutant breast cancer", clean_query="PARP inhibitors in BRCA-mutant breast cancer", scope="research")
    ctx_med.effort_level = EFFORT_MEDIUM

    ctx_ultra = QueryContext(raw_query="PARP inhibitors in BRCA-mutant breast cancer", clean_query="PARP inhibitors in BRCA-mutant breast cancer", scope="research")
    ctx_ultra.effort_level = EFFORT_ULTRA

    req_low = literature_request(ctx_low, "PARP inhibitors in BRCA-mutant breast cancer")
    req_med = literature_request(ctx_med, "PARP inhibitors in BRCA-mutant breast cancer")
    req_ultra = literature_request(ctx_ultra, "PARP inhibitors in BRCA-mutant breast cancer")

    assert req_low["max_articles"] == 4
    assert req_med["max_articles"] == 8
    assert req_ultra["max_articles"] == 25


def test_decomposition_scales_with_effort():
    """Verify heuristic_split and parse_decomposition adapt to max_sub_questions."""
    query = "1) What is KRAS G12D? 2) What is MRTX1133? 3) How does it inhibit KRAS? 4) What are clinical trials? 5) What are resistance mechanisms? 6) Compare with MRTX849. 7) What are combinations?"

    splits_low = heuristic_split(query, max_sub_questions=2)
    assert len(splits_low) <= 2

    splits_high = heuristic_split(query, max_sub_questions=5)
    assert len(splits_high) <= 5

    splits_ultra = heuristic_split(query, max_sub_questions=8)
    assert len(splits_ultra) <= 8

    # parse_decomposition limits
    raw_subqs = {"complex": True, "sub_questions": [{"id": f"q{i}", "text": f"Sub question {i} regarding targets"} for i in range(1, 10)]}
    _, parsed_low = parse_decomposition(raw_subqs, max_sub_questions=2)
    assert len(parsed_low) == 2

    _, parsed_ultra = parse_decomposition(raw_subqs, max_sub_questions=8)
    assert len(parsed_ultra) == 8


def test_api_effort_levels_endpoint():
    """Verify GET /api/effort-levels returns proper metadata for frontend consumption."""
    client = TestClient(app)
    resp = client.get("/api/effort-levels")
    assert resp.status_code == 200
    data = resp.json()
    assert data["default"] == "medium"
    assert len(data["levels"]) == 4

    levels_by_id = {lvl["level"]: lvl for lvl in data["levels"]}
    assert "low" in levels_by_id
    assert "medium" in levels_by_id
    assert "high" in levels_by_id
    assert "ultra" in levels_by_id

    assert levels_by_id["low"]["max_passages"] == 4
    assert levels_by_id["ultra"]["max_passages"] == 25
