"""
Structure and chemistry intelligence (feature 12): AlphaFold DB models with pLDDT confidence, experimental RCSB PDB
entries, and opt-in NVIDIA NIM DiffDock docking and MolMIM generation.

References: AlphaFold (Jumper et al., Nature 2021, doi:10.1038/s41586-021-03819-2); AlphaFold DB (Varadi et al.,
NAR 2022, doi:10.1093/nar/gkab1061; NAR 2024, doi:10.1093/nar/gkad1011); RCSB PDB (Burley et al., NAR 2023,
doi:10.1093/nar/gkac1077); DiffDock (Corso et al., ICLR 2023, arXiv:2210.01776); MolMIM (Reidenbach et al. 2022,
arXiv:2208.09016).
"""

from .clients import StructureClient, StructureClientError, plddt_band
from .source import StructureIntelSource, has_structure_intent

__all__ = ["StructureClient", "StructureClientError", "StructureIntelSource", "has_structure_intent", "plddt_band"]
