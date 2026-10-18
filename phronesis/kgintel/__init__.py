"""
Knowledge-graph intelligence over the full PrimeKG (Chandak, Huang & Zitnik, Sci Data 2023).

store    downloads the Harvard Dataverse release and compiles it into a compact Parquet store
graph    loads the store into an in-memory typed CSR graph with entity resolution
analytics  drug / disease / gene profiles, mechanism paths, network-proximity repurposing,
           phenotype-driven differential diagnosis, polypharmacy checks, set enrichment, similarity
"""

from .graph import PrimeKG, PrimeKGError, get_primekg, reset_primekg
from .store import build_store, download_raw, primekg_root, store_available, store_manifest

__all__ = [
    "PrimeKG",
    "PrimeKGError",
    "build_store",
    "download_raw",
    "get_primekg",
    "primekg_root",
    "reset_primekg",
    "store_available",
    "store_manifest",
]
