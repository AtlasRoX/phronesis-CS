"""
Shared fixtures and helpers for Synapta unit tests.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit_mock


def tool_call_args(tool, args: dict[str, Any], tool_call_id: str = "tool_call_id") -> dict:
    """Create a ToolCall-like dict for BaseTool.invoke."""
    args.pop("tool_call_id", None)
    return {"name": tool.name, "type": "tool_call", "id": tool_call_id, "args": args}


@pytest.fixture(name="tool_call")
def tool_call_fixture():
    """Provide a ToolCall factory for tool invocation inputs."""
    return tool_call_args


@pytest.fixture(autouse=True)
def _clear_edge_index_cache():
    """Edge indices are cached per database; keep tests independent."""
    from ..utils.extractions.milvus_multimodal_pcst import clear_edge_index_cache

    from ..tools.graphrag_reasoning import clear_vectorstore_cache

    clear_edge_index_cache()
    clear_vectorstore_cache()
    yield
    clear_edge_index_cache()
    clear_vectorstore_cache()
