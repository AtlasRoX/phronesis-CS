"""
Omics and chemistry evidence sources (feature 20). One module per database, each with an httpx client, a registry
tool (domain "omics") and a Stage 1 EvidenceSource whose artifact is merged under execution_artifacts["omics_intel"]
as {"<source>": {...}}:

- gwas.py      GWAS Catalog REST API v2 (Sollis et al. 2023; Cerezo et al. 2025)    source key "gwas"
- gtex.py      GTEx Portal API v2, median TPM per tissue (GTEx Consortium 2020)     source key "gtex"
- string_db.py STRING 12.5 pinned, interaction partners (Szklarczyk et al. 2023/2025) source key "string_ppi"
- reactome.py  Reactome ContentService/AnalysisService (Milacic et al. 2024)          source key "reactome"
- pubchem.py   PubChem PUG-REST compound properties (Kim et al. 2025)                 source key "pubchem"
- biorxiv.py   bioRxiv/medRxiv recent preprints, labelled not peer reviewed           source key "preprints"
- depmap.py    DepMap CRISPRGeneEffect.csv, local file only (Tsherniak et al. 2017)   source key "depmap"
- bindingdb.py BindingDB affinities by UniProt, drugs matched by structure            source key "bindingdb"
- pubtator.py  PubTator3 entity search + LitVar2 variant summary (extra literature)   source key "pubtator"
- synlethdb.py SynLethDB synthetic-lethal pairs, local bulk file only                 source key "synlethdb"
- dgidb.py     DGIdb 5 GraphQL drug-gene interactions (Cannon et al. 2024)            source key "dgidb"
- pharos.py    Pharos/TCRD GraphQL target development level (Kelleher et al. 2023)    source key "pharos"

DrugBank (Knox et al., NAR 2024, doi:10.1093/nar/gkad976) is not implemented: its data and API need a DrugBank
licence and may not be redistributed, so ChEMBL and PubChem are the default drug sources. A bring-your-own-key
connector would be a separate, opt-in module.

Identifier families returned by allowed_identifiers (merged into the verifier's observed sets): existing families
"rsid", "ontology", "uniprot", "ensembl", "pmids", "dois", plus new keys "gwas_study" (GCST accessions), "reactome"
(R-HSA stable ids), "pubchem_cid", "inchikey", "string" (STRING protein ids) and "depmap_model" (ACH- ids). The new
keys have no pattern in extensions/id_families.py yet, so the verifier records but does not police them.

Routing is deliberately conservative: each source needs its own intent wording in the question plus the entity it
looks up, honours "papers only"/"trials only" restrictions through ctx.gate_decision, and stays out of BioModel
simulation questions unless its database is named.
"""

from __future__ import annotations

from typing import Any, List

from .bindingdb import BindingdbSource
from .biorxiv import PreprintSource
from .common import ARTIFACT_KEY, OmicsSource
from .depmap import DepmapSource
from .dgidb import DgidbSource
from .gtex import GtexSource
from .gwas import GwasSource
from .pharos import PharosSource
from .pubchem import PubchemSource
from .pubtator import PubtatorSource
from .reactome import ReactomeSource
from .string_db import StringSource
from .synlethdb import SynlethdbSource


def all_sources() -> List[OmicsSource]:
    return [GwasSource(), GtexSource(), StringSource(), ReactomeSource(), PubchemSource(), DepmapSource(), PreprintSource(),
            BindingdbSource(), PubtatorSource(), SynlethdbSource(), DgidbSource(), PharosSource()]


def tool_entries() -> List[Any]:
    """(ToolUniverseEntry, executor) pairs for every omics tool."""
    from . import biorxiv, depmap, gtex, gwas, pubchem, reactome, string_db
    from . import bindingdb, pubtator, synlethdb
    from . import dgidb, pharos

    return [
        (gwas.tool_entry(), gwas.gwas_associations),
        (gtex.tool_entry(), gtex.gtex_expression),
        (string_db.tool_entry(), string_db.string_partners),
        (reactome.tool_entry(), reactome.reactome_pathways),
        (pubchem.tool_entry(), pubchem.pubchem_properties),
        (biorxiv.tool_entry(), biorxiv.recent_preprints),
        (depmap.tool_entry(), depmap.depmap_gene_effect),
        (bindingdb.tool_entry(), bindingdb.bindingdb_affinities),
        (pubtator.tool_entry(), pubtator.pubtator_literature),
        (synlethdb.tool_entry(), synlethdb.synlethdb_partners),
        (dgidb.tool_entry(), dgidb.dgidb_interactions),
        (pharos.tool_entry(), pharos.pharos_targets),
    ]


__all__ = [
    "ARTIFACT_KEY",
    "BindingdbSource",
    "DepmapSource",
    "DgidbSource",
    "GtexSource",
    "GwasSource",
    "OmicsSource",
    "PharosSource",
    "PreprintSource",
    "PubchemSource",
    "PubtatorSource",
    "ReactomeSource",
    "StringSource",
    "SynlethdbSource",
    "all_sources",
    "tool_entries",
]
