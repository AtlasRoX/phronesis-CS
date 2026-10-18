"""
Extension module owned by WP5 (feature 12, structure and chemistry intelligence). Registers the 'structure_intel'
EvidenceSource and its registry tools (domain "structure"):

- structure_alphafold_prediction: AlphaFold DB model and mean pLDDT for a UniProt accession
  (doi:10.1093/nar/gkab1061, doi:10.1093/nar/gkad1011; pLDDT bands from doi:10.1038/s41586-021-03819-2)
- structure_pdb_search: experimental RCSB PDB entries for a UniProt accession (doi:10.1093/nar/gkac1077)
- structure_diffdock and structure_molmim_generate: NVIDIA NIM DiffDock (arXiv:2210.01776) and MolMIM
  (arXiv:2208.09016), registered only when NVIDIA_API_KEY is set; the source calls them only on an explicit
  docking or generation request.

No synthesis-prompt rule is registered (the composed prompt has a fixed size budget): the grounding guidance
travels with the evidence instead, as note attributes on <structure_intel> and as scope notes.
"""

from __future__ import annotations

from typing import Any

from phronesis.sources import register_source
from phronesis.structintel.source import StructureIntelSource

register_source(StructureIntelSource())


def register_tools(registry: Any) -> None:
    from phronesis.structintel import source as s
    from phronesis.structintel.clients import nvidia_api_key
    from phronesis.tools.registry import AdapterType, ToolUniverseEntry

    registry.register(
        ToolUniverseEntry(
            name=s.AF_TOOL,
            description="AlphaFold DB predicted structure for a UniProt accession: model id, version, mean pLDDT with "
            "its confidence band, and model file URLs. A prediction, not an experimental structure.",
            domain="structure",
            adapter_type=AdapterType.DIRECT_REST,
            input_schema=s.AlphaFoldInput,
            canonical_id_types=["uniprot_id", "alphafold_model_id"],
            timeout_seconds=25.0,
            tags=["structure", "alphafold", "plddt", "protein", "prediction"],
            is_async=True,
        ),
        executor=s.alphafold_prediction,
    )
    registry.register(
        ToolUniverseEntry(
            name=s.PDB_TOOL,
            description="Experimental RCSB PDB entries mapped to a UniProt accession: PDB id, title, method, resolution "
            "and bound ligands.",
            domain="structure",
            adapter_type=AdapterType.DIRECT_REST,
            input_schema=s.PdbSearchInput,
            canonical_id_types=["uniprot_id", "pdb_id"],
            timeout_seconds=30.0,
            tags=["structure", "pdb", "rcsb", "crystal", "cryo-em", "ligand", "binding site"],
            is_async=True,
        ),
        executor=s.pdb_search,
    )
    if nvidia_api_key():
        registry.register(
            ToolUniverseEntry(
                name=s.DOCK_TOOL,
                description="NVIDIA NIM DiffDock: predicted binding poses and confidence for a SMILES ligand against an "
                "AlphaFold model or PDB entry. Use only when docking is explicitly requested; not an affinity.",
                domain="structure",
                adapter_type=AdapterType.DIRECT_REST,
                input_schema=s.DiffDockInput,
                canonical_id_types=["uniprot_id", "pdb_id"],
                timeout_seconds=240.0,
                tags=["docking", "diffdock", "ligand", "pose", "nim"],
                is_async=True,
            ),
            executor=s.diffdock_pose,
        )
        registry.register(
            ToolUniverseEntry(
                name=s.MOLMIM_TOOL,
                description="NVIDIA NIM MolMIM: generate analogue molecules of a seed SMILES optimised for QED or plogP. "
                "Use only when molecule generation is explicitly requested; outputs are untested candidates.",
                domain="structure",
                adapter_type=AdapterType.DIRECT_REST,
                input_schema=s.MolMimInput,
                canonical_id_types=[],
                timeout_seconds=150.0,
                tags=["chemistry", "molmim", "generation", "analogs", "nim"],
                is_async=True,
            ),
            executor=s.molmim_generate,
        )
