"""
Cell Marker Database Access Layer for Phronesis Lumicell.
Provides high-performance, offline querying across:
  - CellMarker 2.0 (60,877 human records across 1,715 cell types & 124 tissue classes)
  - PanglaoDB (7,851 human records with specificity & sensitivity metrics)
  - Consensus Atlas (581 integrated cell types & 3,951 marker associations)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class CellMarkerRecord:
    id: int
    species: str
    tissue_class: str
    tissue_type: str
    cancer_type: str
    cell_name: str
    symbol: str
    gene_name: str
    cell_ontology_id: str
    pmid: str
    journal: str
    year: float


@dataclass(frozen=True)
class PanglaoRecord:
    id: int
    symbol: str
    cell_type: str
    organ: str
    canonical_marker: bool
    specificity_human: float
    sensitivity_human: float


class CellMarkerDatabase:
    """Offline relational interface for official CellMarker 2.0 and PanglaoDB databases."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path(__file__).parent / "cell_markers.db"
        self.db_path = db_path
        self._atlas_cache: Optional[Dict[str, List[str]]] = None

    def _get_connection(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FileNotFoundError(f"Cell marker database not found at {self.db_path}")
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def get_consensus_atlas(self) -> Dict[str, List[str]]:
        """Return the pre-computed consensus dictionary of cell types to marker gene lists."""
        if self._atlas_cache is None:
            json_path = self.db_path.parent / "cell_markers.json"
            if not json_path.exists():
                return {}
            with open(json_path, "r", encoding="utf-8") as f:
                self._atlas_cache = json.load(f)
        return self._atlas_cache

    def search_cell_types(self, query: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Search across both CellMarker and PanglaoDB for matching cell types."""
        conn = self._get_connection()
        cur = conn.cursor()
        param = f"%{query}%"

        cur.execute("""
        SELECT DISTINCT cell_name, tissue_type, cancer_type, COUNT(DISTINCT symbol) as marker_count
        FROM cellmarker_human
        WHERE cell_name LIKE ? OR tissue_type LIKE ?
        GROUP BY cell_name, tissue_type, cancer_type
        ORDER BY marker_count DESC
        LIMIT ?;
        """, (param, param, limit))
        results = [dict(row) for row in cur.fetchall()]
        conn.close()
        return results

    def get_markers_for_cell_type(
        self, cell_type: str, tissue: Optional[str] = None, normal_only: bool = True, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Retrieve marker genes for a cell type with citation support."""
        conn = self._get_connection()
        cur = conn.cursor()

        query = """
        SELECT symbol, gene_name, COUNT(DISTINCT pmid) as citation_count,
               GROUP_CONCAT(DISTINCT pmid) as pmids, tissue_type, cancer_type
        FROM cellmarker_human
        WHERE cell_name LIKE ?
        """
        params: List[Any] = [f"%{cell_type}%"]

        if normal_only:
            query += " AND (cancer_type = 'Normal' OR cancer_type IS NULL OR cancer_type = '')"
        if tissue:
            query += " AND (tissue_type LIKE ? OR tissue_class LIKE ?)"
            params.extend([f"%{tissue}%", f"%{tissue}%"])

        query += " GROUP BY symbol ORDER BY citation_count DESC LIMIT ?;"
        params.append(limit)

        cur.execute(query, params)
        results = [dict(row) for row in cur.fetchall()]
        conn.close()
        return results

    def get_cell_types_for_gene(self, symbol: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Identify which human cell types canonically express a specific marker gene."""
        conn = self._get_connection()
        cur = conn.cursor()
        sym = symbol.strip().upper()

        cur.execute("""
        SELECT cell_name, tissue_type, cancer_type, COUNT(DISTINCT pmid) as citations
        FROM cellmarker_human
        WHERE symbol = ?
        GROUP BY cell_name, tissue_type, cancer_type
        ORDER BY citations DESC
        LIMIT ?;
        """, (sym, limit))
        results = [dict(row) for row in cur.fetchall()]
        conn.close()
        return results

    def get_statistics(self) -> Dict[str, Any]:
        """Return comprehensive counts and metrics for the offline marker database."""
        conn = self._get_connection()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM cellmarker_human;")
        total_cm_records = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT cell_name) FROM cellmarker_human;")
        unique_cm_cells = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT symbol) FROM cellmarker_human;")
        unique_cm_symbols = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT tissue_class) FROM cellmarker_human;")
        unique_cm_tissues = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM panglaodb_human;")
        total_pg_records = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT cell_type) FROM panglaodb_human;")
        unique_pg_cells = cur.fetchone()[0]

        conn.close()
        atlas = self.get_consensus_atlas()

        return {
            "cellmarker_records": total_cm_records,
            "cellmarker_unique_cell_types": unique_cm_cells,
            "cellmarker_unique_genes": unique_cm_symbols,
            "cellmarker_tissue_classes": unique_cm_tissues,
            "panglaodb_records": total_pg_records,
            "panglaodb_unique_cell_types": unique_pg_cells,
            "consensus_atlas_cell_types": len(atlas),
            "consensus_atlas_associations": sum(len(v) for v in atlas.values()),
            "database_size_mb": round(self.db_path.stat().st_size / (1024 * 1024), 2)
            if self.db_path.exists()
            else 0.0,
        }
