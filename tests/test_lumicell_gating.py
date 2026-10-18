"""Lumicell runs only on the researcher's own single-cell dataset: never planned without one, never on a model-written path."""

from __future__ import annotations

import asyncio
from typing import Any

from phronesis.agentic import config as agent_config
from phronesis.agentic.budget import RunBudget
from phronesis.agentic.control import RunControl
from phronesis.agentic.llm import AgentLLM
from phronesis.agentic.plan_graph import policy_violation, single_cell_dataset_path
from phronesis.agentic.specialists import tool_allowed
from phronesis.agentic.state import AgentState
from phronesis.agents.query_context import DatasetRef, QueryContext
from tests.fakes import build_recording_registry

Q = "Cluster the cells and find marker genes for APC in colon cancer"
LUMICELL = "lumicell_analyze_single_cell"


def _ctx(*attachments: DatasetRef, **kw: Any) -> QueryContext:
    return QueryContext(raw_query=Q, clean_query=Q, attachments=list(attachments), **kw)


H5AD = DatasetRef(id="ds_0123456789ab", kind="h5ad", display_name="tumour.h5ad", analysis_path="/data/ds_0123456789ab/data.h5ad")
TABLE = DatasetRef(id="ds_ba9876543210", kind="table", display_name="counts.csv", analysis_path="/data/ds_ba9876543210/data.csv")


def test_lumicell_is_not_offered_without_a_single_cell_dataset():
    reg, _ = build_recording_registry()
    entry = reg.get_entry(LUMICELL)
    for ctx in (_ctx(), _ctx(TABLE)):
        assert single_cell_dataset_path(ctx) is None and ctx.single_cell_dataset_path is None
        assert policy_violation(entry, ctx) == "no single-cell dataset (.h5ad) is attached"
        assert tool_allowed(ctx, entry) == (False, "no single-cell dataset (.h5ad) is attached")
    for ctx in (_ctx(H5AD), _ctx(TABLE, H5AD), _ctx(dataset_path="/data/typed.h5ad")):
        assert policy_violation(entry, ctx) is None and tool_allowed(ctx, entry)[0]
    assert _ctx(TABLE, H5AD).single_cell_dataset_path == H5AD.analysis_path


def test_missing_single_cell_dataset_is_a_plan_fact_even_with_a_table_attached():
    ctx = _ctx(TABLE, has_single_cell_intent=True)
    assert "Single-cell analysis needs a .h5ad/.zarr file; none referenced" in ctx.plan_facts()
    assert not any("Single-cell" in f for f in _ctx(H5AD, has_single_cell_intent=True).plan_facts())


def _state(ctx: QueryContext, registry: Any) -> AgentState:
    budget = RunBudget.from_env(None)

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    return AgentState(
        query=Q, clean_query=Q, ctx=ctx, registry=registry, user_id="alice", thread_id="t-lumicell", model_name="m",
        api_key=None, framework=agent_config.AGENTIC, budget=budget, control=RunControl("alice", interactive=False),
        llm=AgentLLM(None, "m", None, budget), tool_results={}, emit=noop, send=noop,
    )


def test_call_tool_replaces_a_model_written_dataset_path_with_the_attachment():
    reg, calls = build_recording_registry()
    state = _state(_ctx(H5AD), reg)
    asyncio.run(state.call_tool(LUMICELL, phase="plan", purpose="test", target_gene="APC", dataset_path="C:/Windows/secret.h5ad"))
    assert calls[-1][0] == LUMICELL and calls[-1][1]["dataset_path"] == H5AD.analysis_path
