"""
Synapta tool over the full local PrimeKG (phronesis.kgintel): exact typed graph queries and analyses, as a
complement to embedding-based subgraph extraction. Works offline once `phronesis-primekg build` has run.
"""

import json
from typing import Any, Dict, List, Literal, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

Operation = Literal[
    "resolve", "drug_profile", "disease_profile", "gene_profile", "neighbors", "paths", "drug_disease",
    "repurposing", "phenotype_diagnosis", "polypharmacy", "enrichment", "similar", "stats",
]

# Result keys trimmed before the JSON goes back to the model (long lists stay in the artifact, not the prompt).
_MAX_CHARS = 24000


class PrimeKGQueryInput(BaseModel):
    """Input for the PrimeKG query tool. Fill only the fields the operation needs."""

    operation: Operation = Field(
        description="resolve: find nodes by name/id. drug_profile / disease_profile / gene_profile: full profile. "
        "neighbors: typed neighbours of any node. paths: shortest mechanism paths source -> target. "
        "drug_disease: how a drug relates to a disease. repurposing: ranked drug hypotheses for a disease. "
        "phenotype_diagnosis: diseases ranked by phenotypes. polypharmacy: interactions in a regimen. "
        "enrichment: pathways/GO/diseases/drugs over-represented in a gene list. similar: most similar nodes. "
        "stats: graph size and provenance."
    )
    query: Optional[str] = Field(default=None, description="resolve: name or identifier to look up.")
    drug: Optional[str] = Field(default=None, description="drug name or DrugBank id (drug_profile, drug_disease).")
    disease: Optional[str] = Field(default=None, description="disease name or MONDO id (disease_profile, drug_disease, repurposing).")
    gene: Optional[str] = Field(default=None, description="gene symbol or NCBI gene id (gene_profile).")
    entity: Optional[str] = Field(default=None, description="any node name (neighbors, similar).")
    node_type: Optional[str] = Field(default=None, description="node type of entity: gene, drug, disease, phenotype, pathway, ...")
    relation: Optional[str] = Field(default=None, description="neighbors: PrimeKG relation such as indication, drug_protein, disease_protein.")
    source: Optional[str] = Field(default=None, description="paths: start node name.")
    target: Optional[str] = Field(default=None, description="paths: end node name.")
    source_type: Optional[str] = Field(default=None, description="paths: node type of source.")
    target_type: Optional[str] = Field(default=None, description="paths: node type of target.")
    present: Optional[List[str]] = Field(default=None, description="phenotype_diagnosis: HPO phenotype names present.")
    absent: Optional[List[str]] = Field(default=None, description="phenotype_diagnosis: phenotype names stated absent.")
    drugs: Optional[List[str]] = Field(default=None, description="polypharmacy: drugs in the regimen.")
    diseases: Optional[List[str]] = Field(default=None, description="polypharmacy: patient conditions to check contraindications.")
    genes: Optional[List[str]] = Field(default=None, description="enrichment: gene symbols.")
    include_experimental: Optional[bool] = Field(
        default=None, description="repurposing: also list experimental-only compounds (default: approved or investigational)."
    )
    limit: Optional[int] = Field(default=None, ge=1, le=100, description="maximum items per list.")


_ARGS: Dict[str, tuple] = {
    "resolve": ("query", "limit"),
    "drug_profile": ("drug", "limit"),
    "disease_profile": ("disease", "limit"),
    "gene_profile": ("gene", "limit"),
    "neighbors": ("entity", "node_type", "relation", "limit"),
    "paths": ("source", "target", "source_type", "target_type", "limit"),
    "drug_disease": ("drug", "disease"),
    "repurposing": ("disease", "limit", "include_experimental"),
    "phenotype_diagnosis": ("present", "absent", "limit"),
    "polypharmacy": ("drugs", "diseases"),
    "enrichment": ("genes", "limit"),
    "similar": ("entity", "node_type", "limit"),
    "stats": (),
}


def _strip(obj: Any) -> Any:
    """Drop internal node indices so the model cites real identifiers."""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k != "index"}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


class PrimeKGQueryTool(BaseTool):
    """Exact queries and analyses over the full PrimeKG graph."""

    name: str = "primekg_query"
    description: str = (
        "Query the full PrimeKG knowledge graph (129k nodes, 4M edges; DrugBank, DisGeNET, Mondo, HPO, Reactome, "
        "GO, SIDER and more). Use it for exact facts (indications, contraindications, targets, side effects, "
        "disease genes and phenotypes), mechanism paths between two entities, drug repurposing hypotheses, "
        "phenotype-based differential diagnosis, regimen interaction checks and gene-set enrichment."
    )
    args_schema: Type[BaseModel] = PrimeKGQueryInput

    def _run(self, operation: str, **kwargs: Any) -> str:
        from ...kgintel import service
        from ...kgintel.graph import PrimeKGError

        allowed = _ARGS.get(operation, ())
        args = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        try:
            out = service.run(operation, **args)
        except PrimeKGError as exc:
            return json.dumps({"error": str(exc), "kind": exc.kind})
        except TypeError as exc:
            return json.dumps({"error": f"missing or invalid arguments for {operation}: {exc}"})
        text = json.dumps(_strip(out), default=str)
        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS] + '... [truncated; ask for a smaller limit]"'
        return text
