"""
PrimeKG release download and compilation into a local Parquet store.

Layout under primekg_root() (PHRONESIS_PRIMEKG_DIR, default ~/.cache/phronesis/primekg):

    raw/    nodes.tab  edges.csv  drug_features.tab  disease_features.tab  (Harvard Dataverse originals)
    store/  nodes.parquet  edges.parquet  drug_features.parquet  disease_features.parquet  manifest.json

edges.csv lists every undirected relationship in both directions (8.1M rows); the store keeps each
(relation, display_relation, pair) once, oriented as first seen, so the graph can rebuild both directions.
disease_features.tab has one row per MONDO member of a grouped disease node; the store merges them into
one row per node and keeps the member MONDO ids and names (they double as synonyms for resolution).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

DATAVERSE_DOI = "doi:10.7910/DVN/IXA7BM"
DATAVERSE_VERSION = "2.1"
DATAVERSE_URL = "https://dataverse.harvard.edu/api/access/datafile/{file_id}?format=original"
CITATION = (
    "Chandak P, Huang K, Zitnik M. Building a knowledge graph to enable precision medicine. "
    "Sci Data 10, 67 (2023). doi:10.1038/s41597-023-01960-3"
)
STORE_FORMAT = 1

# name -> (file name, Dataverse file id)
RAW_FILES: Dict[str, tuple] = {
    "nodes": ("nodes.tab", 6180617),
    "edges": ("edges.csv", 6180616),
    "drug_features": ("drug_features.tab", 6180619),
    "disease_features": ("disease_features.tab", 6180618),
}
STORE_FILES = ("nodes.parquet", "edges.parquet", "drug_features.parquet", "disease_features.parquet")
CSR_FILES = ("csr_indptr.npy", "csr_nbr.npy", "csr_rel.npy", "csr_disp.npy")

DISEASE_TEXT_COLUMNS = (
    "mondo_definition", "umls_description", "orphanet_definition", "orphanet_prevalence",
    "orphanet_epidemiology", "orphanet_clinical_description", "orphanet_management_and_treatment",
    "mayo_symptoms", "mayo_causes", "mayo_risk_factors", "mayo_complications", "mayo_prevention", "mayo_see_doc",
)
DRUG_TEXT_COLUMNS = (
    "description", "half_life", "indication", "mechanism_of_action", "protein_binding", "pharmacodynamics",
    "state", "atc_1", "atc_2", "atc_3", "atc_4", "category", "group", "pathway", "molecular_weight", "tpsa", "clogp",
)


def primekg_root() -> Path:
    return Path(os.environ.get("PHRONESIS_PRIMEKG_DIR") or Path.home() / ".cache" / "phronesis" / "primekg")


def raw_dir(root: Optional[Path] = None) -> Path:
    return Path(root or primekg_root()) / "raw"


def store_dir(root: Optional[Path] = None) -> Path:
    return Path(root or primekg_root()) / "store"


def store_available(root: Optional[Path] = None) -> bool:
    d = store_dir(root)
    return (d / "manifest.json").is_file() and all((d / f).is_file() for f in STORE_FILES)


def store_manifest(root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    path = store_dir(root) / "manifest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -----------------------------------------------------------------------------
# Download
# -----------------------------------------------------------------------------

def download_raw(
    root: Optional[Path] = None,
    files: Optional[Iterable[str]] = None,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> Dict[str, Path]:
    """Fetch the Dataverse originals into raw/ (resumable; existing complete files are kept)."""
    import requests

    out_dir = raw_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, Path] = {}
    for key in files or RAW_FILES:
        name, file_id = RAW_FILES[key]
        dest = out_dir / name
        paths[key] = dest
        if dest.is_file() and not force:
            log(f"{name}: present ({dest.stat().st_size:,} bytes)")
            continue
        part = dest.with_suffix(dest.suffix + ".part")
        offset = part.stat().st_size if part.is_file() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        url = DATAVERSE_URL.format(file_id=file_id)
        log(f"{name}: downloading from {url}" + (f" (resuming at {offset:,})" if offset else ""))
        with requests.get(url, stream=True, timeout=300, headers=headers) as resp:
            resp.raise_for_status()
            mode = "ab" if offset and resp.status_code == 206 else "wb"
            with open(part, mode) as fh:
                for chunk in resp.iter_content(1 << 20):
                    fh.write(chunk)
        part.replace(dest)
        log(f"{name}: {dest.stat().st_size:,} bytes")
    return paths


# -----------------------------------------------------------------------------
# Build
# -----------------------------------------------------------------------------

def build_csr(x: np.ndarray, y: np.ndarray, rel: np.ndarray, disp: np.ndarray, n: int) -> Dict[str, np.ndarray]:
    """Symmetric CSR over undirected edges: row u lists (v, relation, display) for every edge u-v, sorted by
    relation then neighbour. A self-loop appears once."""
    x, y = x.astype(np.int32), y.astype(np.int32)
    loop = x == y
    src = np.concatenate([x, y[~loop]])
    dst = np.concatenate([y, x[~loop]])
    rr = np.concatenate([rel, rel[~loop]]).astype(np.int8)
    dd = np.concatenate([disp, disp[~loop]]).astype(np.int8)
    order = np.lexsort((dst, rr, src))
    indptr = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(np.bincount(src, minlength=n), out=indptr[1:])
    return {"indptr": indptr, "nbr": dst[order].astype(np.int32), "rel": rr[order], "disp": dd[order]}


def save_csr(sdir: Path, edges: pa.Table, n: int) -> None:
    csr = build_csr(
        edges.column("x").to_numpy(), edges.column("y").to_numpy(),
        edges.column("relation").combine_chunks().indices.to_numpy(),
        edges.column("display_relation").combine_chunks().indices.to_numpy(), n,
    )
    for key, fname in zip(("indptr", "nbr", "rel", "disp"), CSR_FILES):
        np.save(sdir / fname, csr[key], allow_pickle=False)

def _sniff_delimiter(path: Path) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        header = fh.readline()
    return "\t" if header.count("\t") > header.count(",") else ","


def _read_table(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, sep=_sniff_delimiter(path), low_memory=False, dtype=str, keep_default_na=False,
                       na_values=[""])


def _build_nodes(path: Path) -> pd.DataFrame:
    nodes = _read_table(path)
    nodes = nodes[["node_index", "node_id", "node_type", "node_name", "node_source"]].copy()
    nodes["node_index"] = nodes["node_index"].astype(np.int32)
    nodes = nodes.sort_values("node_index").reset_index(drop=True)
    if not (nodes["node_index"].to_numpy() == np.arange(len(nodes))).all():
        raise ValueError("PrimeKG nodes: node_index is not a dense 0..N-1 range")
    for col in ("node_id", "node_name"):
        nodes[col] = nodes[col].fillna("").astype(str)
    return nodes


def _build_edges(path: Path, n_nodes: int) -> tuple[pa.Table, Dict[str, int]]:
    """Stream edges.csv, keep one row per undirected (relation, display, pair)."""
    reader = pacsv.open_csv(
        str(path),
        read_options=pacsv.ReadOptions(block_size=1 << 26),
        convert_options=pacsv.ConvertOptions(
            column_types={"relation": pa.string(), "display_relation": pa.string(),
                          "x_index": pa.int32(), "y_index": pa.int32()},
            include_columns=["relation", "display_relation", "x_index", "y_index"],
        ),
    )
    relations: Dict[str, int] = {}
    displays: Dict[str, int] = {}
    xs, ys, rs, ds = [], [], [], []
    raw_rows = 0
    for batch in reader:
        raw_rows += batch.num_rows
        rel = batch.column("relation").dictionary_encode()
        disp = batch.column("display_relation").dictionary_encode()
        rel_map = np.array([relations.setdefault(str(v), len(relations)) for v in rel.dictionary.to_pylist()],
                           dtype=np.int16)
        disp_map = np.array([displays.setdefault(str(v), len(displays)) for v in disp.dictionary.to_pylist()],
                            dtype=np.int16)
        rs.append(rel_map[rel.indices.to_numpy(zero_copy_only=False)])
        ds.append(disp_map[disp.indices.to_numpy(zero_copy_only=False)])
        xs.append(batch.column("x_index").to_numpy(zero_copy_only=False))
        ys.append(batch.column("y_index").to_numpy(zero_copy_only=False))
    x = np.concatenate(xs).astype(np.int64)
    y = np.concatenate(ys).astype(np.int64)
    r = np.concatenate(rs).astype(np.int64)
    d = np.concatenate(ds).astype(np.int64)
    if x.size and (x.min() < 0 or y.min() < 0 or max(x.max(), y.max()) >= n_nodes):
        raise ValueError("PrimeKG edges reference node indices outside the node table")
    # Undirected key: (display, relation, min, max). Keep the first orientation seen.
    lo, hi = np.minimum(x, y), np.maximum(x, y)
    key = ((d * 64 + r) * n_nodes + lo) * n_nodes + hi
    _, first = np.unique(key, return_index=True)
    first.sort()
    rel_names = sorted(relations, key=relations.get)
    disp_names = sorted(displays, key=displays.get)
    table = pa.table({
        "x": pa.array(x[first].astype(np.int32)),
        "y": pa.array(y[first].astype(np.int32)),
        "relation": pa.DictionaryArray.from_arrays(pa.array(r[first].astype(np.int8)), pa.array(rel_names)),
        "display_relation": pa.DictionaryArray.from_arrays(pa.array(d[first].astype(np.int8)), pa.array(disp_names)),
    })
    return table, {"raw_rows": int(raw_rows), "undirected_edges": int(first.size)}


def _mondo_curie(value: Any) -> Optional[str]:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return f"MONDO_{int(float(s)):07d}"
    except ValueError:
        return None


def _build_disease_features(path: Path, nodes: pd.DataFrame) -> pd.DataFrame:
    df = _read_table(path)
    df["node_index"] = df["node_index"].astype(np.int32)
    names = nodes.set_index("node_index")["node_name"].str.lower()
    df["_is_rep"] = df["mondo_name"].fillna("").str.lower().to_numpy() == names.reindex(df["node_index"]).to_numpy()
    df = df.sort_values(["node_index", "_is_rep"], ascending=[True, False], kind="stable")
    grouped = df.groupby("node_index", sort=True)
    text = grouped[list(DISEASE_TEXT_COLUMNS)].first()  # first non-null per column, representative row first
    members = grouped.apply(
        lambda g: pd.Series({
            "mondo_ids": "|".join(dict.fromkeys(c for c in map(_mondo_curie, g["mondo_id"]) if c)),
            "synonyms": "|".join(dict.fromkeys(str(n).strip() for n in g["mondo_name"].dropna() if str(n).strip())),
            "group_name_bert": next((str(v) for v in g["group_name_bert"].dropna()), None),
        }),
        include_groups=False,
    )
    out = members.join(text).reset_index()
    return out


def _build_drug_features(path: Path) -> pd.DataFrame:
    df = _read_table(path)
    df["node_index"] = df["node_index"].astype(np.int32)
    keep = ["node_index", *[c for c in DRUG_TEXT_COLUMNS if c in df.columns]]
    return df[keep].drop_duplicates("node_index").sort_values("node_index").reset_index(drop=True)


def build_store(
    root: Optional[Path] = None,
    download: bool = True,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Compile raw/ into store/ (downloading missing raw files first when download=True)."""
    root = Path(root or primekg_root())
    rdir, sdir = raw_dir(root), store_dir(root)
    if download:
        download_raw(root, log=log)
    missing = [RAW_FILES[k][0] for k in RAW_FILES if not (rdir / RAW_FILES[k][0]).is_file()]
    if missing:
        raise FileNotFoundError(f"PrimeKG raw files missing in {rdir}: {', '.join(missing)}")
    sdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    log("nodes ...")
    nodes = _build_nodes(rdir / RAW_FILES["nodes"][0])
    pq.write_table(pa.Table.from_pandas(nodes, preserve_index=False), sdir / "nodes.parquet", compression="zstd")

    log("edges (streaming) ...")
    edges, edge_counts = _build_edges(rdir / RAW_FILES["edges"][0], len(nodes))
    pq.write_table(edges, sdir / "edges.parquet", compression="zstd")
    save_csr(sdir, edges, len(nodes))

    log("drug features ...")
    drugs = _build_drug_features(rdir / RAW_FILES["drug_features"][0])
    pq.write_table(pa.Table.from_pandas(drugs, preserve_index=False), sdir / "drug_features.parquet",
                   compression="zstd")

    log("disease features ...")
    diseases = _build_disease_features(rdir / RAW_FILES["disease_features"][0], nodes)
    pq.write_table(pa.Table.from_pandas(diseases, preserve_index=False), sdir / "disease_features.parquet",
                   compression="zstd")

    rel_counts = edges.group_by(["relation", "display_relation"]).aggregate([("x", "count")]).to_pylist()
    manifest = {
        "store_format": STORE_FORMAT,
        "source": "PrimeKG",
        "dataverse_doi": DATAVERSE_DOI,
        "dataverse_version": DATAVERSE_VERSION,
        "citation": CITATION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "build_seconds": round(time.time() - t0, 1),
        "raw_files": {
            RAW_FILES[k][0]: {"dataverse_file_id": RAW_FILES[k][1], "bytes": (rdir / RAW_FILES[k][0]).stat().st_size,
                              "sha256": _sha256(rdir / RAW_FILES[k][0])}
            for k in RAW_FILES
        },
        "counts": {
            "nodes": int(len(nodes)),
            "edges_raw_rows": edge_counts["raw_rows"],
            "edges_undirected": edge_counts["undirected_edges"],
            "drug_features": int(len(drugs)),
            "disease_features": int(len(diseases)),
            "node_types": {str(k): int(v) for k, v in nodes["node_type"].value_counts().sort_index().items()},
            "relations": {f"{r['relation']}/{r['display_relation']}": int(r["x_count"])
                          for r in sorted(rel_counts, key=lambda r: (r["relation"], r["display_relation"]))},
        },
    }
    (sdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"PrimeKG store ready in {sdir} ({manifest['build_seconds']} s)")
    return manifest
