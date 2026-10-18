"""
BioContext Ensembl tool for Phronesis.
Queries Ensembl REST API for gene ID mapping and genomic coordinates.
"""

import json
from typing import Type
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from ...utils.biocontext import BioContextMCPClient


class BioContextEnsemblInput(BaseModel):
    """Input for BioContext Ensembl tool."""

    gene_symbol: str = Field(
        description="Gene name or symbol to look up (e.g. 'BRCA1', 'TNF', 'TP53')."
    )
    species: str = Field(
        default="human",
        description="Species name (e.g. 'human', 'mouse', 'rat').",
    )


class BioContextEnsemblTool(BaseTool):
    """Tool to query Ensembl via BioContext MCP."""

    name: str = "biocontext_ensembl"
    description: str = (
        "Query Ensembl via BioContext MCP for gene symbols, stable Ensembl IDs, genomic locations, "
        "and biotypes."
    )
    args_schema: Type[BaseModel] = BioContextEnsemblInput

    def _run(self, gene_symbol: str, species: str = "human") -> str:
        client = BioContextMCPClient()
        result = client.query_ensembl(gene_symbol=gene_symbol, species=species)
        return json.dumps(result, indent=2)
