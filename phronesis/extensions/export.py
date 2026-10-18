"""
Extension module owned by EXPORT (feature 9, report export, run manifest, re-run).

Registers the `sources` artifact: compact citation records built only from successful tool outputs
(Lithoscribe passages, WebSurfer results and plugin CitationRecords), so exports never list a source that
was not in the evidence. Missing fields stay blank; nothing is defaulted.
"""

from __future__ import annotations

import logging
from itertools import zip_longest
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from phronesis.extensions import hooks

logger = logging.getLogger(__name__)

# Caps per kind, so web results and plugin sources are not crowded out by literature (and vice versa).
MAX_LITERATURE = 8
MAX_PER_PLUGIN_SOURCE = 6
MAX_WEB = 4
MAX_SOURCES = 40  # overall safety cap


class SourceRecord(BaseModel):
    kind: str  # literature | web | the CitationRecord type of a plugin source (nct, chembl, ...)
    pmid: Optional[str] = None
    doi: Optional[str] = None
    pmcid: Optional[str] = None
    id: Optional[str] = None
    id_type: Optional[str] = None
    title: str = ""
    authors: str = ""
    journal: str = ""
    year: str = ""
    section: Optional[str] = None
    has_full_text: Optional[bool] = None
    retracted: Optional[bool] = None  # None when no integrity source answered
    url: Optional[str] = None
    tool: str = ""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _title(value: Any) -> str:
    """EuropePMC titles carry escaped markup ('&lt;i&gt;EGFR&lt;/i&gt;'); exports show the plain text."""
    from phronesis.agents.citations import clean_title

    return clean_title(value) or ""


def _literature_records(result: Any) -> List[SourceRecord]:
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return []
    out: List[SourceRecord] = []
    seen = set()
    for p in data.get("ranked_passages") or []:
        if not isinstance(p, dict):
            continue
        pmid, doi = _text(p.get("pmid")) or None, _text(p.get("doi")) or None
        key = pmid or (doi or "").lower() or _text(p.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        url = (
            f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid
            else f"https://doi.org/{doi}" if doi
            else None
        )
        out.append(SourceRecord(
            kind="literature",
            pmid=pmid,
            doi=doi,
            pmcid=_text(p.get("pmcid")) or None,
            title=_title(p.get("title")),
            authors=_text(p.get("authors")),
            journal=_text(p.get("journal")),
            year=_text(p.get("year")),
            section=_text(p.get("section")) or None,
            has_full_text=p.get("has_full_text") if isinstance(p.get("has_full_text"), bool) else None,
            retracted=p.get("retracted") if isinstance(p.get("retracted"), bool) else None,
            url=url,
            tool=getattr(result, "tool_name", "") or "lithoscribe_literature_synthesis",
        ))
    return out


def _web_records(result: Any) -> List[SourceRecord]:
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return []
    out: List[SourceRecord] = []
    seen = set()
    for r in data.get("results") or []:
        url = _text((r or {}).get("url")) if isinstance(r, dict) else ""
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        out.append(SourceRecord(kind="web", title=_title(r.get("title")), url=url, tool=getattr(result, "tool_name", "") or "websurfer_search"))
    return out


def _plugin_records(tool_results: Dict[str, Any]) -> List[SourceRecord]:
    from phronesis.sources import get_sources

    per_source: List[List[SourceRecord]] = []
    for src in get_sources():
        res = tool_results.get(src.key)
        if res is None or not getattr(res, "success", False):
            continue
        try:
            records = src.citations(res) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Source %s citations failed: %s", src.key, exc)
            continue
        out: List[SourceRecord] = []
        per_source.append(out)
        for c in records[: MAX_PER_PLUGIN_SOURCE * 2]:  # deduplicated below; the per-source cap applies after
            ctype, cid = _text(getattr(c, "type", "")).lower(), _text(getattr(c, "id", ""))
            if not ctype or not cid:
                continue
            is_paper = ctype in ("pmid", "doi")
            retracted = getattr(c, "retracted", None)
            out.append(SourceRecord(
                kind="literature" if is_paper else ctype,
                pmid=cid if ctype == "pmid" else None,
                doi=cid if ctype == "doi" else None,
                id=None if is_paper else cid,
                id_type=None if is_paper else ctype,
                retracted=retracted if isinstance(retracted, bool) else None,
                title=_title(getattr(c, "title", None)),
                authors=_text(getattr(c, "authors", None)),
                journal=_text(getattr(c, "journal", None)),
                year=_text(getattr(c, "year", None)),
                section=_text(getattr(c, "section", None)) or None,
                url=_text(getattr(c, "url", None)) or None,
                tool=_text(getattr(c, "source_tool", None)) or src.key,
            ))
    # Interleaved, and capped per source, so a ten-trial registry list cannot push the drug label or variant
    # records out of the export.
    capped = [group[:MAX_PER_PLUGIN_SOURCE] for group in per_source]
    return [r for group in zip_longest(*capped) for r in group if r is not None]


def build_source_records(tool_results: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Literature first (up to 8), then plugin citations (up to 6 per source), then web results (up to 4)."""
    records: List[SourceRecord] = []
    lit = tool_results.get("lithoscribe")
    if lit is not None and getattr(lit, "success", False):
        records.extend(_literature_records(lit)[:MAX_LITERATURE])
    try:
        records.extend(_plugin_records(tool_results))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Plugin citation records failed: %s", exc)
    web = tool_results.get("websurfer")
    if web is not None and getattr(web, "success", False):
        records.extend(_web_records(web)[:MAX_WEB])

    from phronesis.export.report_model import source_key

    by_key: Dict[str, SourceRecord] = {}
    unique: List[SourceRecord] = []
    for r in records:
        key = source_key(r.model_dump())
        if not key:
            continue
        prior = by_key.get(key)
        if prior is not None:
            # The same work from Lithoscribe and a plugin source: listed once, retracted if either says so.
            if r.retracted is True:
                prior.retracted = True
            continue
        by_key[key] = r
        unique.append(r)
    return [r.model_dump() for r in unique[:MAX_SOURCES]]


def _sources_artifact(tool_results: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    records = build_source_records(tool_results)
    return {"sources": records} if records else {}


hooks.register_artifact_builder("export_sources", _sources_artifact)


def register_tools(registry: Any) -> None:
    return None
