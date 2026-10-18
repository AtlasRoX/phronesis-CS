"""
In-memory PrimeKG: a symmetric CSR adjacency over every typed edge, node metadata, clinical feature text,
and an entity resolver (ids, names, MONDO member names, gene symbols; token-indexed fuzzy fallback).

Load once per process with get_primekg(); every query afterwards is pure numpy/scipy over ~8.1M adjacency
entries. All node references are node_index ints (dense 0..N-1, as in the release).
"""

from __future__ import annotations

import difflib
import re
import threading
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import scipy.sparse as sp

from .aliases import ALIASES
from .store import (
    CITATION,
    CSR_FILES,
    DATAVERSE_DOI,
    build_csr,
    primekg_root,
    store_available,
    store_dir,
    store_manifest,
)

NODE_TYPES = (
    "gene/protein", "drug", "disease", "effect/phenotype", "biological_process", "molecular_function",
    "cellular_component", "pathway", "anatomy", "exposure",
)
TYPE_ALIASES = {
    "gene": "gene/protein", "protein": "gene/protein", "target": "gene/protein", "phenotype": "effect/phenotype",
    "symptom": "effect/phenotype", "side_effect": "effect/phenotype", "effect": "effect/phenotype",
    "bp": "biological_process", "mf": "molecular_function", "cc": "cellular_component", "tissue": "anatomy",
}
# Relations that are too dense to be informative as path steps (2.7M drug-drug interaction pairs make every
# two drugs look "connected"); path search skips them unless explicitly allowed.
DENSE_RELATIONS = frozenset({"drug_drug"})


class PrimeKGError(Exception):
    """kind: not_built | not_found | invalid."""

    def __init__(self, message: str, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class Match:
    index: int
    score: float
    matched_on: str  # id | name | synonym | symbol | normalized | fuzzy


_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^0-9a-z]+")
_QUALIFIER = re.compile(r"\s*(?:\((?:disease|disorder)\)|\bdisorder)$", re.IGNORECASE)
_POSSESSIVE = re.compile(r"'s\b")
_PLURAL = re.compile(r"(?<=[a-z]{3})(?<!s)s\b")
# Salt and formulation words dropped when a drug name does not match as written ("imatinib mesylate").
SALT_WORDS = frozenset(
    "hydrochloride hcl dihydrochloride hydrobromide mesylate besylate maleate fumarate succinate tartrate citrate "
    "acetate phosphate sulfate sulphate sodium potassium calcium magnesium bromide chloride monohydrate dihydrate "
    "trihydrate hydrate anhydrous tosylate lactate gluconate disodium dipropionate propionate valerate".split()
)


def normalize_name(text: str) -> str:
    """Lower-case ASCII words with possessives and simple plurals folded ("Crohn's diseases" -> "crohn disease")."""
    s = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    s = _POSSESSIVE.sub("", s)
    s = _WS.sub(" ", _NON_WORD.sub(" ", s)).strip()
    return _PLURAL.sub("", s)


def normalize_series(names: "pd.Series") -> "pd.Series":
    """normalize_name over a Series (same rules, vectorized)."""
    s = names.astype(str).str.normalize("NFKD").str.encode("ascii", "ignore").str.decode("ascii").str.lower()
    s = s.str.replace(_POSSESSIVE, "", regex=True).str.replace(_NON_WORD, " ", regex=True)
    s = s.str.replace(_WS, " ", regex=True).str.strip()
    return s.str.replace(_PLURAL, "", regex=True)


def _group(keys: "pd.Series", idx: np.ndarray) -> Dict[str, np.ndarray]:
    groups = pd.Series(np.arange(len(keys))).groupby(keys.to_numpy(), sort=False).indices
    return {k: np.unique(idx[pos]) for k, pos in groups.items() if k}


def canonical_type(node_type: Optional[str]) -> Optional[str]:
    if node_type is None:
        return None
    t = str(node_type).strip().lower()
    t = TYPE_ALIASES.get(t, t)
    if t not in NODE_TYPES:
        raise PrimeKGError(f"unknown PrimeKG node type {node_type!r}; expected one of {', '.join(NODE_TYPES)}")
    return t


def _pad7(raw: str) -> Optional[str]:
    try:
        return f"{int(str(raw).split('_')[0]):07d}"
    except ValueError:
        return None


def node_curie(node_type: str, node_id: str) -> Optional[str]:
    """A resolvable compact identifier for a node (MONDO_ / HP: / GO: / UBERON: / NCBIGene: / DrugBank / Reactome / MeSH)."""
    if node_type == "gene/protein":
        return f"NCBIGene:{node_id}"
    if node_type == "drug":
        return node_id
    if node_type == "disease":
        pad = _pad7(node_id)
        return f"MONDO_{pad}" if pad else None
    if node_type == "effect/phenotype":
        pad = _pad7(node_id)
        return f"HP:{pad}" if pad else None
    if node_type in ("biological_process", "molecular_function", "cellular_component"):
        pad = _pad7(node_id)
        return f"GO:{pad}" if pad else None
    if node_type == "anatomy":
        pad = _pad7(node_id)
        return f"UBERON:{pad}" if pad else None
    if node_type == "pathway":
        return node_id
    if node_type == "exposure":
        return f"MESH:{node_id}"
    return None


def node_url(node_type: str, node_id: str) -> Optional[str]:
    pad = _pad7(node_id)
    if node_type == "gene/protein":
        return f"https://www.ncbi.nlm.nih.gov/gene/{node_id}"
    if node_type == "drug":
        return f"https://go.drugbank.com/drugs/{node_id}"
    if node_type == "disease" and pad:
        return f"http://purl.obolibrary.org/obo/MONDO_{pad}"
    if node_type == "effect/phenotype" and pad:
        return f"https://hpo.jax.org/browse/term/HP:{pad}"
    if node_type in ("biological_process", "molecular_function", "cellular_component") and pad:
        return f"https://amigo.geneontology.org/amigo/term/GO:{pad}"
    if node_type == "anatomy" and pad:
        return f"http://purl.obolibrary.org/obo/UBERON_{pad}"
    if node_type == "pathway":
        return f"https://reactome.org/content/detail/{node_id}"
    if node_type == "exposure":
        return f"https://ctdbase.org/detail.go?type=chem&acc={node_id}"
    return None


class PrimeKG:
    """The full PrimeKG graph in memory."""

    def __init__(self, root: Optional[Path] = None) -> None:
        if not store_available(root):
            raise PrimeKGError(
                f"PrimeKG store not built in {store_dir(root)}; run `phronesis-primekg build`", kind="not_built"
            )
        sdir = store_dir(root)
        self.root = Path(root or primekg_root())
        self.manifest: Dict[str, Any] = store_manifest(root) or {}

        nodes = pq.read_table(sdir / "nodes.parquet").to_pandas()
        self.n = len(nodes)
        self.node_id = nodes["node_id"].astype(str).to_numpy(dtype=object)
        self.node_name = nodes["node_name"].astype(str).to_numpy(dtype=object)
        type_cat = nodes["node_type"].astype("category")
        self.type_names: List[str] = [str(c) for c in type_cat.cat.categories]
        self.node_type = type_cat.cat.codes.to_numpy().astype(np.int8)
        src_cat = nodes["node_source"].astype("category")
        self.source_names: List[str] = [str(c) for c in src_cat.cat.categories]
        self.node_source = src_cat.cat.codes.to_numpy().astype(np.int8)

        edges = pq.read_table(sdir / "edges.parquet")
        rel_col = edges.column("relation").combine_chunks()
        disp_col = edges.column("display_relation").combine_chunks()
        self.relation_names: List[str] = [str(v) for v in rel_col.dictionary.to_pylist()]
        self.display_names: List[str] = [str(v) for v in disp_col.dictionary.to_pylist()]
        self.n_edges = int(edges.num_rows)
        if all((sdir / f).is_file() for f in CSR_FILES):
            arrays = [np.load(sdir / f, allow_pickle=False) for f in CSR_FILES]
            csr = dict(zip(("indptr", "nbr", "rel", "disp"), arrays))
        else:  # stores built before the CSR was persisted
            csr = build_csr(edges.column("x").to_numpy(), edges.column("y").to_numpy(),
                            rel_col.indices.to_numpy(), disp_col.indices.to_numpy(), self.n)
        del edges
        self.indptr, self.nbr, self.erel, self.edisp = csr["indptr"], csr["nbr"], csr["rel"], csr["disp"]
        self.degree = np.diff(self.indptr).astype(np.int32)

        self._type_code = {t: i for i, t in enumerate(self.type_names)}
        self._rel_code = {r: i for i, r in enumerate(self.relation_names)}
        self._disp_code = {d: i for i, d in enumerate(self.display_names)}

        self.drug_features = self._feature_map(sdir / "drug_features.parquet")
        self.disease_features = self._feature_map(sdir / "disease_features.parquet")
        self._matrices: Dict[Tuple, sp.csr_matrix] = {}
        self._lock = threading.Lock()
        self._build_indices()

    # ------------------------------------------------------------------ loading helpers

    @staticmethod
    def _feature_map(path: Path) -> Dict[int, Dict[str, Any]]:
        df = pq.read_table(path).to_pandas().set_index("node_index")
        df = df.where(df.apply(lambda c: c.astype(str).str.strip().ne("") & c.notna()))
        return {int(k): {c: v for c, v in row.items() if isinstance(v, str)} for k, row in df.to_dict("index").items()}

    def _build_indices(self) -> None:
        """Lookup tables (vectorized): ids, lower-cased names, normalized names and name tokens, including the
        MONDO member names of grouped disease nodes as synonyms."""
        self._synonym_of: Dict[int, List[str]] = {}
        syn_idx: List[int] = []
        syn_names: List[str] = []
        by_id: Dict[str, List[int]] = defaultdict(list)
        for i, feat in self.disease_features.items():
            for mondo in filter(None, (feat.get("mondo_ids") or "").split("|")):
                by_id[mondo.upper()].append(i)
            own = self.node_name[i].lower()
            syns = [s for s in dict.fromkeys((feat.get("synonyms") or "").split("|")) if s and s.lower() != own]
            # "ulcerative colitis (disease)" / "stroke disorder" are also found as "ulcerative colitis" / "stroke".
            bare = _QUALIFIER.sub("", self.node_name[i]).strip()
            if bare and bare.lower() != own:
                syns.append(bare)
            self._synonym_of[i] = syns
            syn_idx.extend([i] * len(syns))
            syn_names.extend(syns)

        types = np.array(self.type_names, dtype=object)[self.node_type]
        for i, (t, nid, name) in enumerate(zip(types, self.node_id, self.node_name)):
            curie = node_curie(t, nid)
            if curie:
                by_id[curie.upper().replace(":", "_")].append(i)
            if t == "gene/protein":
                by_id[f"NCBIGENE_{nid}"].append(i)
                by_id[f"SYMBOL_{name.upper()}"].append(i)
            elif t in ("drug", "pathway", "exposure"):
                by_id[nid.upper()].append(i)
        self._by_id = dict(by_id)

        all_idx = np.concatenate([np.arange(self.n, dtype=np.int64), np.asarray(syn_idx, dtype=np.int64)])
        names = pd.Series(np.concatenate([self.node_name, np.asarray(syn_names, dtype=object)]), dtype=object)
        lower = names.str.lower()
        norm = normalize_series(names)
        self._by_name = _group(lower, all_idx)
        self._by_norm = _group(norm, all_idx)
        toks = norm.str.split().explode()
        tok_frame = pd.DataFrame({"tok": toks.to_numpy(), "idx": all_idx[toks.index.to_numpy()]}).dropna()
        tok_frame = tok_frame[tok_frame["tok"].str.len() > 1].drop_duplicates()
        self._tokens = _group(tok_frame["tok"], tok_frame["idx"].to_numpy())

        # Curated aliases, kept only when their target resolves exactly in this store.
        self._by_alias: Dict[str, np.ndarray] = {}
        for node_type, table in ALIASES.items():
            code = self._type_code.get(node_type)
            for alias, target in table.items():
                hits = self._by_name.get(target.lower())
                if hits is None:
                    hits = self._by_norm.get(normalize_name(target), np.empty(0, dtype=np.int64))
                hits = np.asarray([h for h in hits if int(self.node_type[h]) == code], dtype=np.int64)
                if hits.size:
                    self._by_alias[normalize_name(alias)] = hits[np.argsort(-self.degree[hits])][:1]

    # ------------------------------------------------------------------ basic accessors

    def type_of(self, i: int) -> str:
        return self.type_names[self.node_type[i]]

    def type_code(self, node_type: str) -> int:
        code = self._type_code.get(node_type)
        if code is None:
            raise PrimeKGError(f"node type {node_type!r} not present in this PrimeKG store")
        return code

    def nodes_of_type(self, node_type: str) -> np.ndarray:
        """Indices of every node of a type (empty for a type this store does not contain)."""
        code = self._type_code.get(canonical_type(node_type) or node_type)
        return np.flatnonzero(self.node_type == code) if code is not None else np.empty(0, dtype=np.int64)

    def relation_code(self, relation: str) -> int:
        code = self._rel_code.get(relation)
        if code is None:
            raise PrimeKGError(f"unknown PrimeKG relation {relation!r}")
        return code

    def display_code(self, display: str) -> int:
        code = self._disp_code.get(display)
        if code is None:
            raise PrimeKGError(f"unknown PrimeKG display relation {display!r}")
        return code

    def node(self, i: int, with_degree: bool = False) -> Dict[str, Any]:
        i = int(i)
        t = self.type_of(i)
        nid = str(self.node_id[i])
        out: Dict[str, Any] = {
            "index": i, "id": nid, "type": t, "name": str(self.node_name[i]),
            "source": self.source_names[self.node_source[i]], "curie": node_curie(t, nid), "url": node_url(t, nid),
        }
        if t == "disease":
            feat = self.disease_features.get(i) or {}
            mondo = [m for m in (feat.get("mondo_ids") or "").split("|") if m]
            if mondo:
                out["mondo_ids"] = mondo[:25]
                out["curie"] = mondo[0]
                out["url"] = f"http://purl.obolibrary.org/obo/{mondo[0]}"
        if with_degree:
            out["degree"] = int(self.degree[i])
        return out

    def name(self, i: int) -> str:
        return str(self.node_name[i])

    # ------------------------------------------------------------------ neighbors

    def neighbors(
        self,
        i: int,
        relations: Optional[Iterable[str]] = None,
        displays: Optional[Iterable[str]] = None,
        node_type: Optional[str] = None,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(neighbor indices, relation codes, display codes) of node i, optionally filtered."""
        lo, hi = self.indptr[i], self.indptr[i + 1]
        nb, rel, disp = self.nbr[lo:hi], self.erel[lo:hi], self.edisp[lo:hi]
        mask = np.ones(nb.size, dtype=bool)
        if relations is not None:
            codes = [self._rel_code[r] for r in relations if r in self._rel_code]
            mask &= np.isin(rel, codes)
        if displays is not None:
            codes = [self._disp_code[d] for d in displays if d in self._disp_code]
            mask &= np.isin(disp, codes)
        if node_type is not None:
            code = self._type_code.get(canonical_type(node_type) or node_type)
            mask &= (self.node_type[nb] == code) if code is not None else False
        return nb[mask], rel[mask], disp[mask]

    def neighbor_set(self, i: int, relations: Optional[Iterable[str]] = None, node_type: Optional[str] = None,
                     displays: Optional[Iterable[str]] = None) -> np.ndarray:
        nb, _, _ = self.neighbors(i, relations=relations, node_type=node_type, displays=displays)
        return np.unique(nb)

    def edge_labels(self, u: int, v: int) -> List[str]:
        lo, hi = self.indptr[u], self.indptr[u + 1]
        pos = np.flatnonzero(self.nbr[lo:hi] == v)
        return sorted({self.display_names[self.edisp[lo + p]] for p in pos})

    def edge_relations(self, u: int, v: int) -> List[Tuple[str, str]]:
        lo, hi = self.indptr[u], self.indptr[u + 1]
        pos = np.flatnonzero(self.nbr[lo:hi] == v)
        return sorted({(self.relation_names[self.erel[lo + p]], self.display_names[self.edisp[lo + p]]) for p in pos})

    def adjacency(self, relations: Optional[Iterable[str]] = None, exclude: Iterable[str] = (),
                  displays: Optional[Iterable[str]] = None) -> sp.csr_matrix:
        """Boolean symmetric adjacency restricted to relations (cached)."""
        key = (tuple(sorted(relations)) if relations is not None else None, tuple(sorted(exclude)),
               tuple(sorted(displays)) if displays is not None else None)
        with self._lock:
            cached = self._matrices.get(key)
        if cached is not None:
            return cached
        mask = np.ones(self.nbr.size, dtype=bool)
        if relations is not None:
            mask &= np.isin(self.erel, [self._rel_code[r] for r in relations if r in self._rel_code])
        if exclude:
            mask &= ~np.isin(self.erel, [self._rel_code[r] for r in exclude if r in self._rel_code])
        if displays is not None:
            mask &= np.isin(self.edisp, [self._disp_code[d] for d in displays if d in self._disp_code])
        rows = np.repeat(np.arange(self.n, dtype=np.int32), self.degree)[mask]
        mat = sp.csr_matrix((np.ones(int(mask.sum()), dtype=np.float32), (rows, self.nbr[mask])), shape=(self.n, self.n))
        mat.data[:] = 1.0  # duplicate (u, v) under several relations collapse to one entry
        with self._lock:
            self._matrices[key] = mat
        return mat

    def incidence(self, row_type: str, col_type: str, relations: Iterable[str],
                  displays: Optional[Iterable[str]] = None) -> Tuple[sp.csr_matrix, np.ndarray, np.ndarray]:
        """Binary row_type x col_type matrix over the given relations, with the row and column node indices."""
        key = ("inc", row_type, col_type, tuple(sorted(relations)), tuple(sorted(displays)) if displays else None)
        with self._lock:
            cached = self._matrices.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        rows_idx = self.nodes_of_type(row_type)
        cols_idx = self.nodes_of_type(col_type)
        adj = self.adjacency(relations=relations, displays=displays)
        sub = adj[rows_idx][:, cols_idx].tocsr()
        sub.data[:] = 1.0
        result = (sub, rows_idx, cols_idx)
        with self._lock:
            self._matrices[key] = result  # type: ignore[assignment]
        return result

    # ------------------------------------------------------------------ resolution

    def resolve(self, query: str, node_types: Optional[Sequence[str]] = None, limit: int = 5,
                fuzzy: bool = True) -> List[Match]:
        """Best node matches for a name, symbol or identifier, optionally restricted to node types."""
        q = str(query or "").strip()
        if not q:
            return []
        allowed: Optional[Set[int]] = None
        if node_types:
            allowed = set()
            for t in node_types:
                ct = canonical_type(t)
                if ct is not None and ct in self._type_code:
                    allowed.add(self._type_code[ct])

        def ok(i: int) -> bool:
            return allowed is None or int(self.node_type[i]) in allowed

        out: List[Match] = []
        seen: Set[int] = set()

        def push(idxs: Iterable[int], score: float, how: str) -> None:
            for i in idxs:
                if i not in seen and ok(i):
                    seen.add(i)
                    out.append(Match(int(i), score, how))

        key = q.upper().replace(":", "_")
        if re.fullmatch(r"\d+", q):
            push(self._by_id.get(f"NCBIGENE_{q}", []), 1.0, "id")
        push(self._by_id.get(key, []), 1.0, "id")
        if key.startswith("MONDO_") and re.fullmatch(r"MONDO_\d+", key):
            push(self._by_id.get(f"MONDO_{int(key[6:]):07d}", []), 1.0, "id")
        if re.fullmatch(r"(?:HP|GO|UBERON)_\d+", key):
            prefix, num = key.split("_", 1)
            push(self._by_id.get(f"{prefix}_{int(num):07d}", []), 1.0, "id")
        push(self._by_id.get(f"SYMBOL_{q.upper()}", []), 0.99, "symbol")
        exact = self._by_name.get(q.lower(), [])
        push([i for i in exact if self.node_name[i].lower() == q.lower()], 0.98, "name")
        push(exact, 0.95, "synonym")
        norm = normalize_name(q)
        push(self._by_alias.get(norm, []), 0.96, "alias")
        push(self._by_norm.get(norm, []), 0.93, "normalized")
        stripped = " ".join(t for t in norm.split() if t not in SALT_WORDS)
        if stripped and stripped != norm:
            push(self._by_norm.get(stripped, []), 0.9, "salt_stripped")
        if len(out) >= limit or not fuzzy or not norm:
            return self._rank(out)[:limit]

        # Fuzzy: candidates sharing the rarest query tokens. A name containing every query token scores by how
        # much of it the query covers ("type 2 diabetes" -> "type 2 diabetes mellitus"); otherwise by string
        # similarity.
        toks = [t for t in (stripped or norm).split() if len(t) > 1]
        if not toks:
            return self._rank(out)[:limit]
        q_set = set((stripped or norm).split())  # all tokens, so "type 2 diabetes" never matches type 1
        postings = sorted((self._tokens.get(t, np.empty(0, dtype=np.int64)) for t in toks), key=len)
        cand: Set[int] = set()
        for plist in postings[:2]:
            cand.update(plist[:20000].tolist())
        if not cand:
            close = difflib.get_close_matches(norm, list(self._by_norm.keys()), n=limit, cutoff=0.85) if len(norm) > 4 else []
            for c in close:
                push(self._by_norm[c], 0.8, "fuzzy")
            return self._rank(out)[:limit]
        scored = []
        for i in cand:
            if i in seen or not ok(i):
                continue
            best = 0.0
            for nm in (self.node_name[i], *self._synonym_of.get(i, [])):
                n_norm = normalize_name(nm)
                n_set = set(n_norm.split())
                if q_set <= n_set:
                    s = 0.8 + 0.15 * len(q_set) / max(1, len(n_set))
                else:
                    s = 0.9 * difflib.SequenceMatcher(None, norm, n_norm).ratio()
                best = max(best, s)
            if best >= 0.54:
                scored.append((best, i))
        scored.sort(key=lambda p: (-p[0], -self.degree[p[1]]))
        for score, i in scored[: limit * 2]:
            push([i], round(score, 3), "fuzzy")
        return self._rank(out)[:limit]

    def _rank(self, matches: List[Match]) -> List[Match]:
        # Within a score tier prefer the better-connected node (a grouped disease over a sparse leaf).
        return sorted(matches, key=lambda m: (-m.score, -int(self.degree[m.index])))

    def resolve_one(self, query: str, node_types: Optional[Sequence[str]] = None, min_score: float = 0.75) -> int:
        matches = self.resolve(query, node_types=node_types, limit=1)
        if not matches or matches[0].score < min_score:
            types = f" ({', '.join(node_types)})" if node_types else ""
            raise PrimeKGError(f"no PrimeKG node matches {query!r}{types}", kind="not_found")
        return matches[0].index

    def scan_text(self, text: str, node_types: Sequence[str], max_ngram: int = 6,
                  min_chars: int = 4, stop: Optional[Set[str]] = None) -> List[Tuple[int, str]]:
        """Exact normalized name matches of the given types inside free text, longest first, non-overlapping."""
        stop = stop or set()
        codes = {self._type_code[t] for t in node_types if t in self._type_code}
        words = normalize_name(text).split()
        taken = [False] * len(words)
        found: List[Tuple[int, str]] = []
        for size in range(min(max_ngram, len(words)), 0, -1):
            for start in range(0, len(words) - size + 1):
                if any(taken[start:start + size]):
                    continue
                phrase = " ".join(words[start:start + size])
                if len(phrase) < min_chars or phrase in stop:
                    continue
                hits = [i for i in self._by_norm.get(phrase, []) if int(self.node_type[i]) in codes]
                if not hits and len(phrase) >= 3 and phrase in self._by_alias:
                    hits = [i for i in self._by_alias[phrase] if int(self.node_type[i]) in codes]
                if hits:
                    best = max(hits, key=lambda i: int(self.degree[i]))
                    found.append((best, phrase))
                    for k in range(start, start + size):
                        taken[k] = True
        return found

    # ------------------------------------------------------------------ metadata

    def provenance(self) -> Dict[str, Any]:
        m = self.manifest
        return {
            "source": "PrimeKG",
            "dataverse_doi": m.get("dataverse_doi", DATAVERSE_DOI),
            "dataverse_version": m.get("dataverse_version"),
            "store_built_at": m.get("built_at"),
            "edges_sha256": ((m.get("raw_files") or {}).get("edges.csv") or {}).get("sha256"),
            "citation": m.get("citation", CITATION),
        }

    def stats(self) -> Dict[str, Any]:
        counts = (self.manifest.get("counts") or {})
        return {
            "nodes": self.n,
            "undirected_edges": self.n_edges,
            "node_types": counts.get("node_types") or {
                t: int((self.node_type == c).sum()) for t, c in self._type_code.items()},
            "relations": counts.get("relations"),
            "drugs_with_clinical_text": len(self.drug_features),
            "diseases_with_clinical_text": len(self.disease_features),
            **self.provenance(),
        }


_INSTANCE: Optional[PrimeKG] = None
_INSTANCE_LOCK = threading.Lock()


def get_primekg(root: Optional[Path] = None) -> PrimeKG:
    """Process-wide PrimeKG (loaded on first use; raises PrimeKGError(kind='not_built') without a store)."""
    global _INSTANCE
    if _INSTANCE is not None and (root is None or Path(root) == _INSTANCE.root):
        return _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None or (root is not None and Path(root) != _INSTANCE.root):
            _INSTANCE = PrimeKG(root)
        return _INSTANCE


def reset_primekg() -> None:
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None
