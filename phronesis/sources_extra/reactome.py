"""
Reactome pathways that contain one human gene product.

Reference: Milacic M. et al., "The Reactome Pathway Knowledgebase 2024", Nucleic Acids Res. 2024;52(D1):D672-D678,
doi:10.1093/nar/gkad1025.

API: with a UniProt accession, ContentService `GET https://reactome.org/ContentService/data/mapping/UniProt/
{acc}/pathways?species=9606` lists the lowest-level pathways {stId, displayName, isInDisease, ...}. With only a
gene symbol, AnalysisService `GET https://reactome.org/AnalysisService/identifier/{symbol}/projection?species=9606`
maps the symbol and lists the pathways it is found in ({stId, name, llp, inDisease}). The over-representation
statistics of a one-identifier analysis are meaningless and are not reported.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

from pydantic import BaseModel, Field, model_validator

from ..agents.query_context import QueryContext
from . import common

TOOL_NAME = "reactome_pathways"
SOURCE_KEY = "reactome"
CONTENT_URL = "https://reactome.org/ContentService"
ANALYSIS_URL = "https://reactome.org/AnalysisService"
VERSION_URL = f"{CONTENT_URL}/data/database/version"  # a bare release number, e.g. 97
STID_RE = re.compile(r"\bR-HSA-\d{3,9}\b")

INTENT_RE = re.compile(
    r"\b(pathways?|reactome|signall?ing\s+(?:cascades?|networks?|axis|axes|routes?)|downstream\s+signall?ing|"
    r"biological\s+process(?:es)?|cellular\s+process(?:es)?)\b",
    re.I,
)
NAME_RE = re.compile(r"\breactome\b", re.I)


class ReactomePathwaysInput(BaseModel):
    gene: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9\-]{0,14}$", description="HGNC symbol")
    uniprot: Optional[str] = Field(default=None, pattern=r"^[A-Z0-9]{6,10}$", description="UniProt accession (preferred)")
    limit: int = Field(default=15, ge=1, le=50)

    @model_validator(mode="after")
    def _one_id(self) -> "ReactomePathwaysInput":
        if not (self.gene or self.uniprot):
            raise ValueError("give a gene symbol or a UniProt accession")
        return self


def _pathway(stid: str, name: str, in_disease: Any, lowest_level: Any = None) -> Dict[str, Any]:
    out = {"stId": stid, "name": name, "in_disease": bool(in_disease), "url": f"https://reactome.org/content/detail/{stid}"}
    if lowest_level is not None:
        out["lowest_level"] = bool(lowest_level)
    return out


async def reactome_pathways(gene: Optional[str] = None, uniprot: Optional[str] = None, limit: int = 15) -> Any:
    """Human Reactome pathways for a UniProt accession (ContentService) or a gene symbol (AnalysisService)."""
    subject = uniprot or (gene or "").upper()
    async def lookup(http: Any) -> Any:
        if uniprot:
            rows = await common.get_json(
                http, f"{CONTENT_URL}/data/mapping/UniProt/{quote(uniprot)}/pathways", source="Reactome",
                params={"species": "9606"},
            )
            found = [
                _pathway(str(r["stId"]), str(r.get("displayName") or r.get("name") or ""), r.get("isInDisease"))
                for r in rows or [] if isinstance(r, dict) and r.get("stId")
            ]
            return found, "ContentService UniProt mapping", len(found)
        doc = await common.get_json(
            http, f"{ANALYSIS_URL}/identifier/{quote((gene or '').upper())}/projection", source="Reactome",
            params={"species": "9606", "pageSize": limit, "page": 1},
        )
        found = [
            _pathway(str(r["stId"]), str(r.get("name") or ""), r.get("inDisease"), r.get("llp"))
            for r in (doc or {}).get("pathways") or [] if isinstance(r, dict) and r.get("stId")
        ]
        return found, "AnalysisService identifier mapping", (doc or {}).get("pathwaysFound", len(found))

    try:
        async with common.client(15.0) as http:
            # The database release is read next to the lookup (best effort, cached; DAT-010).
            (pathways, method, total), release = await asyncio.gather(
                lookup(http), common.release_version(http, VERSION_URL, source="Reactome")
            )
    except Exception as exc:  # noqa: BLE001
        return common.failure(TOOL_NAME, exc, subject)
    pathways = [p for p in pathways if p["stId"].startswith("R-HSA-")]
    items = pathways[:limit]
    data = {
        "subject": subject,
        "gene": (gene or "").upper() or None,
        "uniprot": uniprot,
        "method": method,
        "total_pathways": total,
        "reactome_release": release,
        "items": items,
        "citation_records": [
            {"type": "url", "id": p["stId"], "title": f"Reactome pathway {p['stId']}: {p['name']}", "url": p["url"],
             "section": "Reactome pathway"}
            for p in items[:8]
        ],
    }
    return common.ok(TOOL_NAME, data, {"uniprot_id": uniprot or ""},
                     metadata={"source_release": f"Reactome release {release}" if release else None})


def tool_entry() -> Any:
    from ..tools.registry import AdapterType, ToolUniverseEntry

    return ToolUniverseEntry(
        name=TOOL_NAME,
        description=(
            "Reactome Pathway Knowledgebase (ContentService / AnalysisService): human pathways containing a gene "
            "product, by UniProt accession or gene symbol, with stable ids (R-HSA-...) and disease flags."
        ),
        domain=common.DOMAIN,
        adapter_type=AdapterType.DIRECT_REST,
        input_schema=ReactomePathwaysInput,
        canonical_id_types=["reactome_id", "uniprot_id"],
        timeout_seconds=20.0,
        tags=["pathway", "pathways", "reactome", "signaling", "mechanism", "biological_process", "upstream:reactome"],
        is_async=True,
    )


class ReactomeSource(common.OmicsSource):
    key = SOURCE_KEY
    action = "Reactome pathway lookup"
    tool_name = TOOL_NAME
    artifact_name = "reactome"
    label = "Reactome"
    nothing_found = "pathways"
    name_re = NAME_RE
    order = 63

    def intent(self, ctx: QueryContext) -> bool:
        return bool(INTENT_RE.search(common.query_text(ctx)))

    def build_plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        gene = common.gene_symbol(ctx)
        acc = common.uniprot_accession(ctx)
        if acc:
            return {"uniprot": acc, "_subject": f"{gene} ({acc})" if gene else acc}
        return {"gene": gene, "_subject": gene} if gene else None

    def compact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {k: data.get(k) for k in ("gene", "uniprot", "method", "total_pathways", "reactome_release", "items")}

    def evidence_xml(self, result: Any) -> str:
        data = common.data_of(result)
        lines = [
            f'<reactome_pathways source="Reactome ({common.attr(data.get("method"))})" query="{common.attr(data.get("subject"))}" '
            + (f'release="{common.attr(data.get("reactome_release"))}" ' if data.get("reactome_release") else "")
            + f'species="Homo sapiens" total="{common.attr(data.get("total_pathways"))}" note="Pathway membership only: '
            'it shows the gene product takes part in the pathway, not its effect on it. Cite the R-HSA id.">'
        ]
        for it in self.items(data):
            lines.append(
                f'<pathway stId="{common.attr(it.get("stId"))}" name="{common.attr(it.get("name"))}" '
                f'in_disease="{str(bool(it.get("in_disease"))).lower()}"/>'
            )
        lines.append("</reactome_pathways>")
        return "\n".join(lines)

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        data = common.data_of(result)
        out: Dict[str, Set[str]] = {"reactome": {str(i["stId"]) for i in self.items(data) if i.get("stId")}}
        if data.get("uniprot"):
            out["uniprot"] = {str(data["uniprot"])}
        return out
