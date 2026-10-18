"""
Per-inquiry routing context: what was parsed from the question, which sources should run and why,
and the datasets attached to it. Built by query_planner.build_query_context.

Ownership: ENTITIES (feature 3) extends this file additively; fields and methods below keep their names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, Field

EntityType = Literal["gene", "drug", "disease", "variant"]


class ResolvedEntity(BaseModel):
    type: EntityType
    mention: str
    span: Tuple[int, int] = (0, 0)
    label: str
    ids: Dict[str, str] = Field(default_factory=dict)  # gene: hgnc_id/ensembl_id/uniprot_id; drug: chembl_id; disease: ontology_id; variant: rsid
    source: str = "query_text"  # HGNC|OpenTargets|ChEMBL|dbSNP|NCBI Variation|local_lexicon|query_text
    matched_on: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    gene: Optional[str] = None
    variant_kind: Optional[str] = None  # protein_change|rsid|hgvs|fusion|exon_event
    protein_change: Optional[str] = None  # 1-letter, e.g. "G12D"
    validated: bool = False
    ambiguous_with: List[str] = Field(default_factory=list)


class SourceDecision(BaseModel):
    run: bool
    reason: str = ""


SimulationAction = Literal["time_course", "steady_state", "parameter_scan"]
TaskStatus = Literal["completed", "partial", "needs_input", "failed", "cancelled"]

SIM_DURATION_MIN_SECONDS = 1.0
SIM_DURATION_MAX_SECONDS = 5000.0
SIM_SCAN_MAX_POINTS = 50
SIM_MAX_OVERRIDES = 20


class SimulationConstraints(BaseModel):
    """Scoped simulation instructions. Only simulation_forbidden suppresses execution."""

    simulation_requested: bool = False
    simulation_forbidden: bool = False
    replacement_forbidden: bool = False  # "do not substitute/invent/replace ..." - not a ban on computing
    reject_if_parameter_missing: bool = False
    scope: Literal["current_turn", "investigation"] = "current_turn"


class SimulationPlan(BaseModel):
    """The exact simulation the user asked for. A plan with validation_errors is not executed."""

    action: SimulationAction = "time_course"
    biomodel_id: Optional[str] = None
    duration_seconds: float = 60.0
    duration_explicit: bool = False
    intervals: int = 100
    parameter_overrides: Dict[str, float] = Field(default_factory=dict)
    scan_parameter: Optional[str] = None
    scan_start: Optional[float] = None
    scan_end: Optional[float] = None
    scan_points: Optional[int] = None  # includes both endpoints
    target_species: List[str] = Field(default_factory=list)
    validation_errors: List[Dict[str, str]] = Field(default_factory=list)  # [{"code", "message"}]

    @property
    def is_valid(self) -> bool:
        return not self.validation_errors


class TaskOutcome(BaseModel):
    """Whether the requested operation ran, kept separate from evidence grounding."""

    task_status: TaskStatus = "completed"
    requested_operation: Optional[str] = None
    executed_operation: Optional[str] = None
    request_match: bool = True
    reason: str = ""
    grounding_status: Literal["supported", "partial", "unsupported", "contradicted", "not_checked"] = "not_checked"
    # Why an otherwise finished run is partial (budget stop, planner fallback) and the sub-questions left open.
    incomplete_reasons: List[str] = Field(default_factory=list)
    unresolved: List[str] = Field(default_factory=list)


@dataclass
class DatasetRef:
    id: str  # ^ds_[0-9a-f]{12}$
    kind: Literal["h5ad", "table", "sbml"]
    display_name: str
    analysis_path: Optional[str] = None  # server-only; never serialized to clients
    model_path: Optional[str] = None  # server-only
    public_provenance: Dict[str, Any] = field(default_factory=dict)
    dims: Optional[str] = None
    geo_accession: Optional[str] = None
    geo_verified: bool = False
    pmids: List[str] = field(default_factory=list)
    sbml_model_id: Optional[str] = None

    def to_public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "display_name": self.display_name,
            "public_provenance": dict(self.public_provenance),
            "dims": self.dims,
            "geo_accession": self.geo_accession,
            "geo_verified": self.geo_verified,
            "pmids": list(self.pmids),
            "sbml_model_id": self.sbml_model_id,
        }


def _is_unresolved_planner_term(e: ResolvedEntity) -> bool:
    return not e.validated and e.matched_on == "planner"


# QueryContext.observed_identifiers() key for each ResolvedEntity.ids key.
_ENTITY_ID_FAMILIES = {
    "ensembl_id": "ensembl",
    "uniprot_id": "uniprot",
    "hgnc_id": "hgnc",
    "chembl_id": "chembl",
    "ontology_id": "ontology",
    "rsid": "rsid",
}


@dataclass
class QueryContext:
    raw_query: str
    clean_query: str
    user_id: str = "default_user"
    thread_id: str = ""
    entities: List[ResolvedEntity] = field(default_factory=list)
    intents: Set[str] = field(default_factory=set)
    primary_target: Optional[str] = None  # legacy display string, e.g. "KRAS G12D"
    primary_gene: Optional[ResolvedEntity] = None
    biomodel_id: Optional[str] = None  # typed BIOMD accession
    sim_duration: float = 60.0
    suppress_simulation: bool = False  # HARD GUARD
    is_simulation_intent: bool = False
    wants_steady_state: bool = False
    simulation_plan: Optional[SimulationPlan] = None
    simulation_constraints: SimulationConstraints = field(default_factory=SimulationConstraints)
    # "papers only" / "trials only": the only sources this turn may run (None = no restriction).
    source_allowlist: Optional[Set[str]] = None
    source_restriction_reason: str = ""
    dataset_path: Optional[str] = None  # typed path, already passed is_allowed_typed_path
    has_single_cell_intent: bool = False
    # Analyses the question explicitly excludes ("No ... hypothesis generation or debate is needed"):
    # hypothesis | debate | fda_sweep | omics | simulation | single_cell (query_planner.detect_excluded_modules).
    excluded_modules: Set[str] = field(default_factory=set)
    attachments: List[DatasetRef] = field(default_factory=list)
    attachment_errors: List[str] = field(default_factory=list)
    enable_web_search: bool = True
    decisions: Dict[str, SourceDecision] = field(default_factory=dict)
    planner: str = "deterministic"  # deterministic|llm|llm_unavailable
    scope: str = "research"  # research|conversational|unclear; only research runs any source
    normalization: str = "skipped"  # resolved|partial|offline_lexicon|skipped
    source_status: Dict[str, str] = field(default_factory=dict)
    timings_ms: Dict[str, float] = field(default_factory=dict)
    extras: Dict[str, Any] = field(default_factory=dict)
    search_plan: Optional[Any] = None  # search_plan.SearchPlan: what the literature and web searches look for
    planner_task: Optional[Any] = field(default=None, repr=False, compare=False)  # the planner model call, if started
    planner_started: float = 0.0

    # -- entity views --------------------------------------------------------
    def _of_type(self, kind: str) -> List[ResolvedEntity]:
        return [e for e in self.entities if e.type == kind]

    @property
    def genes(self) -> List[ResolvedEntity]:
        return self._of_type("gene")

    @property
    def drugs(self) -> List[ResolvedEntity]:
        return self._of_type("drug")

    @property
    def diseases(self) -> List[ResolvedEntity]:
        return self._of_type("disease")

    @property
    def variants(self) -> List[ResolvedEntity]:
        return self._of_type("variant")

    @property
    def primary_symbol(self) -> Optional[str]:
        if self.primary_gene is not None:
            return self.primary_gene.label
        return self.primary_target.split()[0] if self.primary_target else None

    @property
    def primary_display(self) -> Optional[str]:
        """primary_target with the user's alias, e.g. 'PTPN11 (SHP2)' or 'KRAS G12C'."""
        if self.primary_target is None or self.primary_gene is None:
            return self.primary_target
        mention, label = self.primary_gene.mention, self.primary_gene.label
        if not mention or mention.upper() == label.upper():
            return self.primary_target
        rest = self.primary_target[len(label):] if self.primary_target.startswith(label) else ""
        return f"{label} ({mention}){rest}"

    @property
    def primary_ensembl_id(self) -> Optional[str]:
        return self.primary_gene.ids.get("ensembl_id") if self.primary_gene is not None else None

    def primary_aliases(self) -> List[str]:
        if self.primary_gene is None:
            return []
        aliases = list(self.primary_gene.aliases)
        mention = self.primary_gene.mention
        if mention and mention != self.primary_gene.label and mention not in aliases:
            aliases.append(mention)
        return aliases

    # -- attachments ---------------------------------------------------------
    @property
    def dataset_attachment(self) -> Optional[DatasetRef]:
        return next((a for a in self.attachments if a.kind in ("h5ad", "table")), None)

    @property
    def model_attachment(self) -> Optional[DatasetRef]:
        return next((a for a in self.attachments if a.kind == "sbml"), None)

    @property
    def literature_ids(self) -> List[str]:
        """PMIDs, PMCIDs and DOIs the question names: retrieval obligations fetched by exact identifier first."""
        from ..lithoscribe.identifiers import extract_literature_ids

        return extract_literature_ids(self.raw_query or self.clean_query or "")

    def excludes(self, module: str) -> bool:
        return module in (self.excluded_modules or set())

    @property
    def analysis_dataset_path(self) -> Optional[str]:
        att = self.dataset_attachment
        if att is not None and att.analysis_path:
            return att.analysis_path
        return self.dataset_path

    @property
    def single_cell_attachment(self) -> Optional[DatasetRef]:
        return next((a for a in self.attachments if a.kind == "h5ad" and a.analysis_path), None)

    @property
    def single_cell_dataset_path(self) -> Optional[str]:
        """What Lumicell can read: an attached .h5ad or a typed path. A CSV/TSV table is not single-cell data."""
        att = self.single_cell_attachment
        return att.analysis_path if att is not None else self.dataset_path

    # -- routing -------------------------------------------------------------
    def should_run(self, name: str) -> bool:
        return self.decisions.get(name, SourceDecision(run=False)).run and self.source_allowed(name)

    def source_allowed(self, name: str) -> bool:
        return self.source_allowlist is None or name in self.source_allowlist

    def gate_decision(self, name: str, decision: SourceDecision) -> SourceDecision:
        """Apply the turn's source restriction ("papers only") to a source's own run decision."""
        if decision.run and not self.source_allowed(name):
            return SourceDecision(run=False, reason=self.source_restriction_reason or "excluded at your request")
        return decision

    def focus_terms(self) -> List[str]:
        """Approved symbol (plus the alias typed), up to two drugs and one disease, as written by the user."""
        terms: List[str] = []
        if self.primary_target:
            terms.append(self.primary_target)
            alias = self.primary_gene.mention if self.primary_gene is not None else ""
            if alias and alias.upper() != (self.primary_symbol or "").upper():
                terms.append(alias)
        if terms:
            terms.extend(d.mention for d in self.drugs[:2] if d.mention)
            terms.extend(d.mention for d in self.diseases[:1] if d.mention)
        return list(dict.fromkeys(terms))

    def web_query(self) -> str:
        terms = self.focus_terms()
        return f"{' '.join(terms) if terms else self.clean_query} mechanism kinetics"

    def biomodel_search_term(self) -> str:
        # Model names use the common name ("ERK", "SHP2"), so search with what the user typed.
        if self.primary_gene is not None and self.primary_gene.mention and self.primary_gene.span != (0, 0):
            return self.primary_gene.mention
        return self.primary_target or self.clean_query

    def lumicell_target_gene(self) -> Optional[str]:
        # The approved symbol only: var_names never contain a variant suffix such as "KRAS G12D".
        return self.primary_symbol if self.primary_target else None

    def plan_facts(self) -> List[str]:
        """Human-readable routing facts for the plan step (legacy wording kept exactly)."""
        facts: List[str] = [f"Target: {self.primary_display}" if self.primary_target else "No gene/protein target named"]
        facts.extend(self._entity_facts())
        if self.biomodel_id:
            facts.append(f"BioModel: {self.biomodel_id}")
        model = self.model_attachment
        if model is not None:
            facts.append(f"Model: {model.display_name} (uploaded SBML)")
        followup = self.extras.get("resolved_followup")
        if isinstance(followup, dict) and followup.get("subject"):
            facts.append(f"Follow-up: subject {followup['subject']} from the previous question")
        plan = self.simulation_plan
        if self.suppress_simulation:
            facts.append("Simulation: skipped at your request")
        elif plan is not None and plan.validation_errors:
            facts.append("Simulation: not run - " + "; ".join(e.get("message", e.get("code", "")) for e in plan.validation_errors[:3]))
        elif plan is not None and plan.action == "parameter_scan":
            rng = f" from {plan.scan_start:g} to {plan.scan_end:g}" if plan.scan_start is not None and plan.scan_end is not None else ""
            pts = f", {plan.scan_points} points" if plan.scan_points else ""
            facts.append(f"Simulation: parameter scan of {plan.scan_parameter}{rng}{pts}, {plan.duration_seconds:g}s per point")
        elif self.is_simulation_intent:
            facts.append(f"Simulation: {self.sim_duration:g}s time course" + (" + steady state" if self.wants_steady_state else ""))
        if plan is not None and plan.parameter_overrides and not plan.validation_errors:
            facts.append("Overrides: " + ", ".join(f"{k}={v:g}" for k, v in list(plan.parameter_overrides.items())[:5]))
        if self.source_allowlist is not None and self.source_restriction_reason:
            facts.append(f"Sources: {self.source_restriction_reason}")
        dataset = self.dataset_attachment
        if dataset is not None:
            facts.append(f"Dataset: {dataset.display_name} ({dataset.id}, uploaded{', ' + dataset.dims if dataset.dims else ''})")
        elif self.dataset_path:
            facts.append(f"Dataset: {self.dataset_path}")
        if self.has_single_cell_intent and not self.single_cell_dataset_path:
            facts.append("Single-cell analysis needs a .h5ad/.zarr file; none referenced")
        facts.append("Web search: on" if self.enable_web_search else "Web search: off")
        return facts

    def _entity_facts(self) -> List[str]:
        def show(e: ResolvedEntity) -> str:
            return e.label if not e.mention or e.mention.lower() == e.label.lower() else f"{e.label} ({e.mention})"

        facts: List[str] = []
        families = [g for g in self.genes if g.ambiguous_with]
        if families:
            facts.append("Ambiguous: " + ", ".join(f"{g.mention} ({', '.join(g.ambiguous_with[:4])})" for g in families[:2]))
        if self.drugs:
            facts.append(("Drugs: " if len(self.drugs) > 1 else "Drug: ") + ", ".join(show(d) for d in self.drugs[:3]))
        # A disease the planner model named that no ontology confirmed is shown as an unresolved term, not a fact.
        established = [d for d in self.diseases if not _is_unresolved_planner_term(d)]
        unresolved = [d for d in self.diseases if _is_unresolved_planner_term(d)]
        if established:
            facts.append("Disease: " + ", ".join(show(d) for d in established[:2]))
        if unresolved:
            facts.append("Unresolved term: " + ", ".join(d.mention for d in unresolved[:2]))
        if self.variants:
            facts.append(("Variants: " if len(self.variants) > 1 else "Variant: ") + ", ".join(self._variant_fact(v) for v in self.variants[:3]))
        return facts

    @staticmethod
    def _variant_fact(v: ResolvedEntity) -> str:
        # A resolver looked this variant up and could not confirm it (dbSNP error document, invalid HGVS).
        if v.validated or v.source not in ("dbSNP", "NCBI Variation", "HGNC"):
            return v.label
        return f"{v.label} ({'not found in dbSNP' if v.source == 'dbSNP' else 'HGVS not confirmed'})"

    def to_step_extra(self) -> Dict[str, Any]:
        return {
            "entities": [e.model_dump() for e in self.entities],
            "intents": sorted(self.intents),
            "sources": [{"name": k, "run": d.run, "reason": d.reason} for k, d in self.decisions.items()],
            "planner": self.planner,
            "scope": self.scope,
            "normalization": self.normalization,
            "latency_ms": round(sum(self.timings_ms.values()), 1),
        }

    def to_artifact(self) -> Dict[str, Any]:
        """JSON-safe summary for the client: no server paths (typed paths are reduced to their basename)."""
        dataset_name = None
        if self.dataset_path:
            dataset_name = re.split(r"[\\/]", self.dataset_path)[-1]
        return {
            "primary_target": self.primary_target,
            "primary_symbol": self.primary_symbol,
            "biomodel_id": self.biomodel_id,
            "sim_duration": self.sim_duration,
            "suppress_simulation": self.suppress_simulation,
            "is_simulation_intent": self.is_simulation_intent,
            "wants_steady_state": self.wants_steady_state,
            "dataset": dataset_name,
            "has_single_cell_intent": self.has_single_cell_intent,
            "enable_web_search": self.enable_web_search,
            "attachments": [a.to_public() for a in self.attachments],
            "attachment_errors": list(self.attachment_errors),
            "source_status": dict(self.source_status),
            "search_plan": self.search_plan.to_dict() if self.search_plan is not None else None,
            "simulation_plan": self.simulation_plan.model_dump() if self.simulation_plan is not None else None,
            "simulation_constraints": self.simulation_constraints.model_dump(),
            "source_allowlist": sorted(self.source_allowlist) if self.source_allowlist is not None else None,
            "resolved_followup": self.extras.get("resolved_followup"),
            **self.to_step_extra(),
        }

    def observed_identifiers(self) -> Dict[str, Set[str]]:
        """Identifiers this context legitimately knows about, merged into the verifier's observed sets."""
        out: Dict[str, Set[str]] = {k: set() for k in _ENTITY_ID_FAMILIES.values()}
        out.update({"dataset": set(), "geo": set(), "pmids": set(), "biomodels": set()})
        for e in self.entities:
            if not e.validated:
                continue
            for id_key, family in _ENTITY_ID_FAMILIES.items():
                value = e.ids.get(id_key)
                if value:
                    out[family].add(str(value))
        for att in self.attachments:
            out["dataset"].add(att.id)
            # Declared GEO accessions count only once NCBI confirmed them; an uploaded SBML's own
            # model id is the file's label, not a BioModels accession.
            if att.geo_verified:
                if att.geo_accession:
                    out["geo"].add(att.geo_accession)
                out["pmids"].update(str(p) for p in att.pmids if p)
        return out
