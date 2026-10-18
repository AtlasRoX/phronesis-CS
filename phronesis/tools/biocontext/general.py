"""
BioContext General tool for Phronesis.
Allows agents to query any BioContext resource (OpenTargets, KEGG, EuropePMC, UniProt, Ensembl, PanglaoDB)
dynamically via a single entry point.
"""

import json
from typing import Literal, Type
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from ...utils.biocontext import BioContextMCPClient


class BioContextInput(BaseModel):
    """Input for BioContext tool."""

    resource: Literal[
        "opentargets", "kegg", "europepmc", "uniprot", "ensembl", "panglaodb"
    ] = Field(
        description="The BioContext biomedical database to query: 'opentargets', 'kegg', 'europepmc', 'uniprot', 'ensembl', or 'panglaodb'."
    )
    query: str = Field(
        description="Search term, entity name, gene symbol, or pathway identifier to query."
    )
    extra_param: str | None = Field(
        default=None,
        description="Optional additional parameter (e.g. KEGG operation like 'find' or 'get', or species for Ensembl).",
    )


class BioContextTool(BaseTool):
    """General tool to query BioContext biomedical intelligence platform."""

    name: str = "biocontext"
    description: str = (
        "Universal BioContext MCP tool to retrieve verified biomedical data from OpenTargets "
        "(target-disease associations), KEGG (pathways), Europe PMC (scientific papers), "
        "UniProt (protein functions & sequences), Ensembl (gene mapping), or PanglaoDB (cell markers, from a "
        "local offline snapshot)."
    )
    args_schema: Type[BaseModel] = BioContextInput

    def _run(
        self,
        resource: Literal[
            "opentargets", "kegg", "europepmc", "uniprot", "ensembl", "panglaodb"
        ],
        query: str,
        extra_param: str | None = None,
    ) -> str:
        client = BioContextMCPClient()
        if resource == "opentargets":
            res = client.query_opentargets(query=query)
        elif resource == "kegg":
            from .kegg import kegg_lookup  # gene symbols take find/hsa -> link/pathway (ORC-016)

            res = kegg_lookup(client, extra_param or "find", query)
        elif resource == "europepmc":
            res = client.query_europepmc(query=query)
        elif resource == "uniprot":
            res = client.query_uniprot(query=query)
        elif resource == "ensembl":
            species = extra_param or "human"
            res = client.query_ensembl(gene_symbol=query, species=species)
        elif resource == "panglaodb":
            res = client.query_panglaodb(gene_or_cell=query)
        else:
            res = {"error": f"Unknown BioContext resource: {resource}"}

        return json.dumps(res, indent=2)
