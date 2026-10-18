"""
Structure intelligence (feature 12): registry tool executors and the 'structure_intel' EvidenceSource.

For a protein target in a structure question (structure, AlphaFold, PDB, crystal, fold, binding pocket, docking,
ligand, pLDDT) the source reports:
- the AlphaFold DB model and its mean pLDDT with the AlphaFold confidence band (Jumper et al., Nature 2021,
  doi:10.1038/s41586-021-03819-2; AlphaFold DB, Varadi et al. 2022/2024, doi:10.1093/nar/gkab1061,
  doi:10.1093/nar/gkad1011); a model is a prediction, not an experimental structure;
- experimental RCSB PDB entries mapped to the UniProt accession, with method, resolution and bound ligands
  (Burley et al., NAR 2023, doi:10.1093/nar/gkac1077);
- only when the question explicitly asks and NVIDIA_API_KEY is set: DiffDock poses (Corso et al., ICLR 2023,
  arXiv:2210.01776) for a SMILES ligand named in the question, and MolMIM analogues (Reidenbach et al. 2022,
  arXiv:2208.09016) of a SMILES seed. Neither runs by default; ESMFold is not wired.

The UniProt accession comes from the resolved entities (gene ids.uniprot_id), an accession typed in the question,
or the `biocontext_uniprot` registry tool when one is registered; otherwise the source is skipped with a scope
note. AF model ids and PDB ids are returned by allowed_identifiers() under the families "alphafold" and "pdb"
(id_families.py has no registration API, so the verifier treats them as observed context ids, not checked
families).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from html import escape
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, Field, field_validator

from ..agents.query_context import QueryContext, SourceDecision
from ..sources import CitationRecord, EvidenceSource, SourceRunState
from ..tools.registry import ErrorType, ToolResult
from .clients import (
    AF_MODEL_ID_RE,
    PDB_ID_RE,
    RCSB_FILE_URL,
    UNIPROT_ACCESSION_RE,
    UNIPROT_IN_TEXT_RE,
    StructureClientError,
    get_client,
    nvidia_api_key,
)

logger = logging.getLogger(__name__)

SOURCE_KEY = "structure_intel"
AF_TOOL = "structure_alphafold_prediction"
PDB_TOOL = "structure_pdb_search"
DOCK_TOOL = "structure_diffdock"
MOLMIM_TOOL = "structure_molmim_generate"
UNIPROT_TOOL = "biocontext_uniprot"

STRUCTURE_INTENT_RE = re.compile(
    r"\b(?:structures?|structural(?:ly)?|alphafold|pdb|crystal\w*|cryo-?em|fold(?:s|ed|ing)?|"
    r"binding[ -](?:pocket|site)s?|pockets?|dock(?:s|ed|ing)?|ligands?|plddt|"
    # "in-silico binding predictions", "binding affinity prediction", "virtual screening": structure-based binding work.
    r"in[ -]?silico\s+(?:binding|docking|screening)|binding\s+(?:predictions?|affinit(?:y|ies)\s+predictions?|modell?ing)|"
    r"virtual\s+screening)\b",
    re.I,
)
# "2-fold", "fold change", "log2FC": expression wording, not protein folding.
_FOLD_CHANGE_RE = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*[- ]?\s*fold\b|\bfold[- ]?(?:change|enrichment|increase|decrease|higher|lower|difference)s?\b)",
    re.I,
)
DOCKING_RE = re.compile(
    r"\bdock(?:s|ed|ing)?\b|\bin[ -]?silico\s+binding\b|\bbinding\s+predictions?\b|\bvirtual\s+screening\b", re.I
)
GENERATE_RE = re.compile(r"\bgenerat\w*\s+(?:\w+\s+){0,3}?(?:analog(?:ue)?s|molecules|compounds|derivatives)\b", re.I)
_SMILES_TOKEN_RE = re.compile(r"(?<![\w])([A-Za-z0-9@+\-\[\]()=#$%/\\.]{5,300})(?![\w])")
_SMILES_CHARS_RE = re.compile(r"^[BCNOPSFIHKLMabcegilnoprsuZ0-9@+\-\[\]()=#$%/\\.]+$")

SCOPE_PREDICTION = (
    "AlphaFold DB models are computational predictions, not experimental structures: report the mean pLDDT with its "
    "band, and treat low-confidence (pLDDT < 70) regions as unreliable for pocket or interface claims."
)
SCOPE_DOCKING = (
    "DiffDock poses and confidence scores are model predictions of binding geometry, not measured affinities; never "
    "present them as binding constants or as evidence that the compound binds."
)


def has_structure_intent(text: str) -> bool:
    cleaned = _FOLD_CHANGE_RE.sub(" ", text or "")
    return bool(STRUCTURE_INTENT_RE.search(cleaned))


def wants_docking(text: str) -> bool:
    return bool(DOCKING_RE.search(text or ""))


def wants_generation(text: str) -> bool:
    return bool(GENERATE_RE.search(text or ""))


def smiles_in_text(text: str) -> Optional[str]:
    """The first SMILES-looking token: SMILES characters only, with a bond, branch, bracket or ring closure."""
    for m in _SMILES_TOKEN_RE.finditer(text or ""):
        tok = m.group(1).strip(".")
        if len(tok) < 5 or not _SMILES_CHARS_RE.match(tok):
            continue
        if re.search(r"[=#()\[\]]", tok) or re.search(r"[cnos]\d", tok):
            if not re.search(r"[A-Za-z]", tok):
                continue
            return tok
    return None


def typed_accession(text: str) -> Optional[str]:
    m = UNIPROT_IN_TEXT_RE.search(text or "")
    return m.group(0) if m else None


def _query_text(ctx: QueryContext) -> str:
    return ctx.clean_query or ctx.raw_query or ""


def accession_from_ctx(ctx: QueryContext) -> Tuple[Optional[str], str]:
    """(accession, how) from the resolved entities or the question; (None, "") when neither has one."""
    genes = ([ctx.primary_gene] if ctx.primary_gene is not None else []) + [g for g in ctx.genes if g is not ctx.primary_gene]
    for g in genes:
        acc = str((g.ids or {}).get("uniprot_id") or "").strip().upper()
        if acc and UNIPROT_ACCESSION_RE.match(acc):
            return acc, f"resolved entity {g.label}"
    acc = typed_accession(ctx.raw_query or _query_text(ctx))
    if acc:
        return acc, "typed in the question"
    return None, ""


async def resolve_accession(ctx: QueryContext, registry: Any) -> Tuple[Optional[str], str]:
    acc, how = accession_from_ctx(ctx)
    if acc:
        return acc, how
    symbol = ctx.primary_symbol
    if symbol and registry is not None and registry.get_entry(UNIPROT_TOOL) is not None:
        res = await registry.execute(UNIPROT_TOOL, query=f"{symbol} human", limit=1, reviewed_only=True)
        if getattr(res, "success", False):
            text = res.data if isinstance(res.data, str) else json.dumps(res.data, default=str)
            m = UNIPROT_IN_TEXT_RE.search(text or "")
            if m:
                return m.group(0), f"UniProt lookup for {symbol}"
    if symbol:
        # The BioContext UniProt tool is optional (it is not registered on every server): ask UniProt's REST API
        # directly for the reviewed human entry of the exact gene symbol.
        try:
            acc = await get_client().uniprot_accession(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.info("UniProt REST lookup for %s failed: %s", symbol, exc)
            acc = None
        if acc and UNIPROT_ACCESSION_RE.match(acc.upper()):
            return acc.upper(), f"UniProt lookup for {symbol}"
    return None, ""


# Registry tools --------------------------------------------------------------------------------------------------

class AlphaFoldInput(BaseModel):
    uniprot: str = Field(..., description="UniProt accession, e.g. P04637")

    @field_validator("uniprot")
    @classmethod
    def _acc(cls, value: str) -> str:
        value = value.strip().upper()
        if not UNIPROT_ACCESSION_RE.match(value):
            raise ValueError("not a UniProt accession")
        return value


class PdbSearchInput(AlphaFoldInput):
    max_entries: int = Field(default=8, ge=1, le=10)


class DiffDockInput(BaseModel):
    ligand_smiles: str = Field(..., min_length=3, max_length=500)
    uniprot: Optional[str] = None
    pdb_id: Optional[str] = None
    num_poses: int = Field(default=5, ge=1, le=20)

    @field_validator("uniprot")
    @classmethod
    def _acc(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip().upper()
        if not UNIPROT_ACCESSION_RE.match(value):
            raise ValueError("not a UniProt accession")
        return value

    @field_validator("pdb_id")
    @classmethod
    def _pdb(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip().upper()
        if not PDB_ID_RE.match(value):
            raise ValueError("not a PDB id")
        return value


class MolMimInput(BaseModel):
    smiles: str = Field(..., min_length=3, max_length=500)
    num_molecules: int = Field(default=10, ge=1, le=30)
    property_name: Literal["QED", "plogP"] = "QED"
    min_similarity: float = Field(default=0.3, ge=0.0, le=0.7)
    algorithm: Literal["CMA-ES", "none"] = "CMA-ES"


def _error(tool: str, exc: StructureClientError) -> ToolResult:
    kind = {"timeout": ErrorType.TIMEOUT, "invalid": ErrorType.VALIDATION_ERROR, "auth": ErrorType.VALIDATION_ERROR}
    return ToolResult(success=False, tool_name=tool, error=str(exc), error_type=kind.get(exc.kind, ErrorType.UPSTREAM_API_ERROR))


async def alphafold_prediction(uniprot: str) -> ToolResult:
    try:
        summary = await get_client().alphafold_prediction(uniprot)
    except StructureClientError as exc:
        return _error(AF_TOOL, exc)
    if not summary:
        return ToolResult(success=False, tool_name=AF_TOOL, error=f"AlphaFold DB has no model for {uniprot}", error_type=ErrorType.NOT_FOUND)
    return ToolResult(
        success=True,
        tool_name=AF_TOOL,
        data={"uniprot": uniprot, "alphafold": summary},
        canonical_ids={"uniprot_id": uniprot},
        metadata={"source": "AlphaFold DB"},
    )


async def pdb_search(uniprot: str, max_entries: int = 8) -> ToolResult:
    client = get_client()
    try:
        found = await client.pdb_search(uniprot, rows=max_entries)
    except StructureClientError as exc:
        return _error(PDB_TOOL, exc)
    entries = await client.pdb_entries(found["pdb_ids"]) if found["pdb_ids"] else []
    return ToolResult(
        success=True,
        tool_name=PDB_TOOL,
        data={"uniprot": uniprot, "total_count": found["total_count"], "entries": entries},
        canonical_ids={"uniprot_id": uniprot},
        metadata={"source": "RCSB PDB"},
    )


async def diffdock_pose(ligand_smiles: str, uniprot: Optional[str] = None, pdb_id: Optional[str] = None, num_poses: int = 5) -> ToolResult:
    if not nvidia_api_key():
        return ToolResult(success=False, tool_name=DOCK_TOOL, error="NVIDIA_API_KEY is not set; docking is unavailable", error_type=ErrorType.VALIDATION_ERROR)
    client = get_client()
    try:
        if pdb_id:
            receptor = {"source": "RCSB PDB", "id": pdb_id}
            text = await client.fetch_structure_text(RCSB_FILE_URL.format(pdb_id=pdb_id))
        elif uniprot:
            model = await client.alphafold_prediction(uniprot)
            if not model or not model.get("pdb_url"):
                return ToolResult(success=False, tool_name=DOCK_TOOL, error=f"no AlphaFold model file for {uniprot}", error_type=ErrorType.NOT_FOUND)
            receptor = {"source": "AlphaFold DB", "id": model["model_id"], "mean_plddt": model.get("mean_plddt")}
            text = await client.fetch_structure_text(model["pdb_url"])
        else:
            return ToolResult(success=False, tool_name=DOCK_TOOL, error="a uniprot accession or pdb_id is required", error_type=ErrorType.VALIDATION_ERROR)
        result = await client.diffdock(text, ligand_smiles, "txt", num_poses)
    except StructureClientError as exc:
        return _error(DOCK_TOOL, exc)
    return ToolResult(
        success=True,
        tool_name=DOCK_TOOL,
        data={"method": "DiffDock (NVIDIA NIM)", "ligand_smiles": ligand_smiles, "receptor": receptor, **result},
        metadata={"source": "NVIDIA NIM"},
    )


async def molmim_generate(smiles: str, num_molecules: int = 10, property_name: str = "QED", min_similarity: float = 0.3, algorithm: str = "CMA-ES") -> ToolResult:
    if not nvidia_api_key():
        return ToolResult(success=False, tool_name=MOLMIM_TOOL, error="NVIDIA_API_KEY is not set; generation is unavailable", error_type=ErrorType.VALIDATION_ERROR)
    try:
        result = await get_client().molmim_generate(smiles, num_molecules, property_name, min_similarity, algorithm)
    except StructureClientError as exc:
        return _error(MOLMIM_TOOL, exc)
    return ToolResult(
        success=True,
        tool_name=MOLMIM_TOOL,
        data={"method": "MolMIM (NVIDIA NIM)", "seed_smiles": smiles, "algorithm": algorithm, "min_similarity": min_similarity, **result},
        metadata={"source": "NVIDIA NIM"},
    )


# EvidenceSource --------------------------------------------------------------------------------------------------

def _data(result: Any) -> Dict[str, Any]:
    data = getattr(result, "data", None)
    return data if isinstance(data, dict) else {}


def _attr(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


class StructureIntelSource(EvidenceSource):
    key = SOURCE_KEY
    agent = "Synapta"
    action = "AlphaFold DB & RCSB PDB Structure Lookup"
    required_tools = (AF_TOOL, PDB_TOOL)
    order = 45

    def should_run(self, ctx: QueryContext) -> SourceDecision:
        text = _query_text(ctx)
        if not has_structure_intent(text):
            return SourceDecision(run=False, reason="no structure question")
        acc, how = accession_from_ctx(ctx)
        if acc:
            return ctx.gate_decision(self.key, SourceDecision(run=True, reason=f"structure lookup for {acc} ({how})"))
        if ctx.primary_symbol:
            # Resolved at run time through the UniProt tool when one is registered; otherwise skipped in run().
            return ctx.gate_decision(self.key, SourceDecision(run=True, reason=f"structure lookup for {ctx.primary_symbol}"))
        return SourceDecision(run=False, reason="no protein target named")

    def running_title(self, ctx: QueryContext) -> str:
        acc, _ = accession_from_ctx(ctx)
        return f"Looking up AlphaFold and PDB structures for {ctx.primary_display or acc or 'the target'}"

    def plan_fact(self, ctx: QueryContext) -> Optional[str]:
        acc, _ = accession_from_ctx(ctx)
        return f"Structure: AlphaFold DB + RCSB PDB ({acc or ctx.primary_symbol})"

    async def run(self, ctx: QueryContext, registry: Any, state: SourceRunState) -> ToolResult:
        acc, how = await resolve_accession(ctx, registry)
        if not acc:
            return ToolResult(
                success=False,
                tool_name=SOURCE_KEY,
                error=f"no UniProt accession could be resolved for {ctx.primary_symbol or 'the target'}",
                error_type=ErrorType.NOT_FOUND,
            )
        af_res, pdb_res = await asyncio.gather(
            registry.execute(AF_TOOL, uniprot=acc), registry.execute(PDB_TOOL, uniprot=acc, max_entries=8)
        )
        text = _query_text(ctx)
        data: Dict[str, Any] = {
            "uniprot": acc,
            "uniprot_source": how,
            "gene": ctx.primary_symbol,
            "alphafold": _data(af_res).get("alphafold") if af_res.success else None,
            "pdb_entries": list(_data(pdb_res).get("entries") or []) if pdb_res.success else [],
            "pdb_total_count": _data(pdb_res).get("total_count") if pdb_res.success else None,
            "docking": None,
            "generated": None,
            "notes": [],
            "errors": [],
        }
        if not af_res.success:
            data["errors"].append(f"AlphaFold DB: {str(af_res.error or '')[:160]}")
        if not pdb_res.success:
            data["errors"].append(f"RCSB PDB: {str(pdb_res.error or '')[:160]}")
        smiles = smiles_in_text(ctx.raw_query or text)
        if wants_docking(text):
            if not nvidia_api_key() or registry.get_entry(DOCK_TOOL) is None:
                data["notes"].append("Docking was requested but is not configured on this server (NVIDIA_API_KEY).")
            elif not smiles:
                data["notes"].append("Docking was requested but the question names no ligand SMILES.")
            else:
                dock = await registry.execute(DOCK_TOOL, ligand_smiles=smiles, uniprot=acc, num_poses=5)
                data["docking"] = _data(dock) if dock.success else {"status": "error", "error": str(dock.error or "")[:200]}
        if wants_generation(text):
            if not nvidia_api_key() or registry.get_entry(MOLMIM_TOOL) is None:
                data["notes"].append("Molecule generation was requested but is not configured on this server (NVIDIA_API_KEY).")
            elif not smiles:
                data["notes"].append("Molecule generation was requested but the question names no seed SMILES.")
            else:
                gen = await registry.execute(MOLMIM_TOOL, smiles=smiles, num_molecules=10)
                data["generated"] = _data(gen) if gen.success else {"status": "error", "error": str(gen.error or "")[:200]}
        success = bool(af_res.success or pdb_res.success)
        return ToolResult(
            success=success,
            tool_name=SOURCE_KEY,
            data=data,
            canonical_ids={"uniprot_id": acc},
            error=None if success else "; ".join(data["errors"])[:400],
            error_type=None if success else (af_res.error_type or ErrorType.UPSTREAM_API_ERROR),
            metadata={"source": "AlphaFold DB + RCSB PDB"},
        )

    def describe(self, result: ToolResult, ctx: QueryContext) -> Tuple[str, str]:
        if not result.success:
            err_type = getattr(result.error_type, "value", result.error_type) or "ERROR"
            return "Structure lookup failed", f"{err_type}: {str(result.error or '')[:240]}"
        data = _data(result)
        af = data.get("alphafold") or {}
        parts: List[str] = []
        if af:
            parts.append(f"AlphaFold {af.get('model_id')} mean pLDDT {af.get('mean_plddt')} ({af.get('plddt_band')})")
        n = len(data.get("pdb_entries") or [])
        total = data.get("pdb_total_count")
        title = f"Found {total if total is not None else n} PDB entr{'y' if (total or n) == 1 else 'ies'} for {data.get('uniprot')}"
        if data.get("docking"):
            parts.append("DiffDock poses predicted")
        parts.extend(data.get("notes") or [])
        return title, " · ".join(str(p) for p in parts)

    def evidence_xml(self, result: ToolResult) -> str:
        data = _data(result)
        out = [
            f'<structure_intel uniprot="{_attr(data.get("uniprot"))}" gene="{_attr(data.get("gene"))}" '
            f'source="AlphaFold DB, RCSB PDB" note="{_attr(SCOPE_PREDICTION)}">'
        ]
        af = data.get("alphafold")
        if isinstance(af, dict) and af:
            out.append(
                f'<alphafold_model model_id="{_attr(af.get("model_id"))}" version="{_attr(af.get("version"))}" '
                f'mean_plddt="{_attr(af.get("mean_plddt"))}" plddt_band="{_attr(af.get("plddt_band"))}" '
                f'residues="{_attr("-".join(str(x) for x in (af.get("sequence_range") or []) if x is not None))}" '
                f'url="{_attr(af.get("url"))}" kind="computational prediction"/>'
            )
        entries = data.get("pdb_entries") or []
        out.append(f'<pdb_entries total_count="{_attr(data.get("pdb_total_count"))}" shown="{len(entries)}">')
        for e in entries[:10]:
            if not isinstance(e, dict):
                continue
            out.append(
                f'<pdb_entry pdb_id="{_attr(e.get("pdb_id"))}" method="{_attr(e.get("method"))}" '
                f'resolution_angstrom="{_attr(e.get("resolution"))}" ligands="{_attr(", ".join(e.get("ligands") or []))}" '
                f'release_date="{_attr(e.get("release_date"))}" url="{_attr(e.get("url"))}">{escape(str(e.get("title") or ""))}</pdb_entry>'
            )
        out.append("</pdb_entries>")
        dock = data.get("docking")
        if isinstance(dock, dict) and dock:
            rec = dock.get("receptor") or {}
            out.append(
                f'<docking method="DiffDock (NVIDIA NIM)" status="{_attr(dock.get("status"))}" ligand_smiles="{_attr(dock.get("ligand_smiles"))}" '
                f'receptor="{_attr(rec.get("id"))}" num_poses="{_attr(dock.get("num_poses"))}" '
                f'top_confidence="{_attr(dock.get("top_confidence"))}" note="{_attr(SCOPE_DOCKING)}"/>'
            )
        gen = data.get("generated")
        if isinstance(gen, dict) and gen.get("molecules"):
            out.append(f'<generated_molecules method="MolMIM (NVIDIA NIM)" property="{_attr(gen.get("property_name"))}" '
                       'note="generated candidates, not tested compounds">')
            for m in gen["molecules"][:10]:
                out.append(f'<molecule smiles="{_attr(m.get("smiles"))}" score="{_attr(m.get("score"))}"/>')
            out.append("</generated_molecules>")
        for note in data.get("notes") or []:
            out.append(f"<note>{escape(str(note))}</note>")
        out.append("</structure_intel>")
        return "\n".join(out)

    def allowed_identifiers(self, result: ToolResult) -> Dict[str, Set[str]]:
        data = _data(result)
        pdb = {str(e.get("pdb_id")).upper() for e in data.get("pdb_entries") or [] if isinstance(e, dict) and e.get("pdb_id")}
        af = data.get("alphafold") or {}
        models = {str(af["model_id"])} if isinstance(af, dict) and af.get("model_id") else set()
        models |= set(AF_MODEL_ID_RE.findall(json.dumps(data.get("docking") or {}, default=str)))
        out: Dict[str, Set[str]] = {"pdb": pdb, "alphafold": models}
        if data.get("uniprot"):
            out["uniprot"] = {str(data["uniprot"])}
        return out

    def citations(self, result: ToolResult) -> List[CitationRecord]:
        data = _data(result)
        out: List[CitationRecord] = []
        af = data.get("alphafold")
        if isinstance(af, dict) and af.get("model_id") and af.get("url"):
            out.append(
                CitationRecord(
                    type="url",
                    id=str(af["model_id"]),
                    url=str(af["url"]),
                    title=f"AlphaFold DB model {af['model_id']} ({af.get('description') or data.get('uniprot')})",
                    evidence_level="database-record",
                    source_agent="Synapta",
                    source_tool=AF_TOOL,
                )
            )
        for e in data.get("pdb_entries") or []:
            if isinstance(e, dict) and e.get("pdb_id") and e.get("url") and not e.get("error"):
                out.append(
                    CitationRecord(
                        type="url",
                        id=str(e["pdb_id"]),
                        url=str(e["url"]),
                        title=f"PDB {e['pdb_id']}: {e.get('title') or ''}".strip(": "),
                        year=str(e.get("release_date") or "")[:4] or None,
                        evidence_level="database-record",
                        source_agent="Synapta",
                        source_tool=PDB_TOOL,
                    )
                )
        return out

    def artifact(self, result: ToolResult) -> Optional[Tuple[str, Any]]:
        data = _data(result)
        if not data:
            return None
        af = data.get("alphafold") if isinstance(data.get("alphafold"), dict) else None
        return SOURCE_KEY, {
            "uniprot": data.get("uniprot"),
            "gene": data.get("gene"),
            "alphafold": (
                {k: af.get(k) for k in ("model_id", "mean_plddt", "plddt_band", "url", "pdb_url", "version", "plddt_fractions", "pae_image_url")}
                if af
                else None
            ),
            "pdb_entries": [
                {k: e.get(k) for k in ("pdb_id", "title", "method", "resolution", "ligands", "url", "release_date")}
                for e in (data.get("pdb_entries") or [])[:50]
                if isinstance(e, dict)
            ],
            "pdb_total_count": data.get("pdb_total_count"),
            "docking": data.get("docking"),
            "generated": data.get("generated"),
            "notes": list(data.get("notes") or []),
        }

    def fallback_markdown(self, result: ToolResult) -> str:
        data = _data(result)
        lines: List[str] = []
        af = data.get("alphafold")
        if isinstance(af, dict) and af:
            lines.append(
                f"- AlphaFold DB model {af.get('model_id')} for {data.get('uniprot')}: mean pLDDT {af.get('mean_plddt')} "
                f"({af.get('plddt_band')}; a computational prediction)."
            )
        for e in (data.get("pdb_entries") or [])[:5]:
            if isinstance(e, dict) and not e.get("error"):
                res = f", {e.get('resolution')} Å" if e.get("resolution") is not None else ""
                lines.append(f"- PDB {e.get('pdb_id')}: {e.get('title')} ({e.get('method')}{res}).")
        return "\n".join(lines)

    def scope_notes(self, ctx: QueryContext, result: Optional[ToolResult]) -> List[str]:
        text = _query_text(ctx)
        if result is None:
            return []
        if not result.success:
            return [
                f"No structure evidence was retrieved ({str(result.error or '')[:160]}); do not cite PDB or AlphaFold "
                "identifiers."
            ]
        data = _data(result)
        notes = [SCOPE_PREDICTION] if data.get("alphafold") else []
        if not data.get("pdb_entries"):
            notes.append(f"RCSB PDB lists no experimental structure mapped to {data.get('uniprot')}; do not cite PDB ids.")
        if data.get("docking"):
            notes.append(SCOPE_DOCKING)
        if wants_docking(text) and not data.get("docking"):
            notes.append("No docking was run for this question; do not describe docking poses or scores.")
        return notes
