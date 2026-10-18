"""
BioContext PanglaoDB tool for Phronesis.
Looks up cell-type markers in the bundled offline PanglaoDB snapshot (no remote call).
"""

import json
from typing import Type
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from ...utils.biocontext import BioContextMCPClient


class BioContextPanglaoDBInput(BaseModel):
    """Input for BioContext PanglaoDB tool."""

    gene_or_cell: str = Field(
        description="Gene symbol (e.g. 'CD3D', 'MS4A1') or cell type name (e.g. 'T cells', 'B cells') to look up."
    )


class BioContextPanglaoDBTool(BaseTool):
    """Tool to query cell-type markers via BioContext MCP."""

    name: str = "biocontext_panglaodb"
    description: str = (
        "Look up single-cell cell-type markers in the bundled offline PanglaoDB snapshot: a gene symbol returns "
        "the cell types it marks, a cell-type name returns its markers. status is 'ok', 'no_match' or "
        "'unavailable' (snapshot not installed)."
    )
    args_schema: Type[BaseModel] = BioContextPanglaoDBInput

    def _run(self, gene_or_cell: str) -> str:
        client = BioContextMCPClient()
        result = client.query_panglaodb(gene_or_cell=gene_or_cell)
        return json.dumps(result, indent=2)
