"""
NHGRI-EBI GWAS Catalog associations for a gene, trait or rsID (REST API v2 only).

References: Sollis E. et al., "The NHGRI-EBI GWAS Catalog: knowledgebase and deposition resource", Nucleic Acids
Res. 2023;51(D1):D977-D985, doi:10.1093/nar/gkac1010; Cerezo M. et al., "The NHGRI-EBI GWAS Catalog: standards for
reusability, sustainability and diversity", Nucleic Acids Res. 2025;53(D1):D998-D1005, doi:10.1093/nar/gkae1070.

API: https://www.ebi.ac.uk/gwas/rest/api/v2 (the v1 API was retired in August 2026 and answers 410 Gone; the
summary-statistics API is retired too). `GET /v2/associations` accepts mapped_gene, efo_id, efo_trait, rs_id,
sort=p_value&direction=asc, page and size (OpenAPI at /gwas/rest/api/v2/rest-api-doc.yaml, checked 2026-10-16).
Each association carries pvalue_mantissa/pvalue_exponent (p_value itself underflows to 0.0 below 1e-308, so the
p-value is reported from mantissa and exponent), snp_allele [{rs_id, effect_allele}], efo_traits, reported_trait,
or_value or beta with range (the 95% CI), risk_frequency, accession_id (GCST study) and pubmed_id.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field, model_validator

from ..agents.query_context import QueryContext
from . import common

TOOL_NAME = "gwas_catalog_associations"
SOURCE_KEY = "gwas"
BASE_URL = "https://www.ebi.ac.uk/gwas/rest/api/v2"
STUDY_URL = "https://www.ebi.ac.uk/gwas/studies/{}"
GCST_RE = re.compile(r"\bGCST\d{6,9}\b")

INTENT_RE = re.compile(
    r"\b(gwas|genome[\s-]?wide|genetic\s+(?:association|risk|architecture|basis|variants?|loci|susceptibility|evidence)|"
    r"genetics\s+of|heritab\w*|snps?|polymorphisms?|risk\s+(?:loci|locus|alleles?|variants?)|common\s+variants?|"
    r"(?:variants?|loci|snps?)\s+associated\s+with|susceptibility\s+(?:loci|locus|genes?)|association\s+stud\w*|"
    r"traits?)\b",
    re.I,
)
NAME_RE = re.compile(r"\b(gwas\s+catalog|gwas)\b", re.I)


class GwasAssociationsInput(BaseModel):
    gene: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9\-]{0,14}$", description="HGNC symbol (mapped gene)")
    rs_id: Optional[str] = Field(default=None, pattern=r"^rs\d{1,12}$", description="dbSNP rsID")
    efo_id: Optional[str] = Field(default=None, pattern=r"^(?:EFO|MONDO|Orphanet|HP|DOID|GO)_\d{4,9}$", description="trait ontology id, e.g. MONDO_0005148")
    efo_trait: Optional[str] = Field(default=None, min_length=2, max_length=200, description="exact EFO trait label")
    size: int = Field(default=10, ge=1, le=50, description="associations returned, strongest p-value first")

    @model_validator(mode="after")
    def _one_filter(self) -> "GwasAssociationsInput":
        if not (self.gene or self.rs_id or self.efo_id or self.efo_trait):
            raise ValueError("give a gene, rs_id, efo_id or efo_trait")
        return self


def _present(value: Any) -> Optional[str]:
    text = "" if value is None else str(value).strip()
    return text if text and text not in ("-", "NR", "NA", "null") else None


def p_value_text(a: Dict[str, Any]) -> Optional[str]:
    mant, exp = a.get("pvalue_mantissa"), a.get("pvalue_exponent")
    if isinstance(mant, (int, float)) and isinstance(exp, int):
        return f"{mant:g}e{exp}"
    pv = a.get("p_value")
    return f"{pv:g}" if isinstance(pv, (int, float)) and pv > 0 else None


def parse_association(a: Dict[str, Any]) -> Dict[str, Any]:
    alleles = [s for s in a.get("snp_allele") or [] if isinstance(s, dict)]
    traits = [t for t in a.get("efo_traits") or [] if isinstance(t, dict)]
    study = str(a.get("accession_id") or "")
    out: Dict[str, Any] = {
        "association_id": a.get("association_id"),
        "rsid": next((str(s.get("rs_id")) for s in alleles if s.get("rs_id")), None),
        "risk_allele": ", ".join(str(x) for x in a.get("snp_effect_allele") or []) or None,
        "p_value": p_value_text(a),
        "p_value_note": _present(a.get("pvalue_description")),
        "trait": "; ".join(str(t.get("efo_trait")) for t in traits if t.get("efo_trait")) or None,
        "efo_ids": [str(t.get("efo_id")) for t in traits if t.get("efo_id")],
        "reported_trait": "; ".join(str(x) for x in a.get("reported_trait") or []) or None,
        "odds_ratio": _present(a.get("or_value")),
        "beta": _present(a.get("beta")),
        "ci_95": _present(a.get("range")),
        "risk_allele_frequency": _present(a.get("risk_frequency")),
        "mapped_genes": [str(g) for g in a.get("mapped_genes") or []],
        "location": (a.get("locations") or [None])[0],
        "study": study or None,
        "pmid": _present(a.get("pubmed_id")),
        "first_author": _present(a.get("first_author")),
        "url": STUDY_URL.format(study) if study else None,
    }
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def _subject(gene: Optional[str], rs_id: Optional[str], efo_id: Optional[str], efo_trait: Optional[str]) -> str:
    parts = []
    if rs_id:
        parts.append(rs_id)
    if gene:
        parts.append(f"gene {gene}")
    if efo_trait or efo_id:
        parts.append(f"trait {efo_trait or efo_id}")
    return ", ".join(parts)


def _citations(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for it in items:
        pmid = it.get("pmid")
        if pmid and pmid not in seen:
            seen.add(pmid)
            out.append(
                {
                    "type": "pmid",
                    "id": pmid,
                    "title": f"GWAS Catalog study {it.get('study')}: {it.get('reported_trait') or it.get('trait') or ''}".strip(),
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    "authors": it.get("first_author"),
                    "section": "GWAS Catalog association",
                }
            )
        study = it.get("study")
        if study and study not in seen:
            seen.add(study)
            out.append({"type": "url", "id": study, "title": f"GWAS Catalog study {study}", "url": it.get("url")})
    return out[:20]


async def gwas_associations(
    gene: Optional[str] = None,
    rs_id: Optional[str] = None,
    efo_id: Optional[str] = None,
    efo_trait: Optional[str] = None,
    size: int = 10,
) -> Any:
    """The strongest GWAS Catalog associations (by p-value) for the given filters."""
    subject = _subject(gene, rs_id, efo_id, efo_trait)
    params: Dict[str, Any] = {"sort": "p_value", "direction": "asc", "size": size, "page": 0}
    if gene:
        params["mapped_gene"] = gene.upper()
    if rs_id:
        params["rs_id"] = rs_id.lower()
    if efo_id:
        params["efo_id"] = efo_id
    elif efo_trait:
        params["efo_trait"] = efo_trait
    try:
        async with common.client(15.0) as http:
            doc = await common.get_json(http, f"{BASE_URL}/associations", source="GWAS Catalog", params=params)
    except Exception as exc:  # noqa: BLE001
        return common.failure(TOOL_NAME, exc, subject)
    raw = ((doc or {}).get("_embedded") or {}).get("associations") or []
    items = [parse_association(a) for a in raw if isinstance(a, dict)]
    page = (doc or {}).get("page") or {}
    data = {
        "subject": subject,
        "query": {k: v for k, v in params.items() if k not in ("page",)},
        "api": "GWAS Catalog REST API v2",
        "total_associations": page.get("totalElements", len(items)),
        "items": items,
        "citation_records": _citations(items),
    }
    return common.ok(TOOL_NAME, data, {"rsids": ",".join(sorted({i["rsid"] for i in items if i.get("rsid")}))})


def tool_entry() -> Any:
    from ..tools.registry import AdapterType, ToolUniverseEntry

    return ToolUniverseEntry(
        name=TOOL_NAME,
        description=(
            "NHGRI-EBI GWAS Catalog (REST API v2): genome-wide association results for a gene, trait (EFO/MONDO id "
            "or label) or rsID, strongest p-value first, with risk allele, p-value, odds ratio or beta with 95% CI, "
            "study accession (GCST) and PubMed id."
        ),
        domain=common.DOMAIN,
        adapter_type=AdapterType.DIRECT_REST,
        input_schema=GwasAssociationsInput,
        canonical_id_types=["rsid", "efo_id", "pmid", "gcst"],
        timeout_seconds=20.0,
        tags=["gwas", "genetics", "snp", "variant", "trait", "association", "heritability", "rsid", "gwas_catalog", "upstream:gwas_catalog"],
        is_async=True,
    )


class GwasSource(common.OmicsSource):
    key = SOURCE_KEY
    action = "GWAS Catalog association lookup"
    tool_name = TOOL_NAME
    artifact_name = "gwas"
    label = "GWAS Catalog"
    subject_kind = "gene, trait or rsID"
    nothing_found = "associations"
    name_re = NAME_RE
    order = 60

    def intent(self, ctx: QueryContext) -> bool:
        return bool(INTENT_RE.search(common.query_text(ctx)))

    def build_plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        rs = common.rsids(ctx)
        gene = common.gene_symbol(ctx)
        efo_id, trait = common.disease_trait(ctx)
        if rs:
            plan: Dict[str, Any] = {"rs_id": rs[0]}
            subject = rs[0]
        elif gene:
            plan = {"gene": gene}
            if efo_id:
                plan["efo_id"] = efo_id
            subject = f"{gene}" + (f" and {trait or efo_id}" if efo_id else "")
        elif efo_id or trait:
            plan = {"efo_id": efo_id} if efo_id else {"efo_trait": trait}
            subject = trait or efo_id or ""
        else:
            return None
        plan["_subject"] = subject
        return plan

    def compact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "subject": data.get("subject"),
            "total_associations": data.get("total_associations"),
            "items": [
                {k: it.get(k) for k in ("rsid", "risk_allele", "p_value", "trait", "odds_ratio", "beta", "ci_95", "study", "pmid", "url") if it.get(k) is not None}
                for it in self.items(data)[:25]
            ],
        }

    def evidence_xml(self, result: Any) -> str:
        data = common.data_of(result)
        items = self.items(data)
        lines = [
            f'<gwas_catalog source="NHGRI-EBI GWAS Catalog REST API v2" query="{common.attr(data.get("subject"))}" '
            f'total_associations="{common.attr(data.get("total_associations"))}" shown="{len(items)}" '
            'order="strongest p-value first" note="Quote p-values, odds ratios and betas exactly as given, with the '
            'study accession; an association is statistical, not causal, and its mapped gene may not be the causal gene.">'
        ]
        for it in items:
            fields = {
                "rsid": it.get("rsid"),
                "risk_allele": it.get("risk_allele"),
                "p_value": it.get("p_value"),
                "p_value_note": it.get("p_value_note"),
                "trait": it.get("trait"),
                "reported_trait": it.get("reported_trait"),
                "odds_ratio": it.get("odds_ratio"),
                "beta": it.get("beta"),
                "ci_95": it.get("ci_95"),
                "risk_allele_frequency": it.get("risk_allele_frequency"),
                "mapped_genes": ",".join(it.get("mapped_genes") or []),
                "location": it.get("location"),
                "study": it.get("study"),
                "pmid": it.get("pmid"),
                "first_author": it.get("first_author"),
            }
            lines.append("<association " + " ".join(f'{k}="{common.attr(v)}"' for k, v in fields.items() if v) + "/>")
        lines.append("</gwas_catalog>")
        return "\n".join(lines)

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        items = self.items(common.data_of(result))
        return {
            "rsid": {str(i["rsid"]).lower() for i in items if i.get("rsid")},
            "pmids": {str(i["pmid"]) for i in items if i.get("pmid")},
            "ontology": {e for i in items for e in i.get("efo_ids") or []},
            "gwas_study": {str(i["study"]) for i in items if i.get("study")},
        }
