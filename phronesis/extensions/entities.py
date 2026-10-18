"""Extension module owned by ENTITIES (feature 3, entity normalization + query planner). Registers that feature's hooks, sources and tools."""

from __future__ import annotations

from html import escape
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from phronesis.extensions import hooks

RESOLVER_TOOL = "synapta_resolve_entities"
# Direct calls (agents, tools) get a deadline that survives a loaded network; the query planner passes its own
# tighter deadline_s because it falls back to the offline lexicon when resolution is slow.
DIRECT_DEADLINE_S = 10.0
TOOL_TIMEOUT_S = 12.0
_OK_STATUSES = ("ok", "partial")


class EntityResolveInput(BaseModel):
    query: str = Field(..., description="The user's question, used to anchor mentions")
    terms: List[str] = Field(default_factory=list, max_length=120, description="1-4-gram candidate mentions")
    gene_tokens: List[str] = Field(default_factory=list, max_length=6, description="Gene-shaped tokens for HGNC")
    drug_codes: List[str] = Field(default_factory=list, max_length=4, description="Research-code drug tokens")
    rsids: List[str] = Field(default_factory=list, max_length=5, description="dbSNP rsIDs")
    transcripts_hgvs: List[str] = Field(default_factory=list, max_length=3, description="Transcript-anchored HGVS strings")
    deadline_s: Optional[float] = Field(
        default=None, ge=0.5, le=TOOL_TIMEOUT_S - 1.0,
        description=f"Overall resolution deadline in seconds (default {DIRECT_DEADLINE_S:g})",
    )


def _attr(value: Any) -> str:
    return escape(str(value), quote=True)


_ENTITY_ATTRS = (
    ("hgnc_id", "hgnc_id"),
    ("ensembl_id", "ensembl_id"),
    ("chembl_id", "chembl_id"),
    ("ontology_id", "ontology_id"),
    ("rsid", "rsid"),
)


def render_query_entities(ctx: Any) -> str:
    """<query_entities> block for the evidence: how the question's names were resolved (not biological evidence)."""
    entities = list(getattr(ctx, "entities", None) or [])
    if not entities:
        return ""
    normalization = getattr(ctx, "normalization", "")
    # Say so when the names did not all go through the live resolver.
    extra = f' normalization="{_attr(normalization)}"' if normalization in ("offline_lexicon", "partial") else ""
    lines = [f'<query_entities source="HGNC / OpenTargets / ChEMBL / dbSNP"{extra}>']
    for e in entities:
        attrs = [f'type="{_attr(e.type)}"', f'mention="{_attr(e.mention)}"', f'label="{_attr(e.label)}"']
        for key, name in _ENTITY_ATTRS:
            value = (e.ids or {}).get(key)
            if value:
                attrs.append(f'{name}="{_attr(value)}"')
        if e.gene:
            attrs.append(f'gene="{_attr(e.gene)}"')
        if e.ambiguous_with:
            attrs.append(f'ambiguous_with="{_attr(", ".join(e.ambiguous_with))}"')
        attrs.append(f'validated="{"true" if e.validated else "false"}"')
        lines.append(f"<entity {' '.join(attrs)}/>")
    lines.append("</query_entities>")
    return "\n".join(lines)


ENTITY_RULE = (
    "<query_entities> records how the names in the question were resolved. Use the approved symbol or name given "
    "as label and mention the user's alias once, for example 'PTPN11 (SHP2)'. Resolution is not biological evidence: "
    "never cite it as support for a claim, and treat entries with validated=\"false\" as unverified names."
)

hooks.register_evidence_decorator("query_entities", "entities", render_query_entities)
hooks.register_prompt_rule("entities", ENTITY_RULE)


def _budgeted_resolver(deadline_s: float) -> Any:
    """The shared resolver's transport and cache with per-source and HTTP timeouts scaled to deadline_s."""
    import httpx

    from phronesis.utils.biocontext.entity_resolver import DEFAULT_TIMEOUTS, EntityResolver, get_entity_resolver

    base = get_entity_resolver()
    per_source = 0.8 * deadline_s

    class _Budgeted(EntityResolver):
        def _client(self) -> httpx.AsyncClient:
            client = super()._client()
            client.timeout = httpx.Timeout(max(4.0, per_source), connect=3.0)
            return client

    return _Budgeted(
        transport=getattr(base, "_transport", None),
        cache=getattr(base, "_cache", None),
        timeouts={name: max(t, per_source) for name, t in {**DEFAULT_TIMEOUTS, **getattr(base, "_timeouts", {})}.items()},
        deadline_s=deadline_s,
        hgnc_concurrency=getattr(base, "_hgnc_concurrency", 8),
    )


async def _resolve(deadline_s: Optional[float] = None, **kwargs: Any) -> Any:
    """Resolve the mentions; every upstream failing (timeouts, errors) is a failed call, not an empty success."""
    from phronesis.tools.registry import ErrorType, ToolResult
    from phronesis.utils.biocontext.entity_resolver import get_entity_resolver

    resolver = get_entity_resolver()
    wanted = DIRECT_DEADLINE_S if deadline_s is None else float(deadline_s)
    if abs(wanted - getattr(resolver, "_deadline_s", wanted)) > 1e-6:
        resolver = _budgeted_resolver(wanted)
    data = await resolver.resolve(**kwargs)
    status = dict((data or {}).get("source_status") or {})
    if status and not any(v in _OK_STATUSES for v in status.values()) and not (data or {}).get("entities"):
        all_timeouts = all(v == "timeout" for v in status.values())
        detail = ", ".join(f"{k}: {v}" for k, v in sorted(status.items()))
        return ToolResult(
            success=False,
            tool_name=RESOLVER_TOOL,
            data=data,
            error=f"Entity resolution failed: no upstream answered within {wanted:g}s ({detail})",
            error_type=ErrorType.TIMEOUT if all_timeouts else ErrorType.UPSTREAM_API_ERROR,
            metadata={"source_status": status},
        )
    return data


def register_tools(registry: Any) -> None:
    from phronesis.tools.registry import AdapterType, ToolUniverseEntry

    registry.register(
        ToolUniverseEntry(
            name=RESOLVER_TOOL,
            description=(
                "Normalize gene, drug, disease and variant mentions of a question: HGNC symbols and aliases, "
                "OpenTargets mapIds, ChEMBL synonyms for research codes, dbSNP rsIDs and MANE transcripts."
            ),
            domain="synapta",
            adapter_type=AdapterType.DIRECT_REST,
            input_schema=EntityResolveInput,
            canonical_id_types=["hgnc_id", "ensembl_id", "uniprot_id", "chembl_id", "efo_id"],
            timeout_seconds=TOOL_TIMEOUT_S,
            tags=["normalization", "hgnc", "opentargets", "chembl", "dbsnp"],
        ),
        executor=_resolve,
    )
