"""
BioContext OpenTargets tool for Phronesis.
Queries target-disease associations, genetics, and drug tractability from OpenTargets.
"""

import json
from typing import Type
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from ...utils.biocontext import BioContextMCPClient


class BioContextOpenTargetsInput(BaseModel):
    """Input for BioContext OpenTargets tool."""

    query: str = Field(
        description="Target gene name, disease name, or drug name to search in OpenTargets (e.g. 'TNF', 'Crohn disease', 'Imatinib')."
    )
    size: int = Field(
        default=5,
        description="Maximum number of hits/associations to retrieve.",
    )


class BioContextOpenTargetsTool(BaseTool):
    """Tool to query OpenTargets platform via BioContext MCP."""

    name: str = "biocontext_opentargets"
    description: str = (
        "Query OpenTargets via BioContext MCP for target-disease associations, "
        "druggability, tractability evidence, and clinical trials data."
    )
    args_schema: Type[BaseModel] = BioContextOpenTargetsInput

    def _run(self, query: str, size: int = 5) -> str:
        client = BioContextMCPClient()
        result = client.query_opentargets(query=query, size=size)
        return json.dumps(result, indent=2)
