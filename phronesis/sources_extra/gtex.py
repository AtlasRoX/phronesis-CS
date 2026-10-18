"""
GTEx median gene expression (TPM) across human tissues.

Reference: The GTEx Consortium, "The GTEx Consortium atlas of genetic regulatory effects across human tissues",
Science 2020;369(6509):1318-1330, doi:10.1126/science.aaz1776 (GTEx v8, the dataset queried here).

API: https://gtexportal.org/api/v2/. `GET reference/gene?geneId=<symbol>` returns the versioned gencodeId
(e.g. ENSG00000169174.10 for PCSK9, GENCODE v26), then `GET expression/medianGeneExpression?gencodeId=...&
datasetId=gtex_v8` returns one row per tissue {median, tissueSiteDetailId, ontologyId, unit: TPM}. Without a
datasetId, or with a gencodeId of another GENCODE version, the endpoint returns an empty list, so both are pinned.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field

from ..agents.query_context import QueryContext
from . import common

TOOL_NAME = "gtex_median_expression"
SOURCE_KEY = "gtex"
BASE_URL = "https://gtexportal.org/api/v2"
DATASET = "gtex_v8"

INTENT_RE = re.compile(
    r"\b(expression|expressed|tissues?|tpm|transcript\s+(?:levels?|abundance)|mrna\s+(?:levels?|abundance)|"
    r"tissue[\s-]specific\w*|gtex)\b",
    re.I,
)
NAME_RE = re.compile(r"\bgtex\b", re.I)
_TISSUE_RE = re.compile(r"\b(tissues?|tissue[\s-]specific\w*|organs?|gtex)\b", re.I)


class GtexExpressionInput(BaseModel):
    gene: str = Field(..., pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9\-]{0,14}|ENSG\d{11}(?:\.\d+)?)$", description="HGNC symbol or Ensembl gene id")
    dataset_id: str = Field(default=DATASET, pattern=r"^gtex_v\d{1,2}$")
    top: int = Field(default=10, ge=1, le=60, description="tissues returned, highest median TPM first")


def _num(value: Any) -> Optional[float]:
    try:
        return float(f"{float(value):.6g}")  # drop float noise (0.13387000000000002), keep the upstream precision
    except (TypeError, ValueError):
        return None


def _tissue_label(tissue_id: str) -> str:
    return tissue_id.replace("_", " ")


async def gtex_expression(gene: str, dataset_id: str = DATASET, top: int = 10) -> Any:
    """Tissues ranked by GTEx median TPM for one gene."""
    query = gene.strip()
    try:
        async with common.client(15.0) as http:
            ref = await common.get_json(http, f"{BASE_URL}/reference/gene", source="GTEx", params={"geneId": query})
            genes = [g for g in (ref or {}).get("data") or [] if isinstance(g, dict)]
            match = next(
                (g for g in genes if str(g.get("geneSymbolUpper") or g.get("geneSymbol") or "").upper() == query.upper()
                 or str(g.get("gencodeId") or "").split(".")[0] == query.split(".")[0].upper()),
                None,
            )
            if match is None:
                raise common.UpstreamError(f"GTEx has no gene matching {query}", "not_found")
            gencode = str(match.get("gencodeId"))
            med = await common.get_json(
                http, f"{BASE_URL}/expression/medianGeneExpression", source="GTEx",
                params={"gencodeId": gencode, "datasetId": dataset_id},
            )
    except Exception as exc:  # noqa: BLE001
        return common.failure(TOOL_NAME, exc, query)
    rows = [r for r in (med or {}).get("data") or [] if isinstance(r, dict) and _num(r.get("median")) is not None]
    rows.sort(key=lambda r: -float(r["median"]))
    items: List[Dict[str, Any]] = [
        {
            "tissue": str(r.get("tissueSiteDetailId") or ""),
            "tissue_label": _tissue_label(str(r.get("tissueSiteDetailId") or "")),
            "median_tpm": _num(r.get("median")),
            "unit": r.get("unit") or "TPM",
            "uberon": r.get("ontologyId"),
        }
        for r in rows[:top]
    ]
    symbol = str(match.get("geneSymbol") or query)
    data = {
        "subject": symbol,
        "gene_symbol": symbol,
        "gencode_id": gencode,
        "gencode_version": match.get("gencodeVersion"),
        "dataset_id": dataset_id,
        "n_tissues": len(rows),
        "lowest": [{"tissue": str(r.get("tissueSiteDetailId")), "median_tpm": _num(r.get("median"))} for r in rows[-3:]] if len(rows) > top else [],
        "items": items,
        "citation_records": [
            {
                "type": "url",
                "id": f"GTEx:{gencode}",
                "title": f"GTEx Portal ({dataset_id}) median gene expression: {symbol}",
                "url": f"https://gtexportal.org/home/gene/{symbol}",
                "section": "GTEx median TPM per tissue",
            }
        ],
    }
    release = f"GTEx {dataset_id}" + (f", GENCODE v{match.get('gencodeVersion')}" if match.get("gencodeVersion") else "")
    return common.ok(TOOL_NAME, data, {"ensembl_id": gencode.split(".")[0]}, metadata={"source_release": release})


def tool_entry() -> Any:
    from ..tools.registry import AdapterType, ToolUniverseEntry

    return ToolUniverseEntry(
        name=TOOL_NAME,
        description=(
            "GTEx Portal API v2: median gene expression (TPM) of one gene across 54 normal human tissues (GTEx v8), "
            "tissues ranked from highest to lowest median TPM."
        ),
        domain=common.DOMAIN,
        adapter_type=AdapterType.DIRECT_REST,
        input_schema=GtexExpressionInput,
        canonical_id_types=["ensembl_id"],
        timeout_seconds=20.0,
        tags=["expression", "tissue", "gtex", "tpm", "transcriptomics", "rna", "normal_tissue", "upstream:gtex"],
        is_async=True,
    )


class GtexSource(common.OmicsSource):
    key = SOURCE_KEY
    action = "GTEx tissue expression lookup"
    tool_name = TOOL_NAME
    artifact_name = "gtex"
    label = "GTEx"
    nothing_found = "tissue expression values"
    name_re = NAME_RE
    order = 61

    def intent(self, ctx: QueryContext) -> bool:
        text = common.query_text(ctx)
        if not INTENT_RE.search(text):
            return False
        # Expression in an attached single-cell dataset is Lumicell's job, unless tissues are asked about.
        if ctx.has_single_cell_intent or ctx.dataset_attachment is not None:
            return bool(_TISSUE_RE.search(text))
        return True

    def build_plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        gene = common.gene_symbol(ctx)
        return {"gene": gene, "_subject": gene} if gene else None

    def describe(self, result: Any, ctx: QueryContext):
        if not result.success:
            return super().describe(result, ctx)
        data = common.data_of(result)
        top = (self.items(data) or [{}])[0]
        return (
            f"GTEx: {data.get('gene_symbol')} expression across {data.get('n_tissues')} tissues",
            f"highest median: {top.get('tissue_label')} {top.get('median_tpm')} TPM" if top else "",
        )

    def compact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {k: data.get(k) for k in ("gene_symbol", "gencode_id", "dataset_id", "n_tissues", "items", "lowest")}

    def evidence_xml(self, result: Any) -> str:
        data = common.data_of(result)
        lines = [
            f'<gtex_expression source="GTEx Portal API v2" dataset="{common.attr(data.get("dataset_id"))}" '
            f'gene="{common.attr(data.get("gene_symbol"))}" gencode_id="{common.attr(data.get("gencode_id"))}" '
            f'n_tissues="{common.attr(data.get("n_tissues"))}" order="highest median TPM first" note="Bulk RNA-seq '
            'of normal post-mortem tissue: median TPM per tissue. Quote TPM values exactly as given with the tissue '
            'and GTEx as the source; they say nothing about disease tissue or protein level.">'
        ]
        for it in self.items(data):
            lines.append(
                f'<tissue name="{common.attr(it.get("tissue_label"))}" id="{common.attr(it.get("tissue"))}" '
                f'median_tpm="{common.attr(it.get("median_tpm"))}" unit="{common.attr(it.get("unit"))}"/>'
            )
        for it in data.get("lowest") or []:
            lines.append(
                f'<tissue name="{common.attr(_tissue_label(str(it.get("tissue") or "")))}" id="{common.attr(it.get("tissue"))}" '
                f'median_tpm="{common.attr(it.get("median_tpm"))}" unit="TPM" rank="lowest"/>'
            )
        lines.append("</gtex_expression>")
        return "\n".join(lines)

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        gid = str(common.data_of(result).get("gencode_id") or "").split(".")[0]
        return {"ensembl": {gid}} if gid else {}
