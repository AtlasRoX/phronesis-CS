"""
Recent bioRxiv and medRxiv preprints about a topic. Preprints are not peer reviewed and are labelled so.

API: https://api.biorxiv.org/details/{server}/{YYYY-MM-DD}/{YYYY-MM-DD}/{cursor}/json returns 30 records per page
({doi, title, authors, date, version, type, category, abstract, published, server}) and messages[0].total.
Both dates must be given as YYYY-MM-DD (the "30d" form answers "Both dates must be in yyyy-mm-dd format"), and
records come oldest first. There is no search endpoint, so this reads the first page to learn the total, then the
two newest pages (at most 3 requests per server), and keeps records whose title or abstract mentions a topic term.
It therefore samples the most recent ~60 preprints of the window plus its first 30: absence here is not absence
from bioRxiv. No database paper for the API was verified (docs/agentic/references-verification-tools-data.md §20).
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Set

from pydantic import BaseModel, Field, field_validator

from ..agents.query_context import QueryContext
from . import common

TOOL_NAME = "preprints_recent"
SOURCE_KEY = "preprints"
BASE_URL = "https://api.biorxiv.org/details"
PAGE = 30
MAX_PAGES = 3
LABEL = "preprint, not peer reviewed"

INTENT_RE = re.compile(
    r"\b(latest|most\s+recent|recent(?:ly)?|preprints?|biorxiv|medrxiv|newest|emerging|this\s+(?:week|month|year)|"
    r"cutting[\s-]edge|up[\s-]to[\s-]date|new\s+(?:preprints?|findings|results|research|studies|data|papers?|work|evidence|developments?))\b",
    re.I,
)
NAME_RE = re.compile(r"\b(biorxiv|medrxiv|preprints?)\b", re.I)


def _today() -> date:
    return datetime.now(timezone.utc).date()


class PreprintsInput(BaseModel):
    terms: List[str] = Field(..., min_length=1, max_length=5, description="topic terms matched in title/abstract")
    days: int = Field(default=30, ge=1, le=90, description="look-back window in days")
    servers: List[Literal["biorxiv", "medrxiv"]] = Field(default_factory=lambda: ["biorxiv", "medrxiv"], min_length=1, max_length=2)
    max_results: int = Field(default=10, ge=1, le=25)

    @field_validator("terms")
    @classmethod
    def _clean(cls, terms: List[str]) -> List[str]:
        out = [str(t).strip() for t in terms if 2 <= len(str(t).strip()) <= 80]
        if not out:
            raise ValueError("give at least one topic term of 2-80 characters")
        return out


def _version(value: Any) -> int:
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0


def _term_patterns(terms: List[str]) -> List[re.Pattern]:
    """Whole-word, case-insensitive matches; a plural is accepted ("gasdermin" matches "gasdermins", "MYC" not "MYCN")."""
    return [re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?:s|es)?(?![A-Za-z0-9])", re.I) for t in terms]


async def _server_pages(http: Any, server: str, start: str, end: str) -> Dict[str, Any]:
    base = f"{BASE_URL}/{server}/{start}/{end}"
    first = await common.get_json(http, f"{base}/0/json", source=server)
    msg = ((first or {}).get("messages") or [{}])[0]
    if str(msg.get("status", "ok")).lower() != "ok":
        raise common.UpstreamError(f"{server}: {str(msg.get('status'))[:120]}", "bad_request")
    try:
        total = int(msg.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    records = list((first or {}).get("collection") or [])
    cursors: List[int] = []
    for k in range(1, MAX_PAGES):  # the newest pages; records come oldest first
        c = max(PAGE, total - PAGE * k)
        if c < total and c not in cursors:
            cursors.append(c)
    pages = await asyncio.gather(*(common.get_json(http, f"{base}/{c}/json", source=server) for c in cursors), return_exceptions=True)
    errors = [p for p in pages if isinstance(p, Exception)]
    for p in pages:
        if isinstance(p, dict):
            records.extend(p.get("collection") or [])
    return {"server": server, "total": total, "records": records, "partial": bool(errors),
            "pages_failed": len(errors), "pages_requested": len(cursors) + 1}


async def recent_preprints(terms: List[str], days: int = 30, servers: Optional[List[str]] = None, max_results: int = 10) -> Any:
    """Preprints posted in the last `days` days whose title or abstract mentions one of the terms."""
    servers = list(servers or ["biorxiv", "medrxiv"])
    end = _today()
    start = end - timedelta(days=days)
    subject = ", ".join(terms)
    try:
        async with common.client(20.0) as http:
            results = await asyncio.gather(*(_server_pages(http, s, start.isoformat(), end.isoformat()) for s in servers), return_exceptions=True)
    except Exception as exc:  # noqa: BLE001
        return common.failure(TOOL_NAME, exc, subject)
    ok = [r for r in results if isinstance(r, dict)]
    if not ok:
        return common.failure(TOOL_NAME, next(r for r in results if isinstance(r, Exception)), subject)
    sources = []
    for s, r in zip(servers, results):
        if isinstance(r, Exception):
            sources.append({"name": s, "status": "error", "detail": getattr(r, "detail", str(r))[:160]})
        else:
            entry = {"name": s, "status": "partial" if r["partial"] else "ok", "posted_in_window": r["total"], "scanned": len(r["records"])}
            if r["partial"]:
                entry["pages_failed"] = r.get("pages_failed")
                entry["pages_requested"] = r.get("pages_requested")
            sources.append(entry)
    patterns = _term_patterns(terms)
    best: Dict[str, Dict[str, Any]] = {}
    for r in ok:
        for rec in r["records"]:
            if not isinstance(rec, dict) or not rec.get("doi"):
                continue
            blob = f"{rec.get('title') or ''}\n{rec.get('abstract') or ''}"
            matched = [t for t, p in zip(terms, patterns) if p.search(blob)]
            if not matched:
                continue
            doi = str(rec["doi"])
            prev = best.get(doi)
            if prev is not None and _version(prev.get("version")) >= _version(rec.get("version")):
                continue
            abstract = str(rec.get("abstract") or "")
            best[doi] = {
                "doi": doi,
                "title": str(rec.get("title") or ""),
                "authors": str(rec.get("authors") or "")[:240],
                "date": rec.get("date"),
                "version": rec.get("version"),
                "server": rec.get("server") or r["server"],
                "category": rec.get("category"),
                "published_doi": rec.get("published") if rec.get("published") not in (None, "", "NA") else None,
                "matched_terms": matched,
                "abstract_snippet": abstract[:400].rsplit(" ", 1)[0] + (" [...]" if len(abstract) > 400 else ""),
                "url": f"https://doi.org/{doi}",
                "label": LABEL,
            }
    items = sorted(best.values(), key=lambda x: (len(x["matched_terms"]), str(x.get("date") or "")), reverse=True)[:max_results]
    data = {
        "subject": subject,
        "terms": terms,
        "window": f"{start.isoformat()}..{end.isoformat()}",
        "servers": servers,
        "scanned": sum(len(r["records"]) for r in ok),
        "sources": sources,
        "label": LABEL,
        "note": "The bioRxiv/medRxiv API has no search; only the newest and first pages of the window were scanned.",
        "items": items,
        "citation_records": [
            {"type": "doi", "id": it["doi"], "title": it["title"], "url": it["url"], "authors": it["authors"],
             "journal": f"{it.get('server')} (preprint)", "year": str(it.get("date") or "")[:4] or None,
             "evidence_level": LABEL}
            for it in items
        ],
    }
    return common.ok(TOOL_NAME, data)


def tool_entry() -> Any:
    from ..tools.registry import AdapterType, ToolUniverseEntry

    return ToolUniverseEntry(
        name=TOOL_NAME,
        description=(
            "Recent bioRxiv and medRxiv preprints (api.biorxiv.org details) whose title or abstract mentions the "
            "topic terms, newest first; preprints are not peer reviewed."
        ),
        domain=common.DOMAIN,
        adapter_type=AdapterType.DIRECT_REST,
        input_schema=PreprintsInput,
        canonical_id_types=["doi"],
        timeout_seconds=35.0,
        tags=["preprint", "biorxiv", "medrxiv", "recent", "latest", "literature", "not_peer_reviewed", "upstream:biorxiv"],
        is_async=True,
    )


def _server_label(name: Any) -> str:
    return {"biorxiv": "bioRxiv", "medrxiv": "medRxiv"}.get(str(name or "").lower(), str(name or "A preprint server"))


class PreprintSource(common.OmicsSource):
    key = SOURCE_KEY
    agent = "Lithoscribe"
    action = "bioRxiv/medRxiv recent preprint scan"
    tool_name = TOOL_NAME
    artifact_name = "preprints"
    label = "bioRxiv/medRxiv"
    subject_kind = "gene, drug or disease"
    nothing_found = "recent preprints"
    name_re = NAME_RE
    order = 66

    def intent(self, ctx: QueryContext) -> bool:
        return bool(INTENT_RE.search(common.query_text(ctx)))

    def build_plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        terms: List[str] = []
        gene = common.gene_symbol(ctx)
        if gene:
            terms.append(gene)
        terms.extend((d.label or d.mention or "").strip() for d in ctx.drugs[:2])
        terms.extend((d.label or d.mention or "").strip() for d in ctx.diseases[:1])
        terms = [t for t in dict.fromkeys(terms) if 2 <= len(t) <= 80][:5]
        return {"terms": terms, "_subject": ", ".join(terms)} if terms else None

    def compact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {k: data.get(k) for k in ("terms", "window", "servers", "scanned", "sources", "label", "note", "items")}

    def evidence_xml(self, result: Any) -> str:
        data = common.data_of(result)
        lines = [
            f'<preprints source="bioRxiv/medRxiv API" window="{common.attr(data.get("window"))}" '
            f'terms="{common.attr(", ".join(data.get("terms") or []))}" scanned="{common.attr(data.get("scanned"))}" '
            f'peer_reviewed="false" note="Preprints are not peer reviewed: say so whenever one is cited, and do not '
            'present their findings as established.">'
        ]
        # Per-server status (DAT-014): a server that failed or returned only some pages was not fully scanned, so a
        # missing preprint there is not evidence that none was posted.
        for src in data.get("sources") or []:
            if not isinstance(src, dict):
                continue
            attrs = {k: src.get(k) for k in ("name", "status", "posted_in_window", "scanned", "pages_failed",
                                             "pages_requested", "detail")}
            lines.append("<server " + " ".join(f'{k}="{common.attr(v)}"' for k, v in attrs.items() if v not in (None, "")) + "/>")
        for it in self.items(data):
            lines.append(
                f'<preprint doi="{common.attr(it.get("doi"))}" server="{common.attr(it.get("server"))}" '
                f'date="{common.attr(it.get("date"))}" version="{common.attr(it.get("version"))}" '
                f'published_doi="{common.attr(it.get("published_doi") or "")}" status="{LABEL}" '
                f'title="{common.attr(it.get("title"))}">{common.text(it.get("abstract_snippet"))}</preprint>'
            )
        lines.append("</preprints>")
        return "\n".join(lines)

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        dois: Set[str] = set()
        for it in self.items(common.data_of(result)):
            for d in (it.get("doi"), it.get("published_doi")):
                if d:
                    dois.update({str(d), str(d).lower()})
        return {"dois": dois}

    def scope_notes(self, ctx: QueryContext, result: Optional[Any]) -> List[str]:
        notes = super().scope_notes(ctx, result)
        if result is not None and result.success:
            failed = [s["name"] for s in common.data_of(result).get("sources") or [] if s.get("status") == "error"]
            if failed:
                notes.append(f"{', '.join(failed)} could not be queried for recent preprints (an outage, not an absence of preprints).")
            for src in common.data_of(result).get("sources") or []:
                if src.get("status") == "partial":
                    notes.append(
                        f"{_server_label(src.get('name'))} returned only some pages ({src.get('pages_failed')} of "
                        f"{src.get('pages_requested')} page requests failed); recent preprints may be missing, so do not "
                        "conclude that none were posted."
                    )
            if self.items(common.data_of(result)):
                notes.append("Preprints in the evidence are not peer reviewed; label each one as a preprint when you cite it.")
        return notes
