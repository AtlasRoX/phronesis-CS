"""
Shared plumbing for the omics data sources: one httpx client factory (tests inject a MockTransport through
TRANSPORT), typed upstream errors mapped onto ToolResult error types, XML escaping, entity helpers for the
query context, and OmicsSource, the EvidenceSource base every source in this package extends.

An outage is reported as an outage: a failed lookup becomes a scope note saying the database could not be
queried, never an empty result that the answer could read as evidence of absence.
"""

from __future__ import annotations

import re
from html import escape
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import httpx

from ..agents.query_context import QueryContext, SourceDecision
from ..sources.base import CitationRecord, EvidenceSource, SourceRunState
from ..utils.http import BELOW_400, HttpError, get_json as http_get_json

ARTIFACT_KEY = "omics_intel"
DOMAIN = "omics"
USER_AGENT = "Phronesis-Omics/1.0 (research assistant; omics evidence sources)"

# Tests replace this with httpx.MockTransport(handler); None = the real network.
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None

_GENE_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,14}$")
_UNIPROT_RE = re.compile(r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})$")
_RSID_RE = re.compile(r"\brs\d{3,12}\b", re.IGNORECASE)


class UpstreamError(Exception):
    """kind: timeout | not_found | bad_request | upstream. `detail` is short and safe to show to the user."""

    def __init__(self, detail: str, kind: str = "upstream", status: Optional[int] = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.kind = kind
        self.status = status


def client(timeout_s: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s, connect=min(5.0, timeout_s)),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        transport=TRANSPORT,
        follow_redirects=True,
    )


async def get_json(
    http: httpx.AsyncClient,
    url: str,
    *,
    source: str,
    params: Optional[Dict[str, Any]] = None,
    not_found: Sequence[int] = (404,),
    limiter: Any = None,
) -> Any:
    """GET JSON once (no retry); every failure raises UpstreamError. limiter (an async context manager) spaces calls."""
    accept = frozenset(BELOW_400).difference(not_found)
    try:
        return await http_get_json(http, url, params=params, retries=0, accept=accept, limiter=limiter)
    except HttpError as exc:
        code = exc.status
        if exc.kind == "timeout":
            raise UpstreamError(f"{source} timed out", "timeout") from exc.cause
        if exc.kind == "transport":
            raise UpstreamError(f"{source} unreachable ({type(exc.cause).__name__})") from exc.cause
        if exc.kind == "bad_body":
            raise UpstreamError(f"{source} returned a non-JSON response (HTTP {code})", "upstream", code) from exc.cause
        assert code is not None
        if code in not_found:
            raise UpstreamError(f"{source} has no record (HTTP {code})", "not_found", code) from None
        if code == 429:
            raise UpstreamError(f"{source} rate limited the request (HTTP 429)", "upstream", code) from None
        if code >= 500:
            raise UpstreamError(f"{source} is unavailable (HTTP {code})", "upstream", code) from None
        raise UpstreamError(f"{source} rejected the request (HTTP {code})", "bad_request", code) from None


def failure(tool_name: str, exc: Exception, subject: str = "") -> Any:
    from ..tools.registry import ErrorType, ToolResult

    if isinstance(exc, UpstreamError):
        kind, detail = exc.kind, exc.detail
    elif isinstance(exc, httpx.TimeoutException):
        kind, detail = "timeout", "request timed out"
    elif isinstance(exc, httpx.TransportError):
        kind, detail = "upstream", f"unreachable ({type(exc).__name__})"
    else:
        kind, detail = "upstream", f"{type(exc).__name__}: {str(exc)[:160]}"
    error_type = {
        "timeout": ErrorType.TIMEOUT,
        "not_found": ErrorType.NOT_FOUND,
        "bad_request": ErrorType.INVALID_IDENTIFIER,
    }.get(kind, ErrorType.UPSTREAM_API_ERROR)
    return ToolResult(
        success=False,
        tool_name=tool_name,
        data={"status": "not_found" if kind == "not_found" else "error", "detail": detail, "subject": subject},
        error=detail,
        error_type=error_type,
    )


def ok(
    tool_name: str,
    data: Dict[str, Any],
    canonical_ids: Optional[Dict[str, str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Any:
    from ..tools.registry import ToolResult

    return ToolResult(
        success=True,
        tool_name=tool_name,
        data=data,
        canonical_ids={k: v for k, v in (canonical_ids or {}).items() if v},
        metadata={k: v for k, v in (metadata or {}).items() if v is not None},
    )


# -----------------------------------------------------------------------------
# Database release probes (DAT-010)
# -----------------------------------------------------------------------------
# The release of a source's database, read from a cheap version endpoint next to the main request. Best effort:
# a probe never fails or holds up the tool (short timeout, any error -> None), and its answer is cached so one
# release lookup serves many calls. The retrieval time itself is stamped on every ToolResult by the registry.
RELEASE_TTL_S = 6 * 3600.0
RELEASE_FAILURE_TTL_S = 600.0
RELEASE_TIMEOUT_S = 3.0
_RELEASES: Dict[str, Tuple[float, Optional[str]]] = {}


async def release_version(http: httpx.AsyncClient, url: str, *, source: str) -> Optional[str]:
    """The release string a version endpoint returns (a bare number or string, or {"version"|"release": ...}),
    or None when it could not be read. Cached RELEASE_TTL_S after a success, RELEASE_FAILURE_TTL_S after a failure."""
    import asyncio
    import time

    now = time.monotonic()
    hit = _RELEASES.get(url)
    if hit is not None and hit[0] > now:
        return hit[1]
    version: Optional[str] = None
    try:
        doc = await asyncio.wait_for(get_json(http, url, source=source), timeout=RELEASE_TIMEOUT_S)
        if isinstance(doc, dict):
            doc = doc.get("version") or doc.get("release") or doc.get("dataRelease")
        if isinstance(doc, (int, float, str)) and not isinstance(doc, bool) and str(doc).strip():
            version = str(doc).strip()[:40]
    except Exception:  # noqa: BLE001 - provenance enrichment must never fail the tool
        version = None
    _RELEASES[url] = (now + (RELEASE_TTL_S if version else RELEASE_FAILURE_TTL_S), version)
    return version


def clear_release_cache() -> None:
    _RELEASES.clear()


def attr(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def text(value: Any) -> str:
    return escape("" if value is None else str(value), quote=False)


def data_of(result: Any) -> Dict[str, Any]:
    data = getattr(result, "data", None)
    return data if isinstance(data, dict) else {}


# -----------------------------------------------------------------------------
# Query context helpers
# -----------------------------------------------------------------------------

def query_text(ctx: QueryContext) -> str:
    return ctx.clean_query or ctx.raw_query or ""


def gene_symbol(ctx: QueryContext) -> Optional[str]:
    """The approved symbol of the primary gene (no variant suffix), or None."""
    symbol = (ctx.primary_symbol or "").strip().upper()
    if not symbol and ctx.genes:
        symbol = (ctx.genes[0].label or "").strip().upper()
    return symbol if symbol and _GENE_RE.match(symbol) else None


def uniprot_accession(ctx: QueryContext) -> Optional[str]:
    gene = ctx.primary_gene or (ctx.genes[0] if ctx.genes else None)
    acc = str((gene.ids if gene is not None else {}).get("uniprot_id") or "").strip().upper()
    return acc if acc and _UNIPROT_RE.match(acc) else None


def rsids(ctx: QueryContext) -> List[str]:
    out = [str(v.ids.get("rsid")).lower() for v in ctx.variants if v.ids.get("rsid")]
    out.extend(m.lower() for m in _RSID_RE.findall(query_text(ctx)))
    return list(dict.fromkeys(r for r in out if _RSID_RE.fullmatch(r)))


def disease_trait(ctx: QueryContext) -> Tuple[Optional[str], Optional[str]]:
    """(ontology id with underscore, e.g. MONDO_0005148, label) of the first disease, either may be None."""
    for d in ctx.diseases:
        oid = str(d.ids.get("ontology_id") or "").replace(":", "_").strip()
        oid = oid if re.fullmatch(r"(?:EFO|MONDO|Orphanet|HP|DOID)_\d{4,9}", oid) else ""
        label = (d.label or d.mention or "").strip()
        if oid or label:
            return oid or None, label or None
    return None, None


def simulation_only(ctx: QueryContext) -> bool:
    """A BioModel simulation question: omics lookups stay out unless their database is named."""
    return bool(ctx.biomodel_id and ctx.is_simulation_intent)


# -----------------------------------------------------------------------------
# EvidenceSource base
# -----------------------------------------------------------------------------

class OmicsSource(EvidenceSource):
    """One omics database: subclasses set the names below and implement intent(), plan(), compact() and xml."""

    agent = "Synapta"
    tool_name = ""
    artifact_name = ""  # sub-key of the omics_intel artifact
    label = ""  # database name for steps and scope notes, e.g. "GWAS Catalog"
    subject_kind = "gene"  # what plan() needs, for the routing reason
    nothing_found = "records"  # what an empty result has none of
    name_re: Optional[re.Pattern] = None  # the database named explicitly ("GTEx", "STRING")
    stage = 1

    @property
    def required_tools(self) -> Tuple[str, ...]:  # type: ignore[override]
        return (self.tool_name,)

    # -- routing ---------------------------------------------------------------------------------------------
    def named(self, ctx: QueryContext) -> bool:
        return bool(self.name_re and self.name_re.search(query_text(ctx)))

    def intent(self, ctx: QueryContext) -> bool:
        raise NotImplementedError

    def build_plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        """Tool kwargs for this inquiry (with a 'subject' label kept on ctx.extras), computed once."""
        key = f"omics.{self.key}.plan"
        extras = ctx.extras if isinstance(ctx.extras, dict) else {}
        if key not in extras:
            extras[key] = self.build_plan(ctx) or {}
        return dict(extras[key]) or None

    def subject(self, ctx: QueryContext) -> str:
        plan = self.plan(ctx) or {}
        return str(plan.get("_subject") or "")

    def tool_kwargs(self, ctx: QueryContext) -> Dict[str, Any]:
        return {k: v for k, v in (self.plan(ctx) or {}).items() if not k.startswith("_")}

    def should_run(self, ctx: QueryContext) -> SourceDecision:
        if simulation_only(ctx) and not self.named(ctx):
            return SourceDecision(run=False, reason="BioModel simulation requested")
        if not (self.intent(ctx) or self.named(ctx)):
            return SourceDecision(run=False, reason=f"no {self.label} question")
        plan = self.plan(ctx)
        if not plan:
            return SourceDecision(run=False, reason=f"no {self.subject_kind} named")
        return ctx.gate_decision(self.key, SourceDecision(run=True, reason=f"{self.label} lookup for {plan.get('_subject')}"))

    def running_title(self, ctx: QueryContext) -> str:
        return f"Querying {self.label} for {self.subject(ctx) or 'the question'}"

    async def run(self, ctx: QueryContext, registry: Any, state: SourceRunState) -> Any:
        kwargs = self.tool_kwargs(ctx)
        result = await registry.execute(self.tool_name, **kwargs)
        return await self._disease_only_fallback(ctx, registry, kwargs, result)

    async def _disease_only_fallback(self, ctx: QueryContext, registry: Any, kwargs: Dict[str, Any], result: Any) -> Any:
        """A gene + disease query that genuinely found nothing (MYC + glioblastoma in the GWAS Catalog) is asked
        once more for the disease alone; the answer is labelled as disease-level, never as the gene's."""
        gene, trait_id = kwargs.get("gene"), kwargs.get("efo_id") or kwargs.get("efo_trait")
        if not (gene and trait_id and getattr(result, "success", False) and not self.items(data_of(result))):
            return result
        broad = await registry.execute(self.tool_name, **{k: v for k, v in kwargs.items() if k != "gene"})
        if not (getattr(broad, "success", False) and self.items(data_of(broad))):
            return result
        trait = disease_trait(ctx)[1] or str(trait_id)
        note = f"no {gene}-specific {self.nothing_found} for {trait}; top {self.nothing_found} for {trait} shown"
        broad.data = {**data_of(broad), "subject": f"{trait} ({note})",
                      "fallback": {"kind": "disease_only", "dropped_gene": gene, "trait": trait, "note": note}}
        return broad

    def plan_fact(self, ctx: QueryContext) -> Optional[str]:
        subject = self.subject(ctx)
        return f"{self.label}: {subject}" if subject else None

    # -- results ---------------------------------------------------------------------------------------------
    def items(self, data: Dict[str, Any]) -> List[Any]:
        items = data.get("items")
        return items if isinstance(items, list) else []

    def describe(self, result: Any, ctx: QueryContext) -> Tuple[str, str]:
        subject = self.subject(ctx) or data_of(result).get("subject") or ""
        if not result.success:
            err_type = getattr(result.error_type, "value", result.error_type) or "ERROR"
            return f"{self.label} lookup for {subject} failed", f"{err_type}: {str(result.error or '')[:240]}"
        n = len(self.items(data_of(result)))
        fallback = data_of(result).get("fallback") or {}
        if fallback.get("note"):
            return f"{self.label}: {fallback['note']}", ""
        return f"{self.label}: {n} {self.nothing_found if n != 1 else self.nothing_found.rstrip('s')} for {subject}", ""

    def compact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in data.items() if k not in ("citation_records",)}

    def artifact(self, result: Any) -> Optional[Tuple[str, Any]]:
        data = data_of(result)
        if not data:
            return None
        return ARTIFACT_KEY, {self.artifact_name: {"status": "ok", **self.compact(data)}}

    def citations(self, result: Any) -> List[CitationRecord]:
        out: List[CitationRecord] = []
        for rec in data_of(result).get("citation_records") or []:
            try:
                out.append(CitationRecord(**{**rec, "source_agent": self.agent, "source_tool": self.tool_name}))
            except Exception:  # noqa: BLE001
                continue
        return out

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        return {}

    def scope_notes(self, ctx: QueryContext, result: Optional[Any]) -> List[str]:
        if result is None:
            return []
        subject = self.subject(ctx) or data_of(result).get("subject") or "this question"
        if not result.success:
            et = getattr(result.error_type, "value", result.error_type)
            if et == "NOT_FOUND":
                return [f"{self.label} has no entry for {subject}; do not state {self.label} {self.nothing_found} for it."]
            return [
                f"{self.label} could not be queried for {subject} ({str(result.error or et or 'error')[:160]}). This is "
                f"an outage, not evidence that no {self.nothing_found} exist; do not state {self.label} "
                f"{self.nothing_found} for it."
            ]
        if not self.items(data_of(result)):
            return [f"{self.label} returned no {self.nothing_found} for {subject}; do not cite {self.label} for it."]
        fallback = data_of(result).get("fallback") or {}
        if fallback.get("note"):
            return [f"{self.label}: {fallback['note']}. These are disease-level results; do not attribute them to "
                    f"{fallback.get('dropped_gene')}."]
        return []
