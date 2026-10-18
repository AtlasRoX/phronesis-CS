"""One registry of source and tool keys and the names a reader sees for them.

`SOURCE_REGISTRY` maps a key (an EvidenceSource key, an agent key, a variant sub-source or a provenance probe key) to
a `SourceName`:

- `public`: the short public name for prose and citations ("ChEMBL", "OpenTargets/KEGG");
- `description`: the researcher-facing label of the source switch in the approval card;
- `agent`: the agent label the source's evidence is reported under ("Synapta", "Lithoscribe");
- `component`: the internal component's display name when the key is itself a Phronesis component;
- `version_label`: the row label of its data release in the report's reproducibility table.

Strings are the ones the call sites already produced; where two old maps disagreed for the same source, each
field keeps the string its call site printed (for example `lithoscribe` reads "Europe PMC" in prose while its
approval label is "Literature search (Europe PMC, Semantic Scholar)").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class SourceName:
    key: str
    public: str
    description: Optional[str] = None
    agent: Optional[str] = None
    component: Optional[str] = None
    version_label: Optional[str] = None
    aliases: Tuple[str, ...] = ()


_ENTRIES: Tuple[SourceName, ...] = (
    # Phronesis agents.
    SourceName("synapta", "OpenTargets/KEGG", "Target-disease associations and pathways (OpenTargets, KEGG)",
               agent="Synapta", component="Synapta"),
    SourceName("lithoscribe", "Europe PMC", "Literature search (Europe PMC, Semantic Scholar)",
               agent="Lithoscribe", component="Lithoscribe"),
    SourceName("websurfer", "web search", "Web search", agent="WebSurfer", component="WebSurfer"),
    SourceName("vivarix", "COPASI simulation", "Kinetic model simulation (Vivarix, COPASI)",
               agent="Vivarix", component="Vivarix"),
    SourceName("lumicell", "single-cell analysis", "Single-cell analysis of the attached dataset (Lumicell, Scanpy)",
               agent="Lumicell", component="Lumicell"),
    # Evidence sources (EvidenceSource.key) and the agent their evidence is attributed to.
    SourceName("clinical_trials", "ClinicalTrials.gov", "Clinical trials registry (ClinicalTrials.gov)",
               agent="Lithoscribe", version_label="ClinicalTrials.gov data timestamp", aliases=("clinicaltrials",)),
    SourceName("chembl", "ChEMBL", "Drug mechanisms and bioactivity (ChEMBL)", agent="Synapta",
               version_label="ChEMBL release"),
    SourceName("openfda", "openFDA", "Drug safety reports and labels (openFDA)", agent="Lithoscribe",
               version_label="openFDA last updated"),
    SourceName("disease_targets", "OpenTargets", "Disease-to-target ranking (OpenTargets)", agent="Synapta"),
    SourceName("primekg", "PrimeKG", "PrimeKG knowledge-graph analysis", agent="Synapta"),
    SourceName("variant_intel", "ClinVar/CIViC/cBioPortal/gnomAD",
               "Variant evidence (ClinVar, CIViC, cBioPortal, gnomAD)", agent="Synapta", aliases=("variants",)),
    SourceName("structure_intel", "AlphaFold DB / RCSB PDB", agent="Synapta", aliases=("structintel",)),
    SourceName("dgidb", "DGIdb", agent="Synapta"),
    SourceName("pharos", "Pharos", agent="Synapta"),
    SourceName("bindingdb", "BindingDB", agent="Synapta"),
    SourceName("pubtator", "PubTator3/LitVar2", agent="Lithoscribe"),
    SourceName("synlethdb", "SynLethDB", agent="Synapta"),
    SourceName("gwas", "GWAS Catalog", agent="Synapta"),
    SourceName("gtex", "GTEx", agent="Synapta"),
    SourceName("string_ppi", "STRING", agent="Synapta"),
    SourceName("reactome", "Reactome", agent="Synapta"),
    SourceName("pubchem", "PubChem", agent="Synapta"),
    SourceName("depmap", "DepMap", agent="Synapta"),
    SourceName("preprints", "bioRxiv/medRxiv", agent="Lithoscribe"),
    # Variant evidence sub-sources (synapta_variant_intelligence result `sources[].name`).
    SourceName("clinvar", "ClinVar"),
    SourceName("civic", "CIViC"),
    SourceName("cbioportal", "cBioPortal"),
    SourceName("gnomad", "gnomAD"),
    # Upstream databases probed for their release (provenance.source_versions.SOURCE_KEYS).
    SourceName("opentargets", "OpenTargets", version_label="Open Targets data release"),
    SourceName("kegg", "KEGG", version_label="KEGG last update"),
    SourceName("europepmc", "Europe PMC", version_label="EuropePMC API version"),
)

SOURCE_REGISTRY: Dict[str, SourceName] = {e.key: e for e in _ENTRIES}
_ALIASES: Dict[str, str] = {alias: e.key for e in _ENTRIES for alias in e.aliases}

VARIANT_EVIDENCE_SOURCES: Tuple[str, ...] = ("clinvar", "civic", "cbioportal", "gnomad")

# Internal keys that name a pipeline step, not a source a reader can check (a "(tool_status)" citation is dropped).
INTERNAL_ONLY_KEYS: Tuple[str, ...] = (
    "tool_status", "tool_status_note", "evidence_integration_step", "evidence_integration", "biodiscovery",
    "biodiscovery_ranking",
)


def source_entry(key: Optional[str]) -> Optional[SourceName]:
    """The registry entry for `key` or one of its aliases ("structintel", "clinicaltrials", "variants"), else None."""
    if not key:
        return None
    return SOURCE_REGISTRY.get(key) or SOURCE_REGISTRY.get(_ALIASES.get(key, ""))


def public_name(key: Optional[str], default: Optional[str] = None) -> Optional[str]:
    """Short public name of a source key ("chembl" -> "ChEMBL"); `default` when the key is unknown."""
    entry = source_entry(key)
    return entry.public if entry else default


def description(key: Optional[str], default: Optional[str] = None) -> Optional[str]:
    """The approval card's label for a source switch; `default` when the key has none."""
    entry = source_entry(key)
    return entry.description if entry and entry.description else default


def agent_label(key: Optional[str], default: Optional[str] = None) -> Optional[str]:
    """The agent a source's evidence is reported under ("chembl" -> "Synapta"); `default` when unknown."""
    entry = source_entry(key)
    return entry.agent if entry and entry.agent else default


def version_label(key: Optional[str], default: Optional[str] = None) -> Optional[str]:
    """The reproducibility-table label of a probed source's release ("chembl" -> "ChEMBL release")."""
    entry = source_entry(key)
    return entry.version_label if entry and entry.version_label else default


def labels(field: str, keys: Optional[Tuple[str, ...]] = None) -> Dict[str, str]:
    """{key: value of `field`} for the given keys (default: every key that has the field set)."""
    selected = [SOURCE_REGISTRY[k] for k in keys] if keys is not None else list(_ENTRIES)
    return {e.key: getattr(e, field) for e in selected if getattr(e, field)}


__all__ = [
    "INTERNAL_ONLY_KEYS", "SOURCE_REGISTRY", "SourceName", "VARIANT_EVIDENCE_SOURCES", "agent_label", "description",
    "labels", "public_name", "source_entry", "version_label",
]
