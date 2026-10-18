"""
BioContext KEGG tool for Phronesis.
Queries biological pathways, enzymatic reactions, and drug mechanisms from KEGG.
"""

import json
import re
from typing import Any, Dict, Type
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from ...utils.biocontext import BioContextMCPClient


class BioContextKEGGInput(BaseModel):
    """Input for BioContext KEGG tool."""

    argument: str = Field(
        description="Search term, pathway name, or KEGG identifier (e.g. 'glycolysis', 'pathway:hsa04010', 'D00001')."
    )
    operation: str = Field(
        default="find",
        description="KEGG operation: 'find' (search entries), 'get' (retrieve entry details), or 'link' (find related objects).",
    )


class BioContextKEGGTool(BaseTool):
    """Tool to query KEGG database via BioContext MCP."""

    name: str = "biocontext_kegg"
    description: str = (
        "Query KEGG (Kyoto Encyclopedia of Genes and Genomes) via BioContext MCP for biological "
        "pathways, metabolic networks, disease pathways, and drug-pathway interactions."
    )
    args_schema: Type[BaseModel] = BioContextKEGGInput

    def _run(self, argument: str, operation: str = "find") -> str:
        return json.dumps(kegg_lookup(BioContextMCPClient(), operation, argument), indent=2)


_GENE_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9-]{1,14}$")
_KEGG_DB_PREFIXES = ("pathway", "genes", "compound", "disease", "drug/", "hsa:", "path:", "ko:")


def kegg_lookup(client: Any, operation: str, argument: str) -> Dict[str, Any]:
    """KEGG query for the legacy tools (ORC-016).

    `find pathway/<GENE SYMBOL>` matches pathway names only and is empty for most genes, which used to come back
    as a normal "no pathways" result. A bare gene symbol now takes the maintained route
    find/hsa -> link/pathway -> list/pathway; only when KEGG has no exact gene entry for it is the term searched
    in pathway names (e.g. "MAPK", "glycolysis")."""
    arg = str(argument or "").strip()
    if operation == "find" and not arg.lower().startswith(_KEGG_DB_PREFIXES):
        if _GENE_SYMBOL_RE.match(arg):
            genes = client.query_kegg_gene_pathways(arg)
            if isinstance(genes, dict) and (genes.get("results") or genes.get("upstream_failed")):
                return genes
        return client.query_kegg(operation=operation, argument=f"pathway/{arg}")
    return client.query_kegg(operation=operation, argument=arg)
