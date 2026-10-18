"""
Extension: the full PrimeKG knowledge graph (129k nodes, 4.05M edges, 10 node types, 30 relations) as a local,
offline Synapta evidence source. One investigation per turn plans up to five analyses from the question:
drug / disease / gene profiles, drug-disease mechanism, network-proximity repurposing, phenotype-driven
differential diagnosis, polypharmacy, gene-set enrichment and similarity.

The source runs only when the compiled store exists (`phronesis-primekg build`); the graph is loaded on the
first call and kept in memory.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from phronesis.agents.query_context import QueryContext, SourceDecision
from phronesis.extensions import hooks
from phronesis.sources import register_source
from phronesis.sources.base import CitationRecord, EvidenceSource, SourceRunState
from phronesis.tools.registry import ErrorType, ToolResult

TOOL = "primekg_investigate"
QUERY_TOOL = "primekg_query"

# The synthesis system prompt is at its size budget (tests/test_prompts.py), so PrimeKG adds no standing
# component or rule: its reading rules travel in the <how_to_read> of its own evidence block, so they appear
# only on turns where PrimeKG ran.
PROMPT_COMPONENTS: Dict[str, str] = {}
PROMPT_RULES: Dict[str, str] = {}


class PrimeKGInvestigateInput(BaseModel):
    query: str = Field(max_length=4000)
    drugs: List[str] = Field(default_factory=list, max_length=8)
    diseases: List[str] = Field(default_factory=list, max_length=4)
    # Readable names for `diseases` (same order): diseases are queried by MONDO id, but shown by name.
    disease_names: List[str] = Field(default_factory=list, max_length=4)
    # The user's own words for `diseases` (same order): "pancreatic cancer" when the resolver chose "exocrine
    # pancreatic carcinoma"; used to find a related PrimeKG node that holds the disease's indications.
    disease_mentions: List[str] = Field(default_factory=list, max_length=4)
    genes: List[str] = Field(default_factory=list, max_length=200)
    question_type: Optional[str] = Field(default=None, max_length=40)


class PrimeKGQueryInput(BaseModel):
    operation: str = Field(pattern=r"^[a-z_]{3,30}$")
    arguments: Dict[str, Any] = Field(default_factory=dict)


def _disease_pairs(ctx: QueryContext) -> List[Tuple[str, str]]:
    """(query, readable name) per disease: the MONDO id is the query when known, the label or mention the name."""
    out: Dict[str, str] = {}
    for e in ctx.diseases:
        oid = str(e.ids.get("ontology_id") or "")
        name = str(e.label or e.mention or "")
        query = oid if oid.upper().startswith("MONDO_") else name
        if query:
            out.setdefault(query, name or query)
    return list(out.items())


def _disease_names(ctx: QueryContext) -> List[str]:
    return [name for _, name in _disease_pairs(ctx)]


def _disease_mentions(ctx: QueryContext) -> List[str]:
    """The question's wording per disease query (same order as _disease_pairs), '' when none was recorded."""
    out: Dict[str, str] = {}
    for e in ctx.diseases:
        oid = str(e.ids.get("ontology_id") or "")
        name = str(e.label or e.mention or "")
        query = oid if oid.upper().startswith("MONDO_") else name
        if query:
            out.setdefault(query, str(e.mention or ""))
    return list(out.values())


def _entities(ctx: QueryContext) -> Tuple[List[str], List[str], List[str]]:
    drugs = [e.label or e.mention for e in ctx.drugs if (e.label or e.mention)]
    diseases = [query for query, _ in _disease_pairs(ctx)]
    genes = [(e.gene or e.label or e.mention) for e in ctx.genes if (e.gene or e.label or e.mention)]
    if ctx.primary_symbol and ctx.primary_symbol not in genes:
        genes.insert(0, ctx.primary_symbol)
    return list(dict.fromkeys(drugs)), list(dict.fromkeys(diseases)), list(dict.fromkeys(genes))


class PrimeKGSource(EvidenceSource):
    key = "primekg"
    agent = "Synapta"
    action = "PrimeKG knowledge-graph analysis"
    required_tools = (TOOL,)
    order = 16

    def should_run(self, ctx: QueryContext) -> SourceDecision:
        from phronesis.kgintel.planner import has_kg_intent
        from phronesis.kgintel.store import store_available

        if not store_available():
            return SourceDecision(run=False, reason="PrimeKG store not built (run `phronesis-primekg build`)")
        drugs, diseases, genes = _entities(ctx)
        text = ctx.clean_query or ctx.raw_query or ""
        if drugs or diseases or genes:
            names = ", ".join((drugs + _disease_names(ctx) + genes)[:4])
            return ctx.gate_decision(self.key, SourceDecision(run=True, reason=f"graph evidence for {names}"))
        if has_kg_intent(text):
            return ctx.gate_decision(self.key, SourceDecision(run=True, reason="phenotype, regimen or graph question"))
        return SourceDecision(run=False, reason="no drug, disease, gene or phenotype question")

    def running_title(self, ctx: QueryContext) -> str:
        return "Querying the PrimeKG knowledge graph (129k nodes, 4M edges)"

    def plan_fact(self, ctx: QueryContext) -> Optional[str]:
        drugs, _diseases, genes = _entities(ctx)
        named = ", ".join((drugs + _disease_names(ctx) + genes)[:5])
        return f"PrimeKG graph analysis{': ' + named if named else ''}"

    async def run(self, ctx: QueryContext, registry: Any, state: SourceRunState) -> ToolResult:
        drugs, diseases, genes = _entities(ctx)
        qtype = ctx.search_plan.question_type if ctx.search_plan is not None else None
        return await registry.execute(TOOL, query=ctx.clean_query or ctx.raw_query or "", drugs=drugs[:8],
                                      diseases=diseases[:4], disease_names=_disease_names(ctx)[:4],
                                      disease_mentions=_disease_mentions(ctx)[:4], genes=genes[:200],
                                      question_type=qtype)

    def describe(self, result: ToolResult, ctx: QueryContext) -> Tuple[str, str]:
        from phronesis.phronesis.agents.verifier import truncate_words  # step text: cut on a word, with "…"

        if not result.success:
            err = getattr(result.error_type, "value", result.error_type) or "ERROR"
            return "PrimeKG analysis failed", f"{err}: {truncate_words(result.error, 200)}"
        data = result.data or {}
        done = [a for a in data.get("analyses") or [] if not a.get("error")]
        if not done:
            return "PrimeKG had nothing to add", truncate_words("; ".join(data.get("notes") or []), 240)
        labels = ", ".join(a.get("reason") or a["operation"] for a in done)
        return f"Ran {len(done)} PrimeKG {'analysis' if len(done) == 1 else 'analyses'}", truncate_words(labels, 300)

    def evidence_xml(self, result: ToolResult) -> str:
        from phronesis.kgintel.render import evidence_xml

        data = result.data or {}
        return evidence_xml(data) if data.get("analyses") else ""

    def allowed_identifiers(self, result: ToolResult) -> Dict[str, Set[str]]:
        from phronesis.kgintel.render import identifiers

        return identifiers(result.data or {})

    def citations(self, result: ToolResult) -> List[CitationRecord]:
        from phronesis.kgintel.render import citation_dicts

        return [CitationRecord(**c, source_agent=self.agent, source_tool=TOOL) for c in citation_dicts(result.data or {})]

    def artifact(self, result: ToolResult) -> Optional[Tuple[str, Any]]:
        data = result.data or {}
        done = [a for a in data.get("analyses") or [] if not a.get("error")]
        if not done:
            return None
        return "primekg", {"analyses": done, "provenance": data.get("provenance"), "entities": data.get("entities"),
                           "unresolved": data.get("unresolved")}

    def fallback_markdown(self, result: ToolResult) -> str:
        from phronesis.kgintel.render import fallback_markdown

        data = result.data or {}
        return fallback_markdown(data) if data.get("analyses") else ""

    def scope_notes(self, ctx: QueryContext, result: Optional[ToolResult]) -> List[str]:
        if result is None:
            return []
        if not result.success:
            return [f"The PrimeKG analysis failed ({str(result.error or '')[:160]}); no knowledge-graph evidence is available."]
        data = result.data or {}
        notes = []
        if data.get("unresolved"):
            notes.append(f"PrimeKG has no node matching: {', '.join(data['unresolved'][:8])}.")
        for a in data.get("analyses") or []:
            if a.get("error"):
                notes.append(f"PrimeKG {a.get('operation')} did not run: {a['error'][:160]}")
            if a.get("operation") == "repurposing" and not a.get("error"):
                res = a.get("result") or {}
                cal = res.get("calibration") or {}
                name = (res.get("disease") or {}).get("name") or "this disease"
                if cal.get("signal") == "low":
                    notes.append(f"PrimeKG's repurposing ranking carries little signal for {name}: its known indications "
                                 f"rank with AUROC {cal.get('auroc_known_indications')}, so present its candidates as "
                                 "low-confidence hypotheses.")
                for p in res.get("indications_borrowed_from") or []:
                    notes.append(f"PrimeKG records no indications for {name}; its repurposing calibration and "
                                 f"exclusions use the indications of the related node '{p.get('name')}' "
                                 f"({p.get('curie')}). Say so when citing them.")
                if str(cal.get("signal", "")).startswith("unknown"):
                    notes.append(f"{name} has no known indication in PrimeKG, so its repurposing ranking cannot be "
                                 "calibrated; present its candidates as unvalidated hypotheses.")
        notes.append("PrimeKG is a 2022/2023 snapshot (Harvard Dataverse v2.1); drugs approved later are absent.")
        return notes


register_source(PrimeKGSource())


def register_prompt_hooks() -> None:
    for key, line in PROMPT_COMPONENTS.items():
        hooks.register_prompt_component(key, line)
    for key, text in PROMPT_RULES.items():
        hooks.register_prompt_rule(key, text)


def investigate(query: str, drugs: Optional[List[str]] = None, diseases: Optional[List[str]] = None,
                genes: Optional[List[str]] = None, question_type: Optional[str] = None,
                disease_names: Optional[List[str]] = None, disease_mentions: Optional[List[str]] = None) -> ToolResult:
    """Plan and run up to five PrimeKG analyses; per-analysis failures are reported inside the result."""
    from phronesis.kgintel import service
    from phronesis.kgintel.graph import PrimeKGError, get_primekg
    from phronesis.kgintel.planner import plan_investigation

    try:
        kg = get_primekg()
    except PrimeKGError as exc:
        return ToolResult(success=False, tool_name=TOOL, error=str(exc), error_type=ErrorType.DATASET_REQUIRED)
    plan = plan_investigation(kg, query, drugs or [], diseases or [], genes or [], question_type,
                              disease_names=disease_names or [], disease_mentions=disease_mentions or [])
    analyses: List[Dict[str, Any]] = []
    unresolved: List[str] = []
    for op, kwargs, reason in plan.analyses:
        try:
            out = service.run(op, kg=kg, **kwargs)
            unresolved.extend(out.get("unresolved") or [])
            analyses.append({"operation": op, "reason": reason, "arguments": kwargs, "resolved": out.get("resolved"),
                             "result": out.get("result")})
        except PrimeKGError as exc:
            analyses.append({"operation": op, "reason": reason, "arguments": kwargs, "error": str(exc)[:300]})
        except Exception as exc:  # noqa: BLE001
            analyses.append({"operation": op, "reason": reason, "arguments": kwargs,
                             "error": f"internal error ({type(exc).__name__})"})
    data = {"analyses": analyses, "entities": plan.entities, "notes": plan.notes,
            "unresolved": list(dict.fromkeys(unresolved)), "provenance": kg.provenance()}
    canonical: Dict[str, str] = {}
    for a in analyses:
        for r in a.get("resolved") or []:
            if r.get("type") == "drug":
                canonical.setdefault("drugbank_id", r["id"])
            elif r.get("type") == "disease" and r.get("curie"):
                canonical.setdefault("ontology_id", r["curie"])
            elif r.get("type") == "gene/protein":
                canonical.setdefault("ncbi_gene_id", r["id"])
    return ToolResult(success=True, tool_name=TOOL, data=data, canonical_ids=canonical,
                      metadata={"source": "PrimeKG (local store)", **kg.provenance()})


def query(operation: str, arguments: Optional[Dict[str, Any]] = None) -> ToolResult:
    from phronesis.kgintel import service
    from phronesis.kgintel.graph import PrimeKGError

    try:
        data = service.run(operation, **(arguments or {}))
    except PrimeKGError as exc:
        etype = {"not_built": ErrorType.DATASET_REQUIRED, "not_found": ErrorType.NOT_FOUND}.get(exc.kind, ErrorType.VALIDATION_ERROR)
        return ToolResult(success=False, tool_name=QUERY_TOOL, error=str(exc)[:300], error_type=etype)
    except TypeError as exc:
        return ToolResult(success=False, tool_name=QUERY_TOOL, error=f"bad arguments for {operation}: {exc}"[:300],
                          error_type=ErrorType.VALIDATION_ERROR)
    return ToolResult(success=True, tool_name=QUERY_TOOL, data=data, metadata={"source": "PrimeKG (local store)"})


def register_tools(registry: Any) -> None:
    from phronesis.tools.registry import AdapterType, ToolUniverseEntry

    registry.register(
        ToolUniverseEntry(
            name=TOOL,
            description="PrimeKG: plan and run knowledge-graph analyses (profiles, mechanism paths, repurposing, "
            "phenotype differential, polypharmacy, enrichment, similarity) for the drugs, diseases, genes and "
            "phenotypes in a question.",
            domain="synapta",
            adapter_type=AdapterType.LOCAL_COMPUTE,
            input_schema=PrimeKGInvestigateInput,
            canonical_id_types=["drugbank_id", "ontology_id", "ncbi_gene_id"],
            timeout_seconds=120.0,  # the first call loads the 8M-entry graph
            tags=["primekg", "knowledge_graph", "drugs", "diseases", "genes", "phenotypes", "repurposing"],
            is_async=False,
        ),
        executor=investigate,
    )
    registry.register(
        ToolUniverseEntry(
            name=QUERY_TOOL,
            description="PrimeKG: run one named operation (resolve, drug_profile, disease_profile, gene_profile, "
            "neighbors, paths, drug_disease, repurposing, phenotype_diagnosis, polypharmacy, enrichment, similar, stats).",
            domain="synapta",
            adapter_type=AdapterType.LOCAL_COMPUTE,
            input_schema=PrimeKGQueryInput,
            timeout_seconds=120.0,
            tags=["primekg", "knowledge_graph"],
            is_async=False,
        ),
        executor=query,
    )
    register_prompt_hooks()
