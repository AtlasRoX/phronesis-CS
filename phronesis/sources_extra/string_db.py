"""
STRING protein-protein association partners for one human gene, from the pinned STRING 12.5 release.

References: Szklarczyk D. et al., "The STRING database in 2023: protein-protein association networks and
functional enrichment analyses for any sequenced genome of interest", Nucleic Acids Res. 2023;51(D1):D638-D646,
doi:10.1093/nar/gkac1000; Szklarczyk D. et al., "The STRING database in 2025: protein networks with directionality
of regulation", Nucleic Acids Res. 2025;53(D1):D730-D737, doi:10.1093/nar/gkae1113.

API: https://version-12-5.string-db.org/api/json/... (the versioned address keeps results reproducible).
`interaction_partners?identifiers=<gene>&species=9606&required_score=700&limit=10` returns the top partners with
the combined `score` (0-1; 0.7 = STRING's "high confidence") and per-channel scores (escore experiments, dscore
databases, tscore text mining, ascore co-expression, nscore neighbourhood, fscore fusion, pscore co-occurrence).
`network?identifiers=<gene + partners>` gives the edges among them. An unknown name answers 404 with
[{"Error": "not found", ...}]. STRING scores are association confidence, not physical binding or affinity.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field

from ..agents.query_context import QueryContext
from . import common

TOOL_NAME = "string_interaction_partners"
SOURCE_KEY = "string_ppi"
BASE_URL = "https://version-12-5.string-db.org/api/json"
STRING_VERSION = "12.5"
SPECIES = 9606
CALLER = "phronesis"

INTENT_RE = re.compile(
    r"\b(interact\w*|interactors?|(?:binding|interaction)\s+partners?|partners?|protein[\s-]protein|ppi|"
    r"interactome|(?:protein|signalling|signaling|interaction)\s+networks?|network\s+neighbou?rs?|"
    r"(?:protein\s+)?complex(?:es)?\s+with|protein\s+complex(?:es)?|string[\s-]?db)\b",
    re.I,
)
NAME_RE = re.compile(r"\b(string[\s-]?db|string\s+(?:database|network))\b", re.I)
_DRUG_DRUG_RE = re.compile(r"\bdrug[\s-]+drug\s+interactions?\b", re.I)
_CHANNELS = {"escore": "experiments", "dscore": "databases", "tscore": "textmining", "ascore": "coexpression",
             "nscore": "neighborhood", "fscore": "fusion", "pscore": "cooccurrence"}


class StringPartnersInput(BaseModel):
    gene: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9\-]{0,14}$", description="HGNC symbol")
    required_score: int = Field(default=700, ge=400, le=1000, description="minimum combined score x1000 (700 = high confidence)")
    limit: int = Field(default=10, ge=1, le=50, description="partners returned, highest combined score first")


async def string_partners(gene: str, required_score: int = 700, limit: int = 10) -> Any:
    """Top STRING partners of one human protein with combined and per-channel scores."""
    symbol = gene.strip().upper()
    params = {"identifiers": symbol, "species": SPECIES, "required_score": required_score, "limit": limit, "caller_identity": CALLER}
    try:
        async with common.client(15.0) as http:
            rows = await common.get_json(http, f"{BASE_URL}/interaction_partners", source="STRING", params=params)
            if isinstance(rows, list) and rows and isinstance(rows[0], dict) and rows[0].get("Error"):
                raise common.UpstreamError(f"STRING did not find {symbol} in human (9606)", "not_found")
            rows = [r for r in rows or [] if isinstance(r, dict) and r.get("preferredName_B")]
            rows.sort(key=lambda r: -float(r.get("score") or 0))
            edges: Optional[int] = None
            partners = [str(r["preferredName_B"]) for r in rows[:limit]]
            if len(partners) >= 2:
                try:
                    net = await common.get_json(
                        http, f"{BASE_URL}/network", source="STRING",
                        params={"identifiers": "\r".join([symbol, *partners]), "species": SPECIES,
                                "required_score": required_score, "caller_identity": CALLER},
                    )
                    names = set(partners)
                    edges = sum(
                        1 for e in net or [] if isinstance(e, dict)
                        and e.get("preferredName_A") in names and e.get("preferredName_B") in names
                    )
                except common.UpstreamError:
                    edges = None  # the partner list stands on its own
    except Exception as exc:  # noqa: BLE001
        return common.failure(TOOL_NAME, exc, symbol)
    items: List[Dict[str, Any]] = []
    for r in rows[:limit]:
        items.append(
            {
                "partner": str(r.get("preferredName_B")),
                "partner_string_id": r.get("stringId_B"),
                "combined_score": r.get("score"),
                "channels": {name: r.get(k) for k, name in _CHANNELS.items() if isinstance(r.get(k), (int, float)) and r.get(k)},
            }
        )
    query_id = next((str(r.get("stringId_A")) for r in rows if r.get("stringId_A")), None)
    url = f"https://version-12-5.string-db.org/cgi/network?identifiers={symbol}&species={SPECIES}"
    data = {
        "subject": symbol,
        "gene": symbol,
        "string_id": query_id,
        "string_version": STRING_VERSION,
        "species": SPECIES,
        "required_score": required_score,
        "items": items,
        "edges_among_partners": edges,
        "citation_records": [
            {"type": "url", "id": f"STRING:{query_id or symbol}", "title": f"STRING {STRING_VERSION} network of {symbol} (human)",
             "url": url, "section": "STRING interaction partners"}
        ],
    }
    return common.ok(TOOL_NAME, data, metadata={"source_release": f"STRING {STRING_VERSION}"})


def tool_entry() -> Any:
    from ..tools.registry import AdapterType, ToolUniverseEntry

    return ToolUniverseEntry(
        name=TOOL_NAME,
        description=(
            "STRING 12.5 (pinned) protein-protein interaction network: the top interaction partners of a human "
            "protein with combined confidence score and evidence channels (experiments, databases, text mining, "
            "co-expression), plus the edges among those partners."
        ),
        domain=common.DOMAIN,
        adapter_type=AdapterType.DIRECT_REST,
        input_schema=StringPartnersInput,
        canonical_id_types=["string_id"],
        timeout_seconds=20.0,
        tags=["protein_interaction", "ppi", "interaction", "partners", "network", "string", "interactome", "complex", "upstream:string"],
        is_async=True,
    )


class StringSource(common.OmicsSource):
    key = SOURCE_KEY
    action = "STRING interaction partner lookup"
    tool_name = TOOL_NAME
    artifact_name = "string"
    label = "STRING"
    nothing_found = "interaction partners"
    name_re = NAME_RE
    order = 62

    def intent(self, ctx: QueryContext) -> bool:
        text = common.query_text(ctx)
        return bool(INTENT_RE.search(text)) and not (_DRUG_DRUG_RE.search(text) and not NAME_RE.search(text))

    def build_plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        gene = common.gene_symbol(ctx)
        return {"gene": gene, "_subject": gene} if gene else None

    def compact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {k: data.get(k) for k in ("gene", "string_id", "string_version", "required_score", "items", "edges_among_partners")}

    def evidence_xml(self, result: Any) -> str:
        data = common.data_of(result)
        items = self.items(data)
        lines = [
            f'<string_network source="STRING {common.attr(data.get("string_version"))} (version-12-5.string-db.org)" '
            f'gene="{common.attr(data.get("gene"))}" species="9606" min_combined_score="{common.attr((data.get("required_score") or 0) / 1000)}" '
            f'partners="{len(items)}" edges_among_partners="{common.attr(data.get("edges_among_partners"))}" '
            'note="Combined scores (0-1) are STRING association confidence from all evidence channels, not physical '
            'binding or affinity; quote them exactly as given with STRING as the source.">'
        ]
        for it in items:
            ch = it.get("channels") or {}
            lines.append(
                f'<partner name="{common.attr(it.get("partner"))}" combined_score="{common.attr(it.get("combined_score"))}" '
                + " ".join(f'{common.attr(k)}="{common.attr(v)}"' for k, v in ch.items())
                + "/>"
            )
        lines.append("</string_network>")
        return "\n".join(lines)

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        data = common.data_of(result)
        ids = {str(i.get("partner_string_id")) for i in self.items(data) if i.get("partner_string_id")}
        if data.get("string_id"):
            ids.add(str(data["string_id"]))
        return {"string": ids}
