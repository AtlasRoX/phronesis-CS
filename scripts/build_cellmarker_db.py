"""
Build comprehensive, standardized CellMarker 2.0 & PanglaoDB database and consensus atlas.
Ingests:
  - data/Cell_marker_Human.xlsx (60,877 records, CellMarker 2.0)
  - data/panglaodb_markers.tsv (7,851 human records, PanglaoDB)
Produces:
  - phronesis/lumicell/markers/cell_markers.db (Indexed SQLite relational store)
  - phronesis/lumicell/markers/cell_markers.json (Standardized, deduplicated, HGNC-verified atlas)
"""

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
import pandas as pd

CM_FILE = Path("data/Cell_marker_Human.xlsx")
PG_FILE = Path("data/panglaodb_markers.tsv")
DB_OUT = Path("phronesis/lumicell/markers/cell_markers.db")
JSON_OUT = Path("phronesis/lumicell/markers/cell_markers.json")

# Flow cytometry carbohydrate / glycolipid / non-gene antibody antigens (not mRNA genes)
FLOW_ANTIGENS = {
    "STRO-1", "SSEA-3", "SSEA-4", "TRA-1-60", "TRA-1-81", "PSA-NCAM"
}

# Protein / historical aliases mapped to official HGNC gene symbols
ALIAS_MAP = {
    "BRACHYURY": "TBXT",
    "ALBUMIN": "ALB",
    "KERATIN-5": "KRT5",
    "CYTOKERATIN-18": "KRT18",
    "CYTOKERATIN-7": "KRT7",
    "E-CADHERIN": "CDH1",
    "N-CADHERIN": "CDH2",
    "VE-CADHERIN": "CDH5",
    "VEGFR-2": "KDR",
    "ALPHA-SMA": "ACTA2",
    "PDGFR-BETA": "PDGFRB",
    "IFN-GAMMA": "IFNG",
    "IL-17RB": "IL17RB",
    "SIGLEC-8": "SIGLEC8",
    "HLA-DR": "HLA-DRA",
}

# Mapping of raw / informal names to canonical structured cell types
CANONICAL_NAME_MAP = {
    "b cells naive": "B Cells (Naive)",
    "b cells memory": "B Cells (Memory)",
    "cd8+ t cell": "T Cells (CD8+ Cytotoxic Effector)",
    "cd8 t cell": "T Cells (CD8+ Cytotoxic Effector)",
    "ciliated cell": "Airway Ciliated Epithelium",
    "ciliated cells": "Airway Ciliated Epithelium",
    "ductal cell": "Pancreatic Duct Cells",
    "ductal cells": "Pancreatic Duct Cells",
    "enteroendocrine cell": "Enteroendocrine Cells",
    "epiblast cell": "Epiblast cells",
    "acinar cell": "Pancreatic Acinar Cells (Digestive)",
    "acinar cells": "Pancreatic Acinar Cells (Digestive)",
    "adipocyte": "White Adipocytes",
    "adipocytes": "White Adipocytes",
    "hepatocyte": "Hepatocytes (Parenchymal)",
    "hepatocytes": "Hepatocytes (Parenchymal)",
    "microglial cell": "Microglia (Brain Myeloid)",
    "microglial cells": "Microglia (Brain Myeloid)",
    "podocyte": "Kidney Glomerular Podocytes",
    "podocytes": "Kidney Glomerular Podocytes",
    "müller cells": "Retinal Muller Glia",
    "muller cells": "Retinal Muller Glia",
    "alpha cell (α cell)": "Pancreatic Alpha Cells (Glucagon)",
    "alpha cell": "Pancreatic Alpha Cells (Glucagon)",
    "alpha cells": "Pancreatic Alpha Cells (Glucagon)",
    "beta cell(β cell)": "Pancreatic Beta Cells (Insulin)",
    "beta cell": "Pancreatic Beta Cells (Insulin)",
    "beta cells": "Pancreatic Beta Cells (Insulin)",
    "gamma delta(γδ) t cell": "Gamma-Delta T Cells (gdT)",
    "gamma-delta t cell": "Gamma-Delta T Cells (gdT)",
    "gamma delta t cell": "Gamma-Delta T Cells (gdT)",
    "endothelial cell": "Endothelial Cells (Capillary)",
    "fibroblast": "Interstitial Fibroblasts",
    "macrophage": "Macrophages (Tissue-Resident General)",
    "monocyte": "Monocytes (CD14+ Classical)",
    "natural killer cell": "Natural Killer Cells (CD56dim Cytotoxic)",
    "t cell": "T Cells (CD4+ Naive)",
    "plasma cell": "Plasma Cells / Plasmablasts",
    "plasma cells": "Plasma Cells / Plasmablasts",
    "pericyte": "Vascular Pericytes",
    "pericytes": "Vascular Pericytes",
    "smooth muscle cell": "Vascular Smooth Muscle Cells (VSMC)",
    "smooth muscle cells": "Vascular Smooth Muscle Cells (VSMC)",
}


def clean_gene_symbol(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.strip().upper()
    s = re.sub(r"\[.*?\]|\(.*?\)", "", s).strip()
    if s in ["", "NAN", "NONE", "NULL", "UNKNOWN", "NA"]:
        return ""
    if s in ALIAS_MAP:
        s = ALIAS_MAP[s]
    if s in FLOW_ANTIGENS:
        return ""
    if re.match(r"^[A-Z0-9\-_]+$", s):
        return s
    return ""


def clean_cell_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    name = name.strip()
    name = re.sub(r"\s+", " ", name)
    return name


def normalize_cell_key(key: str) -> str:
    cleaned = clean_cell_name(key)
    lower = cleaned.lower()
    if lower in CANONICAL_NAME_MAP:
        return CANONICAL_NAME_MAP[lower]
    ascii_clean = cleaned.encode("ascii", "ignore").decode("ascii").strip()
    return ascii_clean if ascii_clean else cleaned


def key_signature(k: str) -> str:
    s = k.lower()
    s = re.sub(r"\bcells\b", "cell", s)
    s = re.sub(r"\bneurons\b", "neuron", s)
    return re.sub(r"[^a-z0-9]", "", s)


def build_database():
    print("[1/4] Loading raw data files...")
    df_cm = pd.read_excel(CM_FILE)
    df_pg = pd.read_csv(PG_FILE, sep="\t")

    df_pg_hs = df_pg[df_pg["species"].astype(str).str.contains("Hs")].copy()

    print("[2/4] Initializing SQLite database...")
    DB_OUT.parent.mkdir(parents=True, exist_ok=True)
    if DB_OUT.exists():
        DB_OUT.unlink()

    conn = sqlite3.connect(str(DB_OUT))
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE cellmarker_human (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        species TEXT,
        tissue_class TEXT,
        tissue_type TEXT,
        uberon_id TEXT,
        cancer_type TEXT,
        cell_type TEXT,
        cell_name TEXT,
        cell_ontology_id TEXT,
        marker TEXT,
        symbol TEXT,
        gene_id TEXT,
        gene_type TEXT,
        gene_name TEXT,
        uniprot_id TEXT,
        technology_seq TEXT,
        marker_source TEXT,
        pmid TEXT,
        title TEXT,
        journal TEXT,
        year REAL
    );
    """)

    cm_records = []
    for _, row in df_cm.iterrows():
        symbol = clean_gene_symbol(str(row.get("Symbol", "")))
        if not symbol:
            symbol = clean_gene_symbol(str(row.get("marker", "")))
        cell_name = clean_cell_name(str(row.get("cell_name", "")))
        if not cell_name or not symbol:
            continue

        cm_records.append((
            str(row.get("species", "Human")),
            clean_cell_name(str(row.get("tissue_class", ""))),
            clean_cell_name(str(row.get("tissue_type", ""))),
            str(row.get("uberonongology_id", "") or ""),
            clean_cell_name(str(row.get("cancer_type", "Normal") or "Normal")),
            clean_cell_name(str(row.get("cell_type", "") or "")),
            cell_name,
            str(row.get("cellontology_id", "") or ""),
            str(row.get("marker", "") or ""),
            symbol,
            str(row.get("GeneID", "") or ""),
            str(row.get("Genetype", "") or ""),
            str(row.get("Genename", "") or ""),
            str(row.get("UNIPROTID", "") or ""),
            str(row.get("technology_seq", "") or ""),
            str(row.get("marker_source", "") or ""),
            str(row.get("PMID", "") or ""),
            str(row.get("Title", "") or ""),
            str(row.get("journal", "") or ""),
            float(row.get("year", 0.0) or 0.0) if pd.notna(row.get("year")) else 0.0
        ))

    cur.executemany("""
    INSERT INTO cellmarker_human (
        species, tissue_class, tissue_type, uberon_id, cancer_type, cell_type, cell_name,
        cell_ontology_id, marker, symbol, gene_id, gene_type, gene_name, uniprot_id,
        technology_seq, marker_source, pmid, title, journal, year
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, cm_records)

    cur.execute("CREATE INDEX idx_cm_cell_name ON cellmarker_human(cell_name);")
    cur.execute("CREATE INDEX idx_cm_symbol ON cellmarker_human(symbol);")
    cur.execute("CREATE INDEX idx_cm_tissue_type ON cellmarker_human(tissue_type);")
    cur.execute("CREATE INDEX idx_cm_cancer_type ON cellmarker_human(cancer_type);")

    cur.execute("""
    CREATE TABLE panglaodb_human (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        species TEXT,
        symbol TEXT,
        cell_type TEXT,
        nicknames TEXT,
        ubiquitousness_index REAL,
        product_description TEXT,
        gene_type TEXT,
        canonical_marker INTEGER,
        germ_layer TEXT,
        organ TEXT,
        sensitivity_human REAL,
        specificity_human REAL
    );
    """)

    pg_records = []
    for _, row in df_pg_hs.iterrows():
        symbol = clean_gene_symbol(str(row.get("official gene symbol", "")))
        cell_type = clean_cell_name(str(row.get("cell type", "")))
        if not symbol or not cell_type:
            continue

        pg_records.append((
            str(row.get("species", "")),
            symbol,
            cell_type,
            str(row.get("nicknames", "") or ""),
            float(row.get("ubiquitousness index", 0.0) or 0.0) if pd.notna(row.get("ubiquitousness index")) else 0.0,
            str(row.get("product description", "") or ""),
            str(row.get("gene type", "") or ""),
            int(row.get("canonical marker", 0) or 0) if pd.notna(row.get("canonical marker")) else 0,
            str(row.get("germ layer", "") or ""),
            str(row.get("organ", "") or ""),
            float(row.get("sensitivity_human", 0.0) or 0.0) if pd.notna(row.get("sensitivity_human")) else 0.0,
            float(row.get("specificity_human", 0.0) or 0.0) if pd.notna(row.get("specificity_human")) else 0.0,
        ))

    cur.executemany("""
    INSERT INTO panglaodb_human (
        species, symbol, cell_type, nicknames, ubiquitousness_index, product_description,
        gene_type, canonical_marker, germ_layer, organ, sensitivity_human, specificity_human
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, pg_records)

    cur.execute("CREATE INDEX idx_pg_cell_type ON panglaodb_human(cell_type);")
    cur.execute("CREATE INDEX idx_pg_symbol ON panglaodb_human(symbol);")
    cur.execute("CREATE INDEX idx_pg_organ ON panglaodb_human(organ);")

    conn.commit()
    print(f"  Inserted {len(cm_records)} CellMarker rows and {len(pg_records)} PanglaoDB rows into SQLite.")

    # [3/4] Build standardized consensus atlas for cell_markers.json
    print("[3/4] Building standardized consensus cell marker dictionary...")

    raw_atlas = {}
    if JSON_OUT.exists():
        with open(JSON_OUT, "r", encoding="utf-8") as f:
            raw_atlas = json.load(f)

    merged_atlas = defaultdict(list)
    for raw_k, genes in raw_atlas.items():
        norm_k = normalize_cell_key(raw_k)
        clean_genes = [clean_gene_symbol(g) for g in genes]
        clean_genes = [g for g in clean_genes if g]
        merged_atlas[norm_k].extend(clean_genes)

    cur.execute("""
    SELECT cell_type, symbol, canonical_marker, specificity_human, organ
    FROM panglaodb_human
    ORDER BY cell_type, canonical_marker DESC, specificity_human DESC;
    """)
    pg_cell_markers = defaultdict(list)
    for c_type, sym, canon, spec, organ in cur.fetchall():
        clean_sym = clean_gene_symbol(sym)
        if clean_sym and clean_sym not in pg_cell_markers[c_type]:
            pg_cell_markers[c_type].append(clean_sym)

    cur.execute("""
    SELECT cell_name, symbol, COUNT(DISTINCT pmid) as pmid_count, tissue_type
    FROM cellmarker_human
    WHERE cancer_type = 'Normal' OR cancer_type IS NULL OR cancer_type = ''
    GROUP BY cell_name, symbol
    HAVING pmid_count >= 2
    ORDER BY cell_name, pmid_count DESC;
    """)
    cm_cell_markers = defaultdict(list)
    for c_name, sym, p_count, tissue in cur.fetchall():
        clean_sym = clean_gene_symbol(sym)
        if clean_sym and clean_sym not in cm_cell_markers[c_name]:
            cm_cell_markers[c_name].append(clean_sym)

    for c_type, genes in pg_cell_markers.items():
        target_k = normalize_cell_key(c_type)
        top_genes = genes[:8]
        if len(top_genes) < 2:
            continue
        merged_atlas[target_k].extend(top_genes)

    for c_name, genes in cm_cell_markers.items():
        clean_name = clean_cell_name(c_name)
        if clean_name.lower() in ["cell", "cancer stem cell", "stem cell", "unknown", "other"]:
            continue
        target_k = normalize_cell_key(clean_name)
        top_genes = genes[:8]
        if len(top_genes) < 3:
            continue
        merged_atlas[target_k].extend(top_genes)

    # Sort keys so that more descriptive / Title Cased keys (containing parentheses or capital letters) come first
    sorted_keys = sorted(merged_atlas.keys(), key=lambda x: (
        "(" in x,
        sum(1 for c in x if c.isupper()),
        len(x)
    ), reverse=True)

    unified_atlas = {}
    key_map = {}
    for k in sorted_keys:
        sig = key_signature(k)
        unique_genes = list(dict.fromkeys(merged_atlas[k]))
        if len(unique_genes) < 2:
            continue
        if sig in key_map:
            target_k = key_map[sig]
            unified_atlas[target_k] = list(dict.fromkeys(unified_atlas[target_k] + unique_genes))
        else:
            key_map[sig] = k
            unified_atlas[k] = unique_genes

    final_atlas = {k: unified_atlas[k] for k in sorted(unified_atlas.keys())}

    print("[4/4] Saving standardized JSON atlas...")
    with open(JSON_OUT, "w", encoding="utf-8") as f:
        json.dump(final_atlas, f, indent=2, ensure_ascii=False)
        f.write("\n")

    conn.close()

    total_types = len(final_atlas)
    total_markers = sum(len(v) for v in final_atlas.values())
    unique_genes = len(set(g for v in final_atlas.values() for g in v))
    db_size_mb = DB_OUT.stat().st_size / (1024 * 1024)

    print("\n" + "=" * 60)
    print("STANDARDIZED CELL MARKER DATABASE GENERATION COMPLETE")
    print("=" * 60)
    print(f"SQLite Database:     {DB_OUT} ({db_size_mb:.2f} MB)")
    print(f"JSON Atlas File:     {JSON_OUT}")
    print(f"Total Cell Types:    {total_types}")
    print(f"Total Associations:  {total_markers}")
    print(f"Unique Gene Symbols: {unique_genes}")
    print("=" * 60)


if __name__ == "__main__":
    build_database()
