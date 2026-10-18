"""
DepMap CRISPR gene-effect statistics for one gene from a local, user-provided release file.

Reference: Tsherniak A. et al., "Defining a Cancer Dependency Map", Cell 2017;170(3):564-576.e16,
doi:10.1016/j.cell.2017.06.010 (see also Arafeh R. et al., Nat. Rev. Cancer 2025, doi:10.1038/s41568-024-00763-x).

The DepMap portal API answers scripted clients with a bot-verification page, so there is no live connector. Put a
release's CRISPRGeneEffect.csv (Chronos gene effect: rows are models "ACH-000001", columns "SYMBOL (EntrezID)";
download it from the portal or the release's Figshare record) in PHRONESIS_DEPMAP_DIR. A Model.csv next to it
(ModelID, CellLineName, OncotreeLineage) adds cell-line names and a per-lineage summary. PHRONESIS_DEPMAP_RELEASE
names the release (e.g. "24Q4") in citations. Without the file the source is skipped with a scope note.

Statistics are computed here from the file: mean, median, SD, range, and the fraction of models with gene effect
below the threshold (default -0.5, the cut-off commonly used for a dependency on the Chronos scale, where 0 is a
non-essential gene and -1 the median of common essential genes).
"""

from __future__ import annotations

import csv
import os
import re
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from ..agents.query_context import QueryContext, SourceDecision
from . import common
from phronesis.utils.http_limits import TTLCache

TOOL_NAME = "depmap_gene_effect"
SOURCE_KEY = "depmap"
FILE_NAME = "CRISPRGeneEffect.csv"
MODEL_FILE = "Model.csv"

INTENT_RE = re.compile(
    # "synthetic lethal", "synthetic-lethal partners", "synthetically lethal", "SL partners", "essentiality".
    r"\b(depmap|dependenc(?:y|ies)|dependent\s+(?:cell\s+lines?|models?)|essential(?:ity)?|essential\s+genes?|"
    r"crispr\s+(?:screens?|knock[\s-]?outs?|ko)|gene\s+effects?|chronos|synthetic(?:al)?(?:ly)?[\s-]+lethal\w*|"
    r"SL\s+(?:partners?|interactions?|pairs?|targets?)|"
    r"fitness\s+genes?|cell\s+line\s+(?:viability|fitness))\b",
    re.I,
)
SYNTHETIC_LETHAL_RE = re.compile(
    r"\b(?:synthetic(?:al)?(?:ly)?[\s-]+lethal\w*|SL\s+(?:partners?|interactions?|pairs?|targets?))\b", re.I
)
NAME_RE = re.compile(r"\bdepmap\b", re.I)
_COL_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9\-\.]*)\s*\((\d+)\)\s*$")


class DepmapGeneEffectInput(BaseModel):
    gene: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9\-\.]{0,19}$", description="HGNC symbol")
    threshold: float = Field(default=-0.5, ge=-3.0, le=0.0, description="gene effect below this counts as a dependency")


def depmap_dir() -> Optional[Path]:
    raw = os.getenv("PHRONESIS_DEPMAP_DIR", "").strip()
    return Path(raw) if raw else None


def gene_effect_file() -> Optional[Path]:
    d = depmap_dir()
    if d is None:
        return None
    path = d / FILE_NAME
    return path if path.is_file() else None


_HEADER_CACHE = TTLCache(maxsize=2, ttl_s=24 * 3600.0)  # (path, mtime, size) -> SYMBOL -> (column, Entrez)
_RESULT_CACHE = TTLCache(maxsize=256, ttl_s=24 * 3600.0)  # (path, mtime, size, symbol, threshold) -> stats


def _header_map(path: Path) -> Dict[str, Tuple[int, str]]:
    """SYMBOL -> (column index, Entrez id) from the file header (cached per file version)."""
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size)
    hit = _HEADER_CACHE.get(key)
    if hit is not None:
        return hit
    with open(path, newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    out: Dict[str, Tuple[int, str]] = {}
    for i, name in enumerate(header[1:], start=1):
        m = _COL_RE.match(name)
        if m:
            out.setdefault(m.group(1).upper(), (i, m.group(2)))
    _HEADER_CACHE.set(key, out)
    return out


def _models(directory: Path) -> Dict[str, Dict[str, str]]:
    path = directory / MODEL_FILE
    if not path.is_file():
        return {}
    out: Dict[str, Dict[str, str]] = {}
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                mid = (row.get("ModelID") or "").strip()
                if mid:
                    out[mid] = {
                        "name": (row.get("CellLineName") or row.get("StrippedCellLineName") or "").strip(),
                        "lineage": (row.get("OncotreeLineage") or "").strip(),
                    }
    except (OSError, csv.Error):
        return {}
    return out


def _r(x: float) -> float:
    return round(float(x), 3)


def compute_gene_effect(path: Path, gene: str, threshold: float = -0.5) -> Dict[str, Any]:
    """Statistics of one gene's column; raises KeyError when the gene is not in the file."""
    symbol = gene.upper()
    col = _header_map(path).get(symbol)
    if col is None:
        raise KeyError(symbol)
    idx, entrez = col
    values: List[Tuple[str, float]] = []
    with open(path, encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            parts = line.rstrip("\r\n").split(",", idx + 1)
            if len(parts) <= idx:
                continue
            raw = parts[idx].strip()
            if not raw or raw.upper() in ("NA", "NAN"):
                continue
            try:
                values.append((parts[0].strip().strip("\""), float(raw)))
            except ValueError:
                continue
    if not values:
        raise KeyError(symbol)
    effects = [v for _, v in values]
    dependent = [v for v in effects if v < threshold]
    models = _models(path.parent)
    most = sorted(values, key=lambda mv: mv[1])[:5]
    lineages: Dict[str, List[float]] = {}
    for mid, v in values:
        lin = (models.get(mid) or {}).get("lineage")
        if lin:
            lineages.setdefault(lin, []).append(v)
    lineage_rows = sorted(
        ({"lineage": k, "n": len(v), "mean_effect": _r(statistics.fmean(v)),
          "fraction_dependent": _r(sum(1 for x in v if x < threshold) / len(v))} for k, v in lineages.items() if len(v) >= 3),
        key=lambda r: r["mean_effect"],
    )[:5]
    return {
        "gene": symbol,
        "entrez_id": entrez,
        "n_models": len(effects),
        "mean_effect": _r(statistics.fmean(effects)),
        "median_effect": _r(statistics.median(effects)),
        "sd_effect": _r(statistics.pstdev(effects)) if len(effects) > 1 else 0.0,
        "min_effect": _r(min(effects)),
        "max_effect": _r(max(effects)),
        "threshold": threshold,
        "n_dependent": len(dependent),
        "fraction_dependent": _r(len(dependent) / len(effects)),
        "most_dependent": [
            {"model_id": mid, "gene_effect": _r(v), **({"cell_line": models[mid]["name"]} if models.get(mid, {}).get("name") else {}),
             **({"lineage": models[mid]["lineage"]} if models.get(mid, {}).get("lineage") else {})}
            for mid, v in most
        ],
        "lineages_most_dependent": lineage_rows,
    }


def depmap_gene_effect(gene: str, threshold: float = -0.5) -> Any:
    """Gene-effect statistics across the cell lines of the local DepMap release (sync; runs in a worker)."""
    from ..tools.registry import ErrorType, ToolResult

    symbol = gene.strip().upper()
    path = gene_effect_file()
    if path is None:
        return ToolResult(
            success=False, tool_name=TOOL_NAME, error_type=ErrorType.DATASET_REQUIRED,
            error=f"No DepMap {FILE_NAME} configured (set PHRONESIS_DEPMAP_DIR)",
            data={"status": "skipped", "subject": symbol},
        )
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size, symbol, float(threshold))
    cached = _RESULT_CACHE.get(key)
    try:
        stats = dict(cached) if cached is not None else compute_gene_effect(path, symbol, threshold)
    except KeyError:
        return ToolResult(
            success=False, tool_name=TOOL_NAME, error_type=ErrorType.NOT_FOUND,
            error=f"{symbol} is not a column of the local DepMap {FILE_NAME}", data={"status": "not_found", "subject": symbol},
        )
    except (OSError, UnicodeDecodeError, csv.Error, StopIteration) as exc:
        return ToolResult(
            success=False, tool_name=TOOL_NAME, error_type=ErrorType.INTERNAL_ERROR,
            error=f"DepMap file could not be read: {type(exc).__name__}", data={"status": "error", "subject": symbol},
        )
    _RESULT_CACHE.set(key, dict(stats))
    release = os.getenv("PHRONESIS_DEPMAP_RELEASE", "").strip() or path.parent.name
    data = {
        "subject": symbol,
        **stats,
        "release": release,
        "file": FILE_NAME,
        "items": stats["most_dependent"],
        "citation_records": [
            {"type": "url", "id": f"DepMap:{symbol}", "title": f"DepMap CRISPR gene effect (Chronos) for {symbol}, release {release}",
             "url": f"https://depmap.org/portal/gene/{symbol}", "section": "computed from the local CRISPRGeneEffect.csv"}
        ],
    }
    return ToolResult(success=True, tool_name=TOOL_NAME, data=data, metadata={"source_release": f"DepMap {release}"})


def tool_entry() -> Any:
    from ..tools.registry import AdapterType, ToolUniverseEntry

    return ToolUniverseEntry(
        name=TOOL_NAME,
        description=(
            "DepMap CRISPR (Chronos) gene effect of one gene across cancer cell lines from the local release file "
            "(PHRONESIS_DEPMAP_DIR): mean, median and range of gene effect, fraction of dependent lines (effect < -0.5) "
            "and the most dependent lines and lineages."
        ),
        domain=common.DOMAIN,
        adapter_type=AdapterType.LOCAL_COMPUTE,
        input_schema=DepmapGeneEffectInput,
        canonical_id_types=["entrez_id"],
        timeout_seconds=120.0,
        tags=["depmap", "dependency", "essentiality", "crispr", "gene_effect", "chronos", "cell_line", "cancer"],
        is_async=False,
    )


class DepmapSource(common.OmicsSource):
    key = SOURCE_KEY
    action = "DepMap gene-effect statistics (local release file)"
    tool_name = TOOL_NAME
    artifact_name = "depmap"
    label = "DepMap"
    nothing_found = "gene-effect values"
    name_re = NAME_RE
    order = 65

    def intent(self, ctx: QueryContext) -> bool:
        return bool(INTENT_RE.search(common.query_text(ctx)))

    def build_plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        gene = common.gene_symbol(ctx)
        return {"gene": gene, "_subject": gene} if gene else None

    def should_run(self, ctx: QueryContext) -> SourceDecision:
        decision = super().should_run(ctx)
        if decision.run and gene_effect_file() is None:
            return SourceDecision(run=False, reason=f"no DepMap {FILE_NAME} configured (PHRONESIS_DEPMAP_DIR)")
        return decision

    def describe(self, result: Any, ctx: QueryContext):
        if not result.success:
            return super().describe(result, ctx)
        d = common.data_of(result)
        return (
            f"DepMap: {d.get('gene')} gene effect across {d.get('n_models')} cell lines",
            f"mean {d.get('mean_effect')}, {d.get('n_dependent')} dependent (effect < {d.get('threshold')})",
        )

    def compact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        keys = ("gene", "entrez_id", "release", "n_models", "mean_effect", "median_effect", "sd_effect", "min_effect",
                "max_effect", "threshold", "n_dependent", "fraction_dependent", "most_dependent", "lineages_most_dependent")
        return {k: data.get(k) for k in keys}

    def evidence_xml(self, result: Any) -> str:
        d = common.data_of(result)
        head = {k: d.get(k) for k in ("gene", "entrez_id", "release", "n_models", "mean_effect", "median_effect", "sd_effect",
                                       "min_effect", "max_effect", "threshold", "n_dependent", "fraction_dependent")}
        lines = [
            '<depmap_gene_effect source="DepMap CRISPRGeneEffect.csv (local release file, Chronos scores)" '
            + " ".join(f'{k}="{common.attr(v)}"' for k, v in head.items() if v is not None)
            + ' note="Statistics computed from the file: 0 = no effect, -1 = median of common essential genes; '
            'lines with effect below the threshold count as dependent. Quote them exactly with DepMap and the release; '
            'cancer cell lines in vitro, not patients.">'
        ]
        for m in d.get("most_dependent") or []:
            lines.append("<model " + " ".join(f'{k}="{common.attr(v)}"' for k, v in m.items()) + "/>")
        for r in d.get("lineages_most_dependent") or []:
            lines.append("<lineage " + " ".join(f'{k}="{common.attr(v)}"' for k, v in r.items()) + "/>")
        lines.append("</depmap_gene_effect>")
        return "\n".join(lines)

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        return {"depmap_model": {str(m["model_id"]) for m in common.data_of(result).get("most_dependent") or [] if m.get("model_id")}}

    def scope_notes(self, ctx: QueryContext, result: Optional[Any]) -> List[str]:
        if result is None:
            if (self.intent(ctx) or self.named(ctx)) and common.gene_symbol(ctx) and gene_effect_file() is None and not common.simulation_only(ctx):
                notes = [
                    "DepMap gene-dependency data was not consulted: no local DepMap release file is configured. Do not "
                    "state DepMap essentiality or gene-effect values."
                ]
                if SYNTHETIC_LETHAL_RE.search(common.query_text(ctx)):
                    # SynLethDB (a separate local source) may still supply curated pairs; only DepMap is missing here.
                    notes.append(
                        "No DepMap dependency-screen data was consulted for this question. Name a synthetic-lethal "
                        "partner only when a retrieved source (for example SynLethDB or a cited paper) reports it, cite "
                        "that source, and never infer partners from pathway knowledge alone."
                    )
                return notes
            return []
        return super().scope_notes(ctx, result)
