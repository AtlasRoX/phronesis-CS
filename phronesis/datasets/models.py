"""Records for uploaded datasets and models. Nothing here ever carries a server path."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

DATASET_ID_RE = re.compile(r"^ds_[0-9a-f]{12}$")


class DatasetKind(str, Enum):
    H5AD = "h5ad"
    TABLE = "table"
    SBML = "sbml"


class ConsentStatus(str, Enum):
    PUBLIC_DEIDENTIFIED = "public_deidentified"
    IRB_APPROVED = "irb_approved"
    CONSENT_NO_IRB = "consent_obtained_no_irb"
    NOT_HUMAN_SUBJECTS = "not_human_subjects"
    UNKNOWN = "unknown"


class DatasetSource(str, Enum):
    GEO = "geo"
    CELLXGENE = "cellxgene"
    BIOMODELS = "biomodels"
    IN_HOUSE = "in_house"
    COLLABORATOR = "collaborator"
    OTHER = "other"


class DatasetProvenanceIn(BaseModel):
    """Provenance declared by the uploader. Only the GEO accession can be checked against an external source."""

    source: DatasetSource
    geo_accession: Optional[str] = Field(None, pattern=r"^GSE\d{1,9}$")
    organism: Optional[str] = Field(None, max_length=120)
    consent_status: ConsentStatus = ConsentStatus.UNKNOWN
    irb_protocol_id: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9._\-/ ]*$")
    description: Optional[str] = Field(None, max_length=4000)
    attests_no_direct_identifiers: bool
    table_orientation: Optional[Literal["cells_by_genes", "genes_by_cells"]] = None
    # Which matrix Lumicell analyzes ("X", "raw" or a layer name) and its scale; detected when omitted.
    matrix_layer: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")
    input_scale: Optional[Literal["raw_counts", "normalized", "log1p"]] = None

    @field_validator("geo_accession", "organism", "irb_protocol_id", "description", "matrix_layer", "input_scale", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("geo_accession", mode="before")
    @classmethod
    def _upper_accession(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("attests_no_direct_identifiers")
    @classmethod
    def _must_attest(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("You must attest that the file contains no directly identifying patient information.")
        return value


# What a GEO lookup establishes (SCI-014): status "verified" means only that the declared GSE accession exists in GEO
# (and, separately, whether its taxon matches the declared organism). Nothing ties the uploaded matrix to the series,
# so the series and its PubMed IDs stay a link declared by the uploader.
GEO_VERIFICATION_SCOPE = "accession_exists"
GEO_STATUS_LABELS = {
    "verified": "GEO accession exists (link to this file declared by the uploader, not verified)",
    "not_found": "GEO accession not found",
    "lookup_failed": "GEO lookup failed",
    "not_requested": None,
}


class GeoVerification(BaseModel):
    # "verified" is kept for compatibility; it means "accession exists", see GEO_VERIFICATION_SCOPE.
    status: Literal["verified", "not_found", "lookup_failed", "not_requested"] = "not_requested"
    verification_scope: Literal["accession_exists"] = GEO_VERIFICATION_SCOPE
    accession: Optional[str] = None
    title: Optional[str] = None
    taxon: Optional[str] = None
    n_samples: Optional[int] = None
    pdat: Optional[str] = None
    pubmedids: List[str] = Field(default_factory=list)
    bioproject: Optional[str] = None
    samples: List[str] = Field(default_factory=list)  # first 50 GSM accessions
    organism_matches: Optional[bool] = None
    checked_at: Optional[datetime] = None
    error: Optional[str] = None


class ValidationSummary(BaseModel):
    format: str  # "anndata-h5ad" | "csv->anndata" | "tsv->anndata" | "sbml-l2v4" ...
    n_obs: Optional[int] = None
    n_vars: Optional[int] = None
    x_encoding: Optional[str] = None  # array | csr_matrix | csc_matrix
    obs_columns: List[str] = Field(default_factory=list)
    uns_hints: Dict[str, str] = Field(default_factory=dict)
    sbml_level: Optional[int] = None
    sbml_version: Optional[int] = None
    sbml_model_id: Optional[str] = None
    sbml_model_name: Optional[str] = None
    n_species: Optional[int] = None
    n_reaction_parameters: Optional[int] = None
    units: Dict[str, str] = Field(default_factory=dict)
    # Tables: the orientation actually applied (declared, or inferred from the labels when not declared; SCI-016).
    table_orientation: Optional[Literal["cells_by_genes", "genes_by_cells"]] = None
    warnings: List[str] = Field(default_factory=list)


class DatasetRecord(BaseModel):
    """One upload as stored in manifest.json. Storage paths are derived by the store, never recorded here."""

    id: str
    kind: DatasetKind
    display_name: str  # sanitized basename, never used as a path
    original_extension: str
    size_bytes: int
    sha256: str
    derived_sha256: Optional[str] = None  # sha256 of data.h5ad converted from a table
    uploaded_at: datetime
    declared_by: Literal["uploader"] = "uploader"
    provenance: DatasetProvenanceIn
    geo_verification: GeoVerification = Field(default_factory=GeoVerification)
    validation: ValidationSummary
    provenance_history: List[Dict[str, Any]] = Field(default_factory=list)  # {changed_at, previous}

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not DATASET_ID_RE.match(value):
            raise ValueError("invalid dataset id")
        return value

    @property
    def geo_verified(self) -> bool:
        """The declared GSE accession exists in GEO (not: the file belongs to it; SCI-014)."""
        return self.geo_verification.status == "verified"

    @property
    def geo_declared_pmids(self) -> List[str]:
        """PubMed ids of the declared GEO series, as a declared source; none when GEO's taxon contradicts the
        declared organism."""
        geo = self.geo_verification
        if not self.geo_verified or geo.organism_matches is False:
            return []
        return list(geo.pubmedids)

    def dims(self) -> Optional[str]:
        v = self.validation
        if self.kind == DatasetKind.SBML:
            return f"{v.n_species} species" if v.n_species is not None else None
        if v.n_obs is not None and v.n_vars is not None:
            return f"{v.n_obs:,} cells × {v.n_vars:,} genes"
        return None

    def public_provenance(self) -> Dict[str, Any]:
        """The provenance that may reach the evidence block, the verifier and the client (no paths, no free text)."""
        geo = self.geo_verification
        out: Dict[str, Any] = {
            "id": self.id,
            "display_name": self.display_name,
            "kind": self.kind.value,
            "sha256": self.sha256,
            "source": self.provenance.source.value,
            "geo_accession": self.provenance.geo_accession,
            "geo_verified": self.geo_verified,  # = accession exists; kept for compatibility
            "geo_status": geo.status,
            "geo_accession_exists": self.geo_verified,
            "geo_verification_scope": geo.verification_scope,
            "geo_status_label": GEO_STATUS_LABELS.get(geo.status) if self.provenance.geo_accession else None,
            "geo_link": "declared_by_uploader" if self.provenance.geo_accession else None,
            "organism": self.provenance.organism,
            "consent_status": self.provenance.consent_status.value,
            "uploaded_at": self.uploaded_at.isoformat(),
            "declared_by": self.declared_by,
            "sbml_model_id": self.validation.sbml_model_id,
            "matrix_layer": self.provenance.matrix_layer,
            "input_scale": self.provenance.input_scale,
        }
        if self.geo_verified:
            out["geo_title"] = geo.title
            out["geo_taxon"] = geo.taxon
            out["geo_pubmedids"] = self.geo_declared_pmids
            out["geo_pmids_role"] = "declared_source"
            out["geo_organism_matches"] = geo.organism_matches
        return out

    def to_api(self) -> Dict[str, Any]:
        data = self.model_dump(mode="json")
        data["dims"] = self.dims()
        return data


class ResolvedAttachment(BaseModel):
    """A record plus its server-side file locations; only the store creates these, for the record's owner."""

    record: DatasetRecord
    analysis_path: Optional[str] = None  # the h5ad, or data.h5ad converted from a table
    model_path: Optional[str] = None  # the SBML file

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def display_name(self) -> str:
        return self.record.display_name

    @property
    def kind(self) -> DatasetKind:
        return self.record.kind

    def public_provenance(self) -> Dict[str, Any]:
        return self.record.public_provenance()
