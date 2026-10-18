"""
PubChem compound properties for named drugs (PUG-REST).

Reference: Kim S. et al., "PubChem 2025 update", Nucleic Acids Res. 2025;53(D1):D1516-D1525, doi:10.1093/nar/gkae1059.

API: `GET https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/<name>/property/<props>/JSON` returns
{"PropertyTable": {"Properties": [{"CID", "MolecularFormula", "MolecularWeight", ...}]}}. PubChem now answers a
request for CanonicalSMILES with the key ConnectivitySMILES (and IsomericSMILES with SMILES), so both old and
new key names are read. An unknown name is 404 {"Fault": {"Code": "PUGREST.NotFound"}}. MolecularWeight comes back
as a string ("493.6") and is kept as given.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator

from ..agents.query_context import QueryContext
from . import common

TOOL_NAME = "pubchem_compound_properties"
SOURCE_KEY = "pubchem"
BASE_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PROPERTIES = "MolecularFormula,MolecularWeight,ConnectivitySMILES,CanonicalSMILES,SMILES,InChIKey,XLogP,IUPACName"

INTENT_RE = re.compile(
    r"\b(chemi\w*|chemical\s+structures?|structures?|smiles|inchi\w*|molecular\s+(?:weight|formula|mass|properties)|"
    r"formula|x?logp|lipophilic\w*|physico[\s-]?chemical|properties|pubchem|cid)\b",
    re.I,
)
NAME_RE = re.compile(r"\bpubchem\b", re.I)


class PubchemPropertiesInput(BaseModel):
    names: List[str] = Field(..., min_length=1, max_length=3, description="drug or compound names")

    @field_validator("names")
    @classmethod
    def _clean(cls, names: List[str]) -> List[str]:
        out = []
        for n in names:
            n = str(n).strip()
            if not (1 <= len(n) <= 100) or re.search(r"[/\\?#]", n):
                raise ValueError(f"unsupported compound name: {n[:40]!r}")
            out.append(n)
        return out


def parse_properties(query: str, prop: Dict[str, Any], n_cids: int) -> Dict[str, Any]:
    cid = prop.get("CID")
    out = {
        "query": query,
        "cid": cid,
        "molecular_formula": prop.get("MolecularFormula"),
        "molecular_weight": prop.get("MolecularWeight"),
        "connectivity_smiles": prop.get("ConnectivitySMILES") or prop.get("CanonicalSMILES"),
        "smiles": prop.get("SMILES") or prop.get("IsomericSMILES"),
        "inchikey": prop.get("InChIKey"),
        "xlogp": prop.get("XLogP"),
        "iupac_name": prop.get("IUPACName"),
        "n_cids_for_name": n_cids,
        "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}" if cid else None,
    }
    return {k: v for k, v in out.items() if v is not None}


async def _one(http: Any, name: str) -> Dict[str, Any]:
    try:
        doc = await common.get_json(
            http, f"{BASE_URL}/compound/name/{quote(name, safe='')}/property/{PROPERTIES}/JSON", source="PubChem"
        )
    except common.UpstreamError as exc:
        return {"name": name, "status": "not_found" if exc.kind == "not_found" else "error", "detail": exc.detail, "kind": exc.kind}
    props = [p for p in ((doc or {}).get("PropertyTable") or {}).get("Properties") or [] if isinstance(p, dict) and p.get("CID")]
    if not props:
        return {"name": name, "status": "not_found", "detail": "no CID", "kind": "not_found"}
    return {"name": name, "status": "ok", "item": parse_properties(name, props[0], len(props))}


async def pubchem_properties(names: List[str]) -> Any:
    """Formula, weight, SMILES, InChIKey and XLogP of the first PubChem CID for each name."""
    subject = ", ".join(names)
    try:
        async with common.client(15.0) as http:
            looked = await asyncio.gather(*(_one(http, n) for n in names))
    except Exception as exc:  # noqa: BLE001
        return common.failure(TOOL_NAME, exc, subject)
    items = [x["item"] for x in looked if x["status"] == "ok"]
    sources = [{"name": x["name"], "status": x["status"], **({"detail": x["detail"]} if x.get("detail") else {})} for x in looked]
    if not items:
        kinds = {x.get("kind") for x in looked}
        detail = "; ".join(f"{x['name']}: {x.get('detail')}" for x in looked)
        kind = "not_found" if kinds == {"not_found"} else "timeout" if kinds == {"timeout"} else "upstream"
        return common.failure(TOOL_NAME, common.UpstreamError(f"PubChem: {detail}", kind), subject)
    data = {
        "subject": subject,
        "items": items,
        "sources": sources,
        "citation_records": [
            {"type": "url", "id": f"PubChem CID {it['cid']}", "title": f"PubChem compound {it['cid']} ({it['query']})",
             "url": it.get("url"), "section": "PubChem computed properties"}
            for it in items
        ],
    }
    return common.ok(TOOL_NAME, data, {"pubchem_cids": ",".join(str(i["cid"]) for i in items)})


def tool_entry() -> Any:
    from ..tools.registry import AdapterType, ToolUniverseEntry

    return ToolUniverseEntry(
        name=TOOL_NAME,
        description=(
            "PubChem PUG-REST compound properties by drug or compound name: CID, molecular formula, molecular "
            "weight, connectivity (canonical) and isomeric SMILES, InChIKey, XLogP and IUPAC name."
        ),
        domain=common.DOMAIN,
        adapter_type=AdapterType.DIRECT_REST,
        input_schema=PubchemPropertiesInput,
        canonical_id_types=["pubchem_cid", "inchikey"],
        timeout_seconds=20.0,
        tags=["chemistry", "compound", "pubchem", "smiles", "molecular_weight", "properties", "drug", "upstream:pubchem"],
        is_async=True,
    )


class PubchemSource(common.OmicsSource):
    key = SOURCE_KEY
    action = "PubChem compound property lookup"
    tool_name = TOOL_NAME
    artifact_name = "pubchem"
    label = "PubChem"
    subject_kind = "drug"
    nothing_found = "compound records"
    name_re = NAME_RE
    order = 64

    def intent(self, ctx: QueryContext) -> bool:
        return bool(INTENT_RE.search(common.query_text(ctx)))

    def build_plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        names: List[str] = []
        for d in ctx.drugs:
            # The user's own wording first (a trade name resolves in PubChem too), then the resolved label.
            name = (d.mention or d.label or "").strip()
            if name and not re.search(r"[/\\?#]", name) and name.lower() not in {n.lower() for n in names}:
                names.append(name[:100])
        names = names[:3]
        return {"names": names, "_subject": ", ".join(names)} if names else None

    def compact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"items": self.items(data), "sources": data.get("sources")}

    def evidence_xml(self, result: Any) -> str:
        data = common.data_of(result)
        lines = [
            '<pubchem_compounds source="PubChem PUG-REST" note="Computed descriptors for the first CID PubChem '
            'returns for each name; quote them exactly as given with the CID.">'
        ]
        for it in self.items(data):
            fields = {k: it.get(k) for k in ("query", "cid", "molecular_formula", "molecular_weight", "xlogp", "inchikey",
                                             "connectivity_smiles", "smiles", "iupac_name", "n_cids_for_name")}
            lines.append("<compound " + " ".join(f'{k}="{common.attr(v)}"' for k, v in fields.items() if v is not None) + "/>")
        for s in data.get("sources") or []:
            if s.get("status") != "ok":
                lines.append(f'<not_found name="{common.attr(s.get("name"))}" detail="{common.attr(s.get("detail"))}"/>')
        lines.append("</pubchem_compounds>")
        return "\n".join(lines)

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        items = self.items(common.data_of(result))
        return {
            "pubchem_cid": {str(i["cid"]) for i in items if i.get("cid")},
            "inchikey": {str(i["inchikey"]) for i in items if i.get("inchikey")},
        }

    def scope_notes(self, ctx: QueryContext, result: Optional[Any]) -> List[str]:
        notes = super().scope_notes(ctx, result)
        if result is not None and result.success:
            missing = [s["name"] for s in common.data_of(result).get("sources") or [] if s.get("status") != "ok"]
            if missing:
                notes.append(f"PubChem returned no compound for {', '.join(missing)}; do not state PubChem properties for it.")
        return notes
