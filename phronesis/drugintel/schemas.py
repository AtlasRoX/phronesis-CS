"""Input schemas for the drug-intelligence registry tools and the models of their outputs."""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, constr

ChemblId = constr(pattern=r"^CHEMBL\d{1,9}$")
DrugName = constr(min_length=2, max_length=80)

FAERS_CAVEAT = (
    "Counts are spontaneous reports mentioning the drug in any role; they do not establish causation "
    "and are not incidence rates."
)


# -----------------------------------------------------------------------------
# Tool inputs
# -----------------------------------------------------------------------------

# Up to 8 names per call: agents resolve a whole candidate list at once (the swarm asked for 4-6). Name lookups are
# independent and run at most NAME_RESOLVE_CONCURRENCY at a time (engine.py), so ChEMBL sees no more parallel
# requests than before and 8 names cost two short rounds (~1-3 s each when ChEMBL is healthy) within the 40 s
# tool timeout. ChEMBL ids stay at 3: they come from the question or from one earlier result.
MAX_RESOLVE_NAMES = 8


class ChemblResolveInput(BaseModel):
    names: List[DrugName] = Field(default_factory=list, max_length=MAX_RESOLVE_NAMES)
    chembl_ids: List[ChemblId] = Field(default_factory=list, max_length=3)
    target_symbol: Optional[constr(max_length=40)] = None


class ChemblProfileInput(BaseModel):
    parent_chembl_ids: List[ChemblId] = Field(default_factory=list, max_length=3)
    target_chembl_id: Optional[str] = Field(default=None, pattern=r"^CHEMBL\d{1,9}$")
    activity_limit: int = Field(default=10, ge=1, le=50)
    # Context carried over from chembl_resolve_drugs so the profile can say how each drug was matched.
    target_symbol: Optional[constr(max_length=40)] = None
    query_names: Dict[str, str] = Field(default_factory=dict)
    forms: Dict[str, List[str]] = Field(default_factory=dict)
    matched_via: Dict[str, str] = Field(default_factory=dict)
    # The words the user wrote for each drug ("MRTX1133"), so a drug without a ChEMBL preferred name is not
    # shown to the reader as its ChEMBL id.
    mentions: Dict[str, str] = Field(default_factory=dict)
    include_target_approved: Optional[bool] = None  # default: only when no drug ids are given
    include_target_bioactive: Optional[bool] = None  # default: only when no drug ids are given
    # The target's preferred name from chembl_resolve_drugs: the profile falls back to it when its own target-name
    # lookup fails, and skips that lookup when no drug ids are given.
    target_pref_name: Optional[constr(max_length=200)] = None


class OpenFdaSafetyInput(BaseModel):
    ingredient: str = Field(..., min_length=2, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9 \-]*$")
    brand_names: List[constr(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9 \-]*$")] = Field(
        default_factory=list, max_length=3
    )
    top_n: int = Field(default=10, ge=1, le=100)
    include_label: bool = True


# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------

class Mechanism(BaseModel):
    mechanism_of_action: str
    action_type: Optional[str] = None
    target_chembl_id: Optional[str] = None
    target_pref_name: Optional[str] = None
    form_chembl_id: str
    max_phase: Optional[float] = None


class Bioactivity(BaseModel):
    activity_id: int
    assay_chembl_id: str
    assay_description: Optional[str] = None
    assay_type: Optional[str] = None
    document_chembl_id: Optional[str] = None
    molecule_chembl_id: str
    target_chembl_id: str
    target_organism: Optional[str] = None
    standard_type: str
    standard_relation: Optional[str] = None
    standard_value: Optional[float] = None
    standard_units: Optional[str] = None
    pchembl_value: Optional[float] = None
    data_validity_comment: Optional[str] = None


class DrugRecord(BaseModel):
    query_name: str
    mention: Optional[str] = None  # the user's own words for the drug, when known
    chembl_id: str  # parent molecule
    pref_name: Optional[str] = None
    forms: List[str] = Field(default_factory=list)  # salt / alternative form ids collapsed into the parent
    matched_via: Literal["pref_name", "synonym", "chembl_id"]
    max_phase: Optional[float] = None
    max_phase_meaning: str
    first_approval: Optional[int] = None
    molecule_type: Optional[str] = None
    chembl_black_box_flag: Optional[int] = None
    withdrawn_flag: Optional[bool] = None
    trade_names: List[str] = Field(default_factory=list)
    mechanisms: List[Mechanism] = Field(default_factory=list)
    bioactivities: List[Bioactivity] = Field(default_factory=list)
    bioactivity_total: Optional[int] = None


class TargetRef(BaseModel):
    target_chembl_id: str
    pref_name: Optional[str] = None
    symbol_query: Optional[str] = None


class ApprovedDrug(BaseModel):
    chembl_id: str
    pref_name: Optional[str] = None
    mechanism_of_action: Optional[str] = None
    first_approval: Optional[int] = None
    molecule_type: Optional[str] = None


class BioactiveCompound(BaseModel):
    """One compound (by parent) with measured activity against the target; its best record by pChEMBL."""

    chembl_id: str
    pref_name: Optional[str] = None
    max_phase: Optional[float] = None
    molecule_type: Optional[str] = None
    best_pchembl: Optional[float] = None
    standard_type: Optional[str] = None
    standard_relation: Optional[str] = None
    standard_value: Optional[float] = None
    standard_units: Optional[str] = None
    assay_chembl_id: Optional[str] = None
    activity_id: Optional[int] = None
    records: int = 1  # activity records for this compound among those fetched


class ChemblResolveOutput(BaseModel):
    chembl_release: Optional[str] = None
    target: Optional[TargetRef] = None
    target_lookup: Literal["resolved", "not_found", "skipped", "error"] = "skipped"
    target_lookup_error: Optional[str] = None  # set when target_lookup is "error"
    target_lookup_error_kind: Optional[str] = None  # ChemblError.kind: timeout | http | transport | ...
    drugs: List[DrugRecord] = Field(default_factory=list)
    unresolved_names: List[str] = Field(default_factory=list)
    invalid_chembl_ids: List[str] = Field(default_factory=list)
    lookup_errors: List[Dict[str, str]] = Field(default_factory=list)  # [{"query", "error"}]: upstream failures per name


class ChemblProfileOutput(BaseModel):
    chembl_release: Optional[str] = None
    target: Optional[TargetRef] = None
    drugs: List[DrugRecord] = Field(default_factory=list)
    unresolved_names: List[str] = Field(default_factory=list)
    target_approved_drugs: List[ApprovedDrug] = Field(default_factory=list)
    target_approved_total: Optional[int] = None
    # "ok" when the approved-drugs-for-target lookup answered, "failed" when it did not (timeout, HTTP error):
    # an empty target_approved_drugs then means "unknown", not "none". None when the lookup was not requested.
    target_approved_status: Optional[Literal["ok", "failed"]] = None
    target_approved_error: Optional[str] = None
    target_approved_error_kind: Optional[str] = None  # ChemblError.kind: timeout | http | transport | bad_body
    # Compounds with measured IC50/Ki/Kd/EC50 activity against the target (no drug named): research compounds,
    # mostly not approved drugs. Distinct compounds among the activity records fetched, best pChEMBL first.
    target_bioactive_compounds: List[BioactiveCompound] = Field(default_factory=list)
    target_bioactive_distinct: Optional[int] = None  # distinct compounds among the records fetched
    target_activity_records_total: Optional[int] = None  # activity records with a pChEMBL value in ChEMBL
    target_activity_records_fetched: Optional[int] = None
    lookup_errors: List[Dict[str, str]] = Field(default_factory=list)


class FaersReaction(BaseModel):
    term: str
    count: int


class LabelExcerpt(BaseModel):
    set_id: str
    effective_time: Optional[str] = None
    generic_name: Optional[str] = None
    combination_product: Optional[bool] = None  # None when openFDA did not harmonize the label
    brand_name: Optional[str] = None
    manufacturer_name: Optional[str] = None
    application_number: Optional[str] = None
    harmonized: bool
    boxed_warning: Optional[str] = None
    boxed_warning_truncated: bool = False
    warnings: Optional[str] = None
    warnings_section: Literal["warnings_and_cautions", "warnings", None] = None
    warnings_truncated: bool = False
    contraindications: Optional[str] = None
    contraindications_truncated: bool = False
    indications_and_usage: Optional[str] = None
    indications_truncated: bool = False
    dailymed_url: str


class FdaSupplement(BaseModel):
    """One approved efficacy supplement (submission class EFFICACY: typically a new or changed indication)."""

    application_number: str
    submission_number: Optional[str] = None
    date: Optional[str] = None  # YYYY-MM-DD
    review_priority: Optional[str] = None


class FdaApplication(BaseModel):
    application_number: str  # "NDA208558", "BLA125514"
    sponsor: Optional[str] = None
    brand_names: List[str] = Field(default_factory=list)
    marketing_status: List[str] = Field(default_factory=list)
    original_approval_date: Optional[str] = None  # YYYY-MM-DD of the approved ORIG submission
    combination: bool = False  # every product of the application has more than one active ingredient
    marketed: bool = False  # a product is marketed (prescription or over the counter)


class FdaApproval(BaseModel):
    """Drugs@FDA (openFDA drug/drugsfda) approval history of one active ingredient's NDA/BLA applications.

    status: "approved" (a single-ingredient NDA/BLA with an approved original submission), "combination_only"
    (approved only as part of combination products), "not_found" (Drugs@FDA lists no approved NDA/BLA for it) or
    "error" (the lookup failed: unknown, never "not approved")."""

    status: Literal["approved", "combination_only", "not_found", "error"]
    ingredient: str
    search: Optional[str] = None
    application_number: Optional[str] = None  # the current (marketed) application, else the first approved one
    sponsor: Optional[str] = None
    brand_names: List[str] = Field(default_factory=list)
    marketed: Optional[bool] = None
    first_approval_date: Optional[str] = None
    first_approval_application: Optional[str] = None
    applications: List[FdaApplication] = Field(default_factory=list)
    applications_total: int = 0
    efficacy_supplements: List[FdaSupplement] = Field(default_factory=list)
    efficacy_supplements_total: int = 0
    unapproved_applications: List[str] = Field(default_factory=list)  # e.g. tentative approvals only
    error: Optional[str] = None
    url: Optional[str] = None


class OpenFdaSafetyOutput(BaseModel):
    ingredient: str
    disclaimer: str
    last_updated: Optional[str] = None
    faers_search: str
    total_reports: int
    serious_reports: Optional[int] = None
    top_reactions: List[FaersReaction] = Field(default_factory=list)
    # MedDRA terms that are not adverse reactions (death, disease progression, off-label use, product-use and
    # efficacy terms) are kept out of top_reactions; deaths are reported separately as an outcome.
    excluded_terms: int = 0
    excluded_reactions: List[FaersReaction] = Field(default_factory=list)
    deaths_reported: Optional[int] = None
    label: Optional[LabelExcerpt] = None
    label_search: Optional[str] = None
    caveats: List[str] = Field(default_factory=lambda: [FAERS_CAVEAT])
    # The name and FAERS field whose search found the reports: the ingredient, or its US adopted name
    # ("mesalazine" -> "mesalamine"), on patient.drug.activesubstance or patient.drug.openfda.generic_name.
    # None when no search found any report.
    us_name: Optional[str] = None
    faers_matched_name: Optional[str] = None
    faers_match_field: Optional[Literal["activesubstance", "openfda.generic_name"]] = None
    faers_searches_tried: List[str] = Field(default_factory=list)
    # Drugs@FDA approval status of the ingredient (None when the lookup was not made).
    approval: Optional[FdaApproval] = None
