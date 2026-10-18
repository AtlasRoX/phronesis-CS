"""
BioContext EuropePMC tool for Phronesis.
Queries Europe PMC for biomedical and life sciences research literature.
"""

import json
from typing import Type
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from ...utils.biocontext import BioContextMCPClient


class BioContextEuropePMCInput(BaseModel):
    """Input for BioContext EuropePMC tool."""

    query: str = Field(
        description="Search string for biomedical literature (e.g. keywords, author, gene, disease, drug name, or PMID)."
    )
    limit: int = Field(
        default=5,
        description="Maximum number of articles to return.",
    )


class BioContextEuropePMCTool(BaseTool):
    """Tool to query Europe PMC literature database via BioContext MCP."""

    name: str = "biocontext_europepmc"
    description: str = (
        "Query Europe PMC via BioContext MCP for peer-reviewed biomedical literature, preprints, "
        "citation counts, and open-access scientific publications."
    )
    args_schema: Type[BaseModel] = BioContextEuropePMCInput

    def _run(self, query: str, limit: int = 5) -> str:
        client = BioContextMCPClient()
        result = client.query_europepmc(query=query, limit=limit)
        return json.dumps(result, indent=2)
