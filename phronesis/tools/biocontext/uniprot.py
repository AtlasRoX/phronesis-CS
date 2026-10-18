"""
BioContext UniProt tool for Phronesis.
Queries UniProtKB for verified protein descriptions, sequences, and functional domains.
"""

import json
from typing import Type
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from ...utils.biocontext import BioContextMCPClient


class BioContextUniProtInput(BaseModel):
    """Input for BioContext UniProt tool."""

    query: str = Field(
        description="Protein name, gene symbol, or UniProt accession (e.g. 'P01375', 'TNF', 'EGFR human')."
    )
    limit: int = Field(
        default=5,
        description="Maximum number of protein entries to return.",
    )
    reviewed_only: bool = Field(
        default=True,
        description="Whether to restrict to Swiss-Prot reviewed entries.",
    )


class BioContextUniProtTool(BaseTool):
    """Tool to query UniProtKB via BioContext MCP."""

    name: str = "biocontext_uniprot"
    description: str = (
        "Query UniProtKB via BioContext MCP for protein sequence, functions, annotations, "
        "and organism data."
    )
    args_schema: Type[BaseModel] = BioContextUniProtInput

    def _run(self, query: str, limit: int = 5, reviewed_only: bool = True) -> str:
        client = BioContextMCPClient()
        result = client.query_uniprot(
            query=query, limit=limit, reviewed_only=reviewed_only
        )
        return json.dumps(result, indent=2)
