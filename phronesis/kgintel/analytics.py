"""
Analyses over the full PrimeKG. Every function takes a loaded PrimeKG and node indices, and returns a
JSON-safe dict whose node entries carry index, id, curie, url, name and type.

    drug_profile          indications, contraindications, off-label uses, targets/enzymes/transporters/carriers,
                          side effects, interaction count, DrugBank clinical text
    disease_profile       genes, phenotypes present/absent, approved/contraindicated/off-label drugs,
                          related diseases, exposures, MONDO/Orphanet/Mayo clinical text
    gene_profile          drugs by role, diseases, pathways, GO terms, phenotypes, tissue expression, PPI
    mechanism_paths       shortest typed paths between two nodes, ranked toward specific (low-degree) connectors
    drug_disease          the direct relation of a drug to a disease, shared genes, and mechanism paths
    repurposing           network-proximity (Guney et al. 2016) + target overlap + similar-disease indications,
                          with known indications held out as a calibration check
    phenotype_diagnosis   information-content-weighted phenotype overlap ranking of diseases
    polypharmacy          pairwise interactions and shared metabolising enzymes / transporters / targets
    enrichment            hypergeometric over-representation of a gene set (BH-adjusted)
    similar               same-type nodes by Jaccard similarity of their neighbourhoods
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
import scipy.sparse as sp
from scipy.stats import hypergeom

from .graph import DENSE_RELATIONS, PrimeKG, PrimeKGError, normalize_name

GENE = "gene/protein"
DRUG = "drug"
DISEASE = "disease"
PHENOTYPE = "effect/phenotype"
DRUG_ROLES = ("target", "enzyme", "transporter", "carrier")
TEXT_LIMIT = 700


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------

def brief(kg: PrimeKG, i: int) -> Dict[str, Any]:
    n = kg.node(i)
    return {k: n[k] for k in ("index", "id", "name", "type", "curie", "url") if n.get(k) is not None}


def _ranked(kg: PrimeKG, idxs: Iterable[int], limit: int, by: str = "degree") -> List[Dict[str, Any]]:
    arr = np.unique(np.asarray(list(idxs), dtype=np.int64))
    if by == "degree":
        arr = arr[np.argsort(-kg.degree[arr], kind="stable")]
    else:
        arr = np.array(sorted(arr, key=lambda i: kg.name(i).lower()), dtype=np.int64)
    return [brief(kg, int(i)) for i in arr[:limit]]


def _section(kg: PrimeKG, idxs: np.ndarray, limit: int, by: str = "degree") -> Dict[str, Any]:
    idxs = np.unique(idxs)
    return {"total": int(idxs.size), "items": _ranked(kg, idxs, limit, by=by)}


def _clip(text: Optional[str], limit: int = TEXT_LIMIT) -> Optional[str]:
    if not text:
        return None
    s = re.sub(r"\s+", " ", str(text)).strip()
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + "…"


_GROUP_WORDS = ("approved", "investigational", "experimental", "withdrawn", "illicit", "nutraceutical", "vet_approved")


def parse_drug_features(name: str, feat: Dict[str, Any]) -> Dict[str, Any]:
    """DrugBank-derived drug text with the 'X is ...' templating removed."""
    if not feat:
        return {}
    groups_text = str(feat.get("group") or "").lower()
    groups = [g for g in _GROUP_WORDS if re.search(rf"\b{g}\b", groups_text)]
    cats = str(feat.get("category") or "")
    cats = re.sub(rf"^{re.escape(name)} is part of\s*", "", cats, flags=re.IGNORECASE).rstrip(". ")
    categories = [c.strip() for c in cats.split(" ; ") if c.strip()]
    atc = {}
    for col, label, pattern in (
        ("atc_1", "anatomical", r"anatomically related to (.+)"),
        ("atc_2", "therapeutic", r"therapeutic group of (.+)"),
        ("atc_3", "pharmacological", r"pharmacologically related to (.+)"),
        ("atc_4", "chemical", r"functional group of .*? is (.+)"),
    ):
        m = re.search(pattern, str(feat.get(col) or ""), flags=re.IGNORECASE)
        if m:
            atc[label] = m.group(1).strip().rstrip(".")
    state = re.sub(rf"^{re.escape(name)} is (?:a |an )?", "", str(feat.get("state") or ""), flags=re.IGNORECASE).rstrip(". ")

    def num(col: str) -> Optional[str]:
        raw = str(feat.get(col) or "")
        m = re.search(r"(-?\d+(?:\.\d+)?)", raw)
        return m.group(1) if m else None

    return {k: v for k, v in {
        "groups": groups or None,
        "state": state or None,
        "atc": atc or None,
        "categories": categories[:25] or None,
        "description": _clip(feat.get("description")),
        "indication_text": _clip(feat.get("indication")),
        "mechanism_of_action": _clip(feat.get("mechanism_of_action")),
        "pharmacodynamics": _clip(feat.get("pharmacodynamics")),
        "half_life": _clip(feat.get("half_life"), 300),
        "protein_binding": _clip(feat.get("protein_binding"), 300),
        "pathway": _clip(feat.get("pathway"), 300),
        "molecular_weight": num("molecular_weight"),
        "tpsa": num("tpsa"),
        "clogp": num("clogp"),
    }.items() if v}


def parse_disease_features(feat: Dict[str, Any]) -> Dict[str, Any]:
    if not feat:
        return {}
    definition = feat.get("mondo_definition") or feat.get("orphanet_definition") or feat.get("umls_description")
    return {k: v for k, v in {
        "definition": _clip(definition),
        "prevalence": _clip(feat.get("orphanet_prevalence"), 200),
        "epidemiology": _clip(feat.get("orphanet_epidemiology"), 500),
        "clinical_description": _clip(feat.get("orphanet_clinical_description")),
        "management_and_treatment": _clip(feat.get("orphanet_management_and_treatment")),
        "symptoms": _clip(feat.get("mayo_symptoms")),
        "causes": _clip(feat.get("mayo_causes")),
        "risk_factors": _clip(feat.get("mayo_risk_factors"), 500),
        "complications": _clip(feat.get("mayo_complications"), 500),
        "prevention": _clip(feat.get("mayo_prevention"), 400),
        "disease_group": feat.get("group_name_bert"),
        "member_mondo_ids": [m for m in (feat.get("mondo_ids") or "").split("|") if m][:25] or None,
        "synonyms": [s for s in (feat.get("synonyms") or "").split("|") if s][:15] or None,
    }.items() if v}


def _require(kg: PrimeKG, i: int, node_type: str) -> None:
    if not 0 <= int(i) < kg.n:
        raise PrimeKGError(f"node index {i} out of range", kind="invalid")
    if kg.type_of(int(i)) != node_type:
        raise PrimeKGError(f"{kg.name(int(i))!r} is a {kg.type_of(int(i))}, not a {node_type}", kind="invalid")


def bfs_distances(adj: sp.csr_matrix, sources: Sequence[int], max_depth: int,
                  blocked: Optional[np.ndarray] = None) -> np.ndarray:
    """Hop distance from the nearest source (-1 beyond max_depth). Blocked nodes are reached but not expanded."""
    dist = np.full(adj.shape[0], -1, dtype=np.int16)
    frontier = np.unique(np.asarray(sources, dtype=np.int64))
    dist[frontier] = 0
    for depth in range(1, max_depth + 1):
        expand = frontier if blocked is None else frontier[~blocked[frontier]]
        if expand.size == 0:
            break
        nxt = np.unique(adj[expand].indices)
        nxt = nxt[dist[nxt] < 0]
        if nxt.size == 0:
            break
        dist[nxt] = depth
        frontier = nxt
    return dist


# -----------------------------------------------------------------------------
# profiles
# -----------------------------------------------------------------------------

def drug_profile(kg: PrimeKG, i: int, limit: int = 25) -> Dict[str, Any]:
    _require(kg, i, DRUG)
    nb, rel, disp = kg.neighbors(i)

    def pick(relation: str, display: Optional[str] = None) -> np.ndarray:
        mask = rel == kg._rel_code.get(relation, -1)
        if display is not None:
            mask &= disp == kg._disp_code.get(display, -1)
        return nb[mask]

    roles = {role: _section(kg, pick("drug_protein", role), limit) for role in DRUG_ROLES}
    return {
        "drug": kg.node(i, with_degree=True),
        "clinical": parse_drug_features(kg.name(i), kg.drug_features.get(int(i), {})),
        "indications": _section(kg, pick("indication"), limit),
        "contraindications": _section(kg, pick("contraindication"), limit),
        "off_label_uses": _section(kg, pick("off-label use"), limit),
        "proteins": roles,
        "side_effects": _section(kg, pick("drug_effect"), limit),
        "drug_interactions": {"total": int(np.unique(pick("drug_drug")).size),
                              "note": "DrugBank drug-drug interaction pairs; use polypharmacy for a specific regimen"},
    }


def disease_profile(kg: PrimeKG, i: int, limit: int = 25, indications_from: Optional[Sequence[int]] = None) -> Dict[str, Any]:
    """Disease profile. indications_from: related disease nodes (indication_proxy) whose approved / off-label
    drugs are shown, labelled with that disease, when this node records none."""
    _require(kg, i, DISEASE)
    sec = lambda rel, t=None: _section(kg, kg.neighbor_set(i, [rel], node_type=t), limit)  # noqa: E731
    related = []
    for p in indications_from or []:
        p = int(p)
        if p == int(i) or kg.type_of(p) != DISEASE:
            continue
        related.append({"disease": brief(kg, p),
                        "indicated_drugs": _section(kg, kg.neighbor_set(p, ["indication"], node_type=DRUG), limit),
                        "off_label_drugs": _section(kg, kg.neighbor_set(p, ["off-label use"], node_type=DRUG), limit)})
    out: Dict[str, Any] = {
        "disease": kg.node(i, with_degree=True),
        "clinical": parse_disease_features(kg.disease_features.get(int(i), {})),
        "genes": sec("disease_protein", GENE),
        "phenotypes_present": sec("disease_phenotype_positive", PHENOTYPE),
        "phenotypes_absent": sec("disease_phenotype_negative", PHENOTYPE),
        "indicated_drugs": sec("indication", DRUG),
        "contraindicated_drugs": sec("contraindication", DRUG),
        "off_label_drugs": sec("off-label use", DRUG),
        "related_diseases": sec("disease_disease", DISEASE),
        "exposures": sec("exposure_disease", "exposure"),
    }
    if related:
        out["indications_from_related"] = related
    return out


# Words that say what kind of disease something is, not which one: sharing them is no evidence of relatedness.
_CONTEXT_STOP = frozenset(
    "disease disorder syndrome cancer carcinoma neoplasm neoplasia tumor tumour malignant benign type of and the "
    "with in to or by due primary familial hereditary susceptibility chronic acute adult childhood juvenile "
    "early late onset form variant".split()
)


def _stem(tok: str) -> str:
    return tok[:6] if len(tok) >= 6 else tok  # "pancreatic" ~ "pancreas"


class DiseaseContext:
    """The diseases a question is about, used to put related entries first in a gene profile.

    A disease scores highest when it is a context disease, next when it is one disease_disease (MONDO parent/child)
    step away, otherwise by the specificity-weighted name words it shares with a context disease.
    """

    def __init__(self, kg: PrimeKG, diseases: Sequence[int]) -> None:
        self.kg = kg
        self.core: Set[int] = {int(d) for d in diseases}
        self.family: Set[int] = set(self.core)
        for d in self.core:
            self.family.update(int(x) for x in kg.neighbor_set(d, ["disease_disease"], node_type=DISEASE))
        n_disease = max(1, int(kg.nodes_of_type(DISEASE).size))
        self.weights: Dict[str, float] = {}
        for d in self.core:
            for tok in normalize_name(kg.name(d)).split():
                if len(tok) < 3 or tok in _CONTEXT_STOP:
                    continue
                df = int(np.asarray(kg._tokens.get(tok, np.empty(0))).size) if hasattr(kg, "_tokens") else 1
                w = math.log(1.0 + n_disease / max(1, df))
                self.weights[_stem(tok)] = max(self.weights.get(_stem(tok), 0.0), w)
        self._cache: Dict[int, float] = {}

    def score(self, i: int) -> float:
        i = int(i)
        if i in self._cache:
            return self._cache[i]
        if i in self.core:
            s = 1000.0
        elif i in self.family:
            s = 500.0
        else:
            stems = {_stem(t) for t in normalize_name(self.kg.name(i)).split() if len(t) >= 3 and t not in _CONTEXT_STOP}
            s = sum(self.weights.get(t, 0.0) for t in stems)
        self._cache[i] = s
        return s

    def drug_level(self, drug: int) -> Tuple[int, float]:
        """(2 when the drug is indicated for a context-matched disease, 1 when used off-label for one, else 0;
        the best context score among those diseases)."""
        level, best = 0, 0.0
        nb, rel, _ = self.kg.neighbors(drug, relations=["indication", "off-label use"], node_type=DISEASE)
        ind = self.kg._rel_code.get("indication", -1)
        for v, r in zip(nb.tolist(), rel.tolist()):
            sc = self.score(v)
            if sc > 0:
                lv = 2 if r == ind else 1
                if (lv, sc) > (level, best):
                    level, best = lv, sc
        return level, best

    def describe(self) -> Dict[str, Any]:
        return {"diseases": [brief(self.kg, d) for d in sorted(self.core)],
                "rule": "context diseases, their MONDO parents/children, then diseases sharing specific name words "
                        "rank first; drugs indicated (then off-label) for those diseases rank first, then approved "
                        "drugs, then by graph degree"}


def _approved(kg: PrimeKG, drug: int) -> bool:
    return bool(re.search(r"\bapproved\b", str((kg.drug_features.get(int(drug)) or {}).get("group") or ""), re.IGNORECASE))


def _context_section(kg: PrimeKG, idxs: np.ndarray, limit: int, kind: str, ctx: "DiseaseContext") -> Dict[str, Any]:
    """A section ranked by relevance to the question's diseases, then degree; matches carry context_match."""
    arr = [int(i) for i in np.unique(idxs)]
    if kind == DISEASE:
        rel = {i: ctx.score(i) for i in arr}
        order = sorted(arr, key=lambda i: (-rel[i], -int(kg.degree[i])))
        matched = sum(1 for i in arr if rel[i] > 0)
    else:
        lvl = {i: ctx.drug_level(i) for i in arr}
        order = sorted(arr, key=lambda i: (-lvl[i][0], -lvl[i][1], -int(_approved(kg, i)), -int(kg.degree[i])))
        rel = {i: float(lvl[i][0]) for i in arr}
        matched = sum(1 for i in arr if lvl[i][0] > 0)
    items = []
    for i in order[:limit]:
        item = brief(kg, i)
        if rel[i] > 0:
            item["context_match"] = True
        items.append(item)
    return {"total": len(arr), "items": items, "context_matches": matched}


def gene_profile(kg: PrimeKG, i: int, limit: int = 25, context: Optional[Sequence[int]] = None) -> Dict[str, Any]:
    """Gene profile. With context (the question's disease node indices) the disease and drug lists put entries
    related to those diseases first instead of the globally best-connected hubs."""
    _require(kg, i, GENE)
    ctx = DiseaseContext(kg, [int(c) for c in context if kg.type_of(int(c)) == DISEASE]) if context else None
    if ctx is not None and not ctx.core:
        ctx = None

    def drug_sec(role: str) -> Dict[str, Any]:
        idxs = kg.neighbor_set(i, ["drug_protein"], node_type=DRUG, displays=[role])
        return _context_section(kg, idxs, limit, DRUG, ctx) if ctx is not None else _section(kg, idxs, limit)

    drugs = {role: drug_sec(role) for role in DRUG_ROLES}
    sec = lambda rel, t=None: _section(kg, kg.neighbor_set(i, [rel], node_type=t), limit)  # noqa: E731
    disease_ids = kg.neighbor_set(i, ["disease_protein"], node_type=DISEASE)
    out: Dict[str, Any] = {
        "gene": kg.node(i, with_degree=True),
        "drugs": drugs,
        "diseases": _context_section(kg, disease_ids, limit, DISEASE, ctx) if ctx is not None
        else _section(kg, disease_ids, limit),
        "pathways": sec("pathway_protein", "pathway"),
        "biological_processes": sec("bioprocess_protein", "biological_process"),
        "molecular_functions": sec("molfunc_protein", "molecular_function"),
        "cellular_components": sec("cellcomp_protein", "cellular_component"),
        "phenotypes": sec("phenotype_protein", PHENOTYPE),
        "expressed_in": sec("anatomy_protein_present", "anatomy"),
        "not_expressed_in": sec("anatomy_protein_absent", "anatomy"),
        "interactors": sec("protein_protein", GENE),
        "exposures": sec("exposure_protein", "exposure"),
    }
    if ctx is not None:
        out["context"] = ctx.describe()
    return out


def neighbors(kg: PrimeKG, i: int, relation: Optional[str] = None, node_type: Optional[str] = None,
              limit: int = 50) -> Dict[str, Any]:
    nb, rel, disp = kg.neighbors(i, relations=[relation] if relation else None, node_type=node_type)
    groups: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    for v, r, d in zip(nb.tolist(), rel.tolist(), disp.tolist()):
        groups[(kg.relation_names[r], kg.display_names[d])].append(v)
    return {
        "node": kg.node(i, with_degree=True),
        "groups": [
            {"relation": r, "display_relation": d, "total": len(set(v)), "items": _ranked(kg, v, limit)}
            for (r, d), v in sorted(groups.items(), key=lambda kv: -len(kv[1]))
        ],
    }


# -----------------------------------------------------------------------------
# paths
# -----------------------------------------------------------------------------

def mechanism_paths(
    kg: PrimeKG, a: int, b: int, max_hops: int = 3, limit: int = 10,
    exclude_relations: Iterable[str] = DENSE_RELATIONS, hub_degree: int = 2500,
) -> Dict[str, Any]:
    """Shortest typed paths a -> b; intermediates above hub_degree are avoided unless that leaves no path."""
    if a == b:
        raise PrimeKGError("source and target are the same node", kind="invalid")
    adj = kg.adjacency(exclude=tuple(exclude_relations))
    hubs_avoided = True
    blocked = kg.degree > hub_degree
    blocked[[a, b]] = False
    ds = bfs_distances(adj, [a], max_hops, blocked)
    dt = bfs_distances(adj, [b], max_hops, blocked)
    if ds[b] < 0:
        hubs_avoided = False
        blocked = None
        ds = bfs_distances(adj, [a], max_hops)
        dt = bfs_distances(adj, [b], max_hops)
    length = int(ds[b]) if ds[b] >= 0 else -1
    result: Dict[str, Any] = {
        "source": brief(kg, a), "target": brief(kg, b), "max_hops": max_hops,
        "excluded_relations": sorted(exclude_relations), "hubs_avoided": hubs_avoided and length > 0,
        "shortest_length": length if length > 0 else None, "paths": [], "metapaths": [],
    }
    if length <= 0:
        return result
    on = (ds >= 0) & (dt >= 0) & ((ds.astype(np.int32) + dt) == length)
    if blocked is not None:
        on &= ~blocked
        on[[a, b]] = True

    found: List[List[int]] = []
    budget = [max(200, limit * 30)]

    def walk(path: List[int]) -> None:
        if budget[0] <= 0:
            return
        u = path[-1]
        k = len(path) - 1
        if u == b:
            found.append(list(path))
            budget[0] -= 1
            return
        row = adj.indices[adj.indptr[u]:adj.indptr[u + 1]]
        nxt = row[on[row] & (ds[row] == k + 1)]
        for v in nxt[np.argsort(kg.degree[nxt], kind="stable")][:25]:
            path.append(int(v))
            walk(path)
            path.pop()

    walk([int(a)])
    scored = sorted(found, key=lambda p: sum(math.log(max(2, int(kg.degree[v]))) for v in p[1:-1]))
    metas: Counter = Counter()
    for p in scored:
        steps = []
        for u, v in zip(p, p[1:]):
            labels = kg.edge_labels(u, v)
            steps.append({"from": int(u), "to": int(v), "relations": labels})
        meta = " → ".join([kg.type_of(p[0])] + [f"[{'/'.join(s['relations'])}] {kg.type_of(s['to'])}" for s in steps])
        metas[meta] += 1
        if len(result["paths"]) < limit:
            result["paths"].append({
                "nodes": [brief(kg, v) for v in p],
                "relations": [s["relations"] for s in steps],
                "text": " — ".join([kg.name(p[0])] + [f"[{'/'.join(s['relations'])}] {kg.name(s['to'])}" for s in steps]),
                "specificity": round(sum(math.log(max(2, int(kg.degree[v]))) for v in p[1:-1]), 2),
            })
    result["n_paths_enumerated"] = len(found)
    result["metapaths"] = [{"pattern": m, "count": c} for m, c in metas.most_common(8)]
    return result


def drug_disease(kg: PrimeKG, drug: int, disease: int, limit: int = 8) -> Dict[str, Any]:
    _require(kg, drug, DRUG)
    _require(kg, disease, DISEASE)
    direct = [d for r, d in kg.edge_relations(drug, disease)]
    targets = kg.neighbor_set(drug, ["drug_protein"], node_type=GENE)
    genes = kg.neighbor_set(disease, ["disease_protein"], node_type=GENE)
    shared = np.intersect1d(targets, genes)
    # Disease genes one PPI step from a target.
    ppi = kg.adjacency(relations=["protein_protein"])
    near = np.unique(ppi[targets].indices) if targets.size else np.array([], dtype=np.int64)
    near_genes = np.setdiff1d(np.intersect1d(near, genes), shared)
    related = kg.neighbor_set(disease, ["disease_disease"], node_type=DISEASE)
    related_ind = [int(r) for r in related if "indication" in kg.edge_labels(drug, int(r))]
    return {
        "drug": brief(kg, drug), "disease": brief(kg, disease),
        "direct_relations": direct,
        "status": ("contraindicated" if "contraindication" in direct else "indicated" if "indication" in direct
                   else "off-label" if "off-label use" in direct else "no direct relation in PrimeKG"),
        "drug_targets_total": int(targets.size), "disease_genes_total": int(genes.size),
        "shared_genes": _ranked(kg, shared, 25),
        "disease_genes_adjacent_to_targets": {"total": int(near_genes.size), "items": _ranked(kg, near_genes, 15)},
        "indicated_for_related_diseases": [brief(kg, r) for r in related_ind[:10]],
        "paths": mechanism_paths(kg, drug, disease, max_hops=3, limit=limit),
    }


# -----------------------------------------------------------------------------
# repurposing
# -----------------------------------------------------------------------------

def _jaccard_rows(mat: sp.csr_matrix, row: int) -> np.ndarray:
    v = mat[row]
    inter = np.asarray((mat @ v.T).todense()).ravel()
    sizes = np.asarray(mat.sum(axis=1)).ravel()
    union = sizes + sizes[row] - inter
    with np.errstate(divide="ignore", invalid="ignore"):
        j = np.where(union > 0, inter / union, 0.0)
    return j


def disease_similarity(kg: PrimeKG, disease: int) -> Tuple[np.ndarray, np.ndarray]:
    """(disease node indices, similarity) by mean Jaccard of gene and phenotype sets (one set if the other is empty)."""
    g_mat, rows, _ = kg.incidence(DISEASE, GENE, ["disease_protein"])
    p_mat, _, _ = kg.incidence(DISEASE, PHENOTYPE, ["disease_phenotype_positive"])
    r = int(np.searchsorted(rows, disease))
    jg, jp = _jaccard_rows(g_mat, r), _jaccard_rows(p_mat, r)
    has_g, has_p = g_mat[r].nnz > 0, p_mat[r].nnz > 0
    sim = (jg + jp) / 2 if has_g and has_p else jg if has_g else jp
    sim[r] = 0.0
    return rows, sim


# Chosen on a 20-disease benchmark (recovering each disease's known indications, scripts/benchmark_primekg_repurposing.py):
# mean AUROC 0.81 with direct ontology relatives held out of the similar-disease signal (0.75 for that signal alone,
# 0.70 proximity alone, 0.66 target overlap alone). The components are complementary; no weighting of one alone wins.
REPURPOSING_WEIGHTS = {"target_overlap": 1.0, "network_proximity": 0.5, "similar_disease_indication": 2.0}


def repurposing_signals(kg: PrimeKG, disease: int, n_random: int = 200, seed: int = 0) -> Dict[str, Any]:
    """Per-drug evidence for a disease: specificity-weighted target hits on disease genes, PPI network proximity
    (z-scored against degree-matched random target sets) and indications for similar diseases."""
    _require(kg, disease, DISEASE)
    rng = np.random.default_rng(seed)
    disease_genes = kg.neighbor_set(disease, ["disease_protein"], node_type=GENE)
    expanded_from: List[int] = []
    if disease_genes.size < 3:
        for r in kg.neighbor_set(disease, ["disease_disease"], node_type=DISEASE):
            extra = kg.neighbor_set(int(r), ["disease_protein"], node_type=GENE)
            if extra.size:
                expanded_from.append(int(r))
                disease_genes = np.union1d(disease_genes, extra)

    # Gene specificity: a gene associated with many diseases (ESR1, TNF) says little about this one.
    g_mat, _, g_cols = kg.incidence(DISEASE, GENE, ["disease_protein"])
    n_dis_per_gene = np.asarray(g_mat.sum(axis=0)).ravel()
    idf = np.log((g_mat.shape[0] + 1) / (n_dis_per_gene + 1))
    gene_weight = dict(zip(g_cols.tolist(), (idf / max(float(idf.max()), 1e-9)).tolist()))

    ppi = kg.adjacency(relations=["protein_protein"])
    t_mat, drug_rows, gene_cols = kg.incidence(DRUG, GENE, ["drug_protein"], displays=["target"])
    n_targets = dict(zip(drug_rows.tolist(), np.diff(t_mat.indptr).tolist()))
    proximity: Dict[int, Tuple[float, float, int]] = {}
    if disease_genes.size:
        dist = bfs_distances(ppi, disease_genes, max_depth=8).astype(np.float64)
        ppi_deg = np.diff(ppi.indptr)
        reachable = gene_cols[(ppi_deg[gene_cols] > 0) & (dist[gene_cols] >= 0)]
        bins = np.floor(np.log2(np.maximum(ppi_deg[reachable], 1))).astype(int)
        bin_members = {b: reachable[bins == b] for b in np.unique(bins)}
        gene_bin = dict(zip(reachable.tolist(), bins.tolist()))
        for r in range(t_mat.shape[0]):
            cols = t_mat.indices[t_mat.indptr[r]:t_mat.indptr[r + 1]]
            targets = [int(g) for g in gene_cols[cols] if int(g) in gene_bin]
            if not targets:
                continue
            d_c = float(dist[targets].mean())
            samples = np.stack([rng.choice(bin_members[gene_bin[t]], size=n_random) for t in targets], axis=1)
            rand = dist[samples].mean(axis=1)
            sd = float(rand.std())
            proximity[int(drug_rows[r])] = (d_c, (d_c - float(rand.mean())) / sd if sd > 0 else 0.0, len(targets))

    direct: Dict[int, List[int]] = defaultdict(list)
    for g in disease_genes.tolist():
        for d in kg.neighbor_set(g, ["drug_protein"], node_type=DRUG, displays=["target"]).tolist():
            direct[int(d)].append(g)

    d_rows, sim = disease_similarity(kg, disease)
    top = np.argsort(-sim)[:30]
    similar = [(int(d_rows[t]), float(sim[t])) for t in top if sim[t] > 0.05]
    sim_ind: Dict[int, List[Tuple[int, float]]] = defaultdict(list)
    for d, s in similar:
        for drug in kg.neighbor_set(d, ["indication"], node_type=DRUG).tolist():
            sim_ind[int(drug)].append((d, s))

    drugs: Dict[int, Dict[str, Any]] = {}
    for drug in set(proximity) | set(direct) | set(sim_ind):
        d_c, z, n_ppi = proximity.get(drug, (None, None, 0))
        hits = direct.get(drug, [])
        drugs[drug] = {
            "hits": hits, "hit_weight": float(sum(gene_weight.get(g, 0.0) for g in hits)),
            "n_targets": int(n_targets.get(drug, 0)), "d_c": d_c, "z": z, "n_ppi": n_ppi,
            "sims": sorted(sim_ind.get(drug, []), key=lambda p: -p[1]),
        }
    return {"disease_genes": disease_genes, "expanded_from": expanded_from, "similar": similar, "drugs": drugs,
            "pool": int(len(drug_rows))}


def score_repurposing(f: Dict[str, Any], weights: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """Component scores (each 0-3 before weighting) for one drug's signals."""
    w = {**REPURPOSING_WEIGHTS, **(weights or {})}
    selectivity = 1.0 / math.sqrt(max(1.0, f["n_targets"] / 5.0))  # promiscuous binders hit disease genes by chance
    s_direct = min(3.0, 1.5 * f["hit_weight"]) * selectivity
    s_prox = min(3.0, max(0.0, -f["z"]) / 1.5) if f["z"] is not None else 0.0
    s_sim = 3.0 * (f["sims"][0][1] if f["sims"] else 0.0)
    return {"target_overlap": round(w["target_overlap"] * s_direct, 3),
            "network_proximity": round(w["network_proximity"] * s_prox, 3),
            "similar_disease_indication": round(w["similar_disease_indication"] * s_sim, 3)}


_SALT_TAIL = re.compile(
    r"\s+(?:acetate|benzoate|cypionate|valerate|dienanthate|propionate|dipropionate|sodium|potassium|calcium|"
    r"hydrochloride|mesylate|maleate|succinate|sulfate|phosphate|citrate|tartrate|fumarate|besylate|bromide|"
    r"chloride|hemihydrate|monohydrate|undecanoate|enanthate)$",
    re.IGNORECASE,
)


def calibration_stats(rank_of: Dict[int, int], indicated: Set[int], pool: int) -> Dict[str, Any]:
    known_ranks = sorted(rank_of[d] for d in indicated if d in rank_of)
    auroc = None
    if indicated and pool > len(indicated):
        # P(known indication outranks a random other drug); unscored drugs tie at the bottom.
        other_ranks = np.sort(np.array([k for d, k in rank_of.items() if d not in indicated], dtype=np.int64))
        n_other = pool - len(indicated)
        n_unscored_other = n_other - other_ranks.size
        wins = 0.0
        for d in indicated:
            k = rank_of.get(d)
            if k is None:
                wins += 0.5 * n_unscored_other
            else:
                wins += (other_ranks.size - np.searchsorted(other_ranks, k)) + n_unscored_other
        auroc = round(wins / (len(indicated) * n_other), 3)
    return {
        "known_indications": len(indicated),
        "known_indications_scored": len(known_ranks),
        "in_top_20": sum(1 for k in known_ranks if k <= 20),
        "in_top_100": sum(1 for k in known_ranks if k <= 100),
        "median_rank": int(np.median(known_ranks)) if known_ranks else None,
        "auroc_known_indications": auroc,
        "drugs_ranked": len(rank_of), "drug_pool": pool,
    }


def _drug_groups(kg: PrimeKG, drug: int) -> List[str]:
    text = str((kg.drug_features.get(drug) or {}).get("group") or "").lower()
    return [g for g in _GROUP_WORDS if re.search(rf"\b{g}\b", text)]


# -----------------------------------------------------------------------------
# repurposing: what a candidate is (diagnostic, nutrient, supportive care) and the indication proxy
# -----------------------------------------------------------------------------

# Imaging tracers, radiodiagnostics and contrast agents bind the disease's receptors (Ga-68 dotatate -> SSTR2) or
# accumulate in tumours, so the graph scores them like ligands; they are never treatments.
_DIAGNOSTIC_NAME_RE = re.compile(
    r"\bga(?:llium)?[\s\-]?68\b|\bgallium ga\b|\bf[\s\-]?18\b|\(18f\)|\bfluor\w*\s+f[\s\-]?18\b|\btc[\s\-]?99m\b|"
    r"\btechnetium\b|\bin[\s\-]?111\b|\bindium in\b|\bi[\s\-]?123\b|\brb[\s\-]?82\b|\bzr[\s\-]?89\b|\bcu[\s\-]?64\b|"
    r"\(11c\)|\bc[\s\-]11\b|\btl[\s\-]?201\b|\bxe[\s\-]?133\b|\bimaging\b|\bcontrast\b|\bgadolin\w*|^gado\w+|"
    r"\btide gallium\b|^io(?:hex|pam|vers|dix|prom|xil|xagl|trol|thalam|panoic|podic|fetamine|flupane)\w*",
    re.IGNORECASE,
)
_DIAGNOSTIC_CATEGORIES = frozenset({
    "diagnostic radiopharmaceuticals", "radioactive diagnostic agent", "contrast media", "x-ray contrast media, iodinated",
    "magnetic resonance imaging contrast media", "radiographic contrast agent", "ultrasound contrast media",
})
# Nutrients and supplements (vitamins, minerals, amino acids, herbal products): DrugBank lists them as "approved",
# and their many targets hit disease genes by chance. A drug that also carries a therapeutic ATC group (tretinoin,
# leucovorin, calcitriol) is kept.
_NUTRIENT_CATEGORIES = frozenset({
    "vitamins", "mineral supplements", "micronutrients", "dietary supplements", "supplements",
    "herbs and natural products", "allogeneic cultured cell scaffold",
})
_NUTRIENT_ATC = ("blood substitutes and perfusion solutions", "general nutrients", "mineral supplements", "vitamins",
                 "tonics")
_INORGANIC_SALT_RE = re.compile(
    r"^(?:sodium|potassium|calcium|magnesium|zinc|ferrous|ferric|iron|copper|cupric|ammonium|lithium|aluminium|"
    r"aluminum)\s+(?:chloride|sulfate|sulphate|carbonate|bicarbonate|phosphate|citrate|gluconate|lactate|acetate|"
    r"oxide|hydroxide|iodide|fluoride|nitrate)(?:,?\s+\w+)?$",
    re.IGNORECASE,
)
_ENDOGENOUS_NAMES = frozenset({"water", "oxygen", "d-glucose", "glucose", "sucrose", "atp", "nadh", "adenine",
                               "glycine", "glutathione", "l-glutamine", "carbon dioxide", "nitrogen"})
# Drug classes used for symptom control in cancer care, not against the disease: kept in place and tagged, so
# the reader sees why an antiemetic or cannabinoid scores (CNR1/CNR2 are disease genes) without it being hidden.
_SUPPORTIVE_CATEGORIES = frozenset({"cannabinoids and similars", "cannabinoid receptor agonists", "antiemetics and antinauseants",
                                    "hematopoietic cell growth factors", "appetite stimulants"})
_SUPPORTIVE_ATC = ("antiemetics and antinauseants", "immunostimulants, colony stimulating factors")
# Somatostatin-receptor ligands and other neuroendocrine-tumour / pituitary agents: in the graph they reach any
# tumour with SSTR genes, but they treat neuroendocrine tumours, acromegaly or Cushing disease.
_NET_TEXT_RE = re.compile(r"neuroendocrine|carcinoid|acromegal|somatostatin[\s\-]receptor|cushing|vipoma|gigantism|"
                          r"pituitary", re.IGNORECASE)
_NET_DISEASE_RE = re.compile(r"neuroendocrine|carcinoid|insulinoma|gastrinoma|glucagonoma|vipoma|somatostatinoma|"
                             r"islet|pituitar|acromegal|cushing|pheochromocytoma|paraganglioma", re.IGNORECASE)
NET_AGENT_FACTOR = 0.5          # a neuroendocrine/pituitary agent for a non-neuroendocrine disease keeps half


def _feature_categories(kg: PrimeKG, drug: int) -> Set[str]:
    feat = kg.drug_features.get(int(drug)) or {}
    cats = re.sub(r"^.*? is part of\s*", "", str(feat.get("category") or ""), flags=re.IGNORECASE).rstrip(". ")
    return {c.strip().lower() for c in cats.split(" ; ") if c.strip()}


def _atc_therapeutic(kg: PrimeKG, drug: int) -> str:
    m = re.search(r"therapeutic group of (.+)", str((kg.drug_features.get(int(drug)) or {}).get("atc_2") or ""),
                  flags=re.IGNORECASE)
    return m.group(1).strip().rstrip(".").lower() if m else ""


def drug_exclusion(kg: PrimeKG, drug: int) -> Optional[str]:
    """Why a drug is not a repurposing candidate at all (diagnostic / imaging agent, nutrient or supplement,
    inorganic salt, endogenous compound), or None. Uses the name and the DrugBank category and ATC fields."""
    name = kg.name(int(drug)).strip()
    cats = _feature_categories(kg, drug)
    atc = _atc_therapeutic(kg, drug)
    if _DIAGNOSTIC_NAME_RE.search(name) or cats & _DIAGNOSTIC_CATEGORIES:
        return "diagnostic or imaging agent"
    if atc and "therapeutic radiopharmaceuticals" not in atc and (
            "diagnostic radiopharmaceuticals" in atc or "contrast media" in atc or atc == "diagnostic agents"):
        return "diagnostic or imaging agent"
    if name.lower() in _ENDOGENOUS_NAMES:
        return "endogenous compound"
    if _INORGANIC_SALT_RE.match(name):
        return "inorganic salt / electrolyte"
    if "allogeneic cultured cell scaffold" in cats:
        return "cell / tissue product"
    only_nutrient_atc = bool(atc) and not re.sub(r"\band\b|[,\s]", "", _strip_all(atc, _NUTRIENT_ATC))
    if only_nutrient_atc or (cats & _NUTRIENT_CATEGORIES and (not atc or only_nutrient_atc)):
        return "nutrient or supplement"
    return None


def _strip_all(text: str, phrases: Iterable[str]) -> str:
    for p in phrases:
        text = text.replace(p, " ")
    return text


def drug_tags(kg: PrimeKG, drug: int) -> List[str]:
    """Labels a reader needs next to a candidate; currently 'supportive care' (antiemetics, cannabinoids,
    colony-stimulating factors)."""
    cats = _feature_categories(kg, drug)
    atc = _atc_therapeutic(kg, drug)
    if cats & _SUPPORTIVE_CATEGORIES or any(p in atc for p in _SUPPORTIVE_ATC):
        return ["supportive care"]
    return []


def _net_agent(kg: PrimeKG, drug: int, hits: Sequence[int]) -> bool:
    """A neuroendocrine-tumour / pituitary agent: its indications say so, or every disease gene it hits is a
    somatostatin receptor."""
    names = [kg.name(int(d)) for d in kg.neighbor_set(int(drug), ["indication"], node_type=DISEASE)]
    text = " ".join(names) + " " + str((kg.drug_features.get(int(drug)) or {}).get("indication") or "")
    if names and _NET_TEXT_RE.search(text):
        return True
    return bool(hits) and all(kg.name(int(g)).upper().startswith("SSTR") for g in hits)


def indication_proxy(kg: PrimeKG, disease: int, mentions: Sequence[str] = (), max_own: int = 1,
                     min_indications: int = 3) -> Optional[Dict[str, Any]]:
    """A closely related disease node whose indication edges stand in for a node that has none.

    PrimeKG often records a disease's approved drugs on a sibling or child grouping ("pancreatic adenocarcinoma"
    holds gemcitabine, erlotinib, irinotecan, 5-FU, mitomycin) while the ontology label a resolver picks
    ("exocrine pancreatic carcinoma", MONDO_0005192) has none, so its repurposing ranking cannot be calibrated and
    its profile reads 'no approved drugs'. Considered: direct disease_disease neighbours (MONDO parent/child) and
    exact-name matches of the user's own words; a neighbour must share a specific name word with the disease or the
    mention (so 'adenocarcinoma' or 'digestive system carcinoma' never stand in for a pancreatic disease). Returns
    None when the disease has more than max_own indications or nothing qualifies."""
    _require(kg, disease, DISEASE)
    own = int(kg.neighbor_set(disease, ["indication"], node_type=DRUG).size)
    if own > max_own:
        return None
    stems: Set[str] = set()
    for text in [kg.name(disease), *mentions]:
        stems |= {_stem(t) for t in normalize_name(text).split() if len(t) >= 4 and t not in _CONTEXT_STOP}
    exact: Set[int] = set()
    for m in mentions:
        for hit in kg.resolve(m, node_types=[DISEASE], limit=3, fuzzy=False):
            if hit.score >= 0.93 and hit.index != disease:
                exact.add(int(hit.index))
    pool = set(int(x) for x in kg.neighbor_set(disease, ["disease_disease"], node_type=DISEASE)) | exact
    best: Optional[Tuple[Tuple[int, int, int], int, int]] = None
    for c in pool:
        if c == disease:
            continue
        n_ind = int(kg.neighbor_set(c, ["indication"], node_type=DRUG).size)
        if n_ind < min_indications or n_ind <= own:
            continue
        shared = {_stem(t) for t in normalize_name(kg.name(c)).split() if len(t) >= 4} & stems
        if c not in exact and not shared:
            continue
        key = (int(c in exact), len(shared), n_ind)
        if best is None or key > best[0]:
            best = (key, c, n_ind)
    if best is None:
        return None
    _, c, n_ind = best
    how = "named in the question" if c in exact else "MONDO relative"
    return {**brief(kg, c), "indications": n_ind, "relation": how,
            "reason": f"{kg.name(disease)} has {'no' if own == 0 else 'only ' + str(own)} recorded "
                      f"indication{'s' if own != 1 else ''}"}


# Downstream effectors and direct regulators of frequently mutated oncogenes. PrimeKG's PPI layer lacks several of
# these edges (KRAS-MAP2K1, KRAS-PTPN11, KRAS-CDK4/6), so the RAS/RAF/EGFR/PI3K axes are written out; any other
# gene uses only the graph rule in pathway_neighbourhood().
_RAS_AXIS = ("RAF1", "BRAF", "ARAF", "MAP2K1", "MAP2K2", "MAPK1", "MAPK3", "PIK3CA", "PIK3CB", "PIK3CD", "PIK3R1",
             "AKT1", "AKT2", "AKT3", "MTOR", "PTPN11", "SOS1", "SOS2", "SHOC2", "CDK4", "CDK6", "RALGDS", "RAC1")
PATHWAY_AXES: Dict[str, Tuple[str, ...]] = {
    "KRAS": _RAS_AXIS, "NRAS": _RAS_AXIS, "HRAS": _RAS_AXIS,
    "BRAF": ("RAF1", "ARAF", "MAP2K1", "MAP2K2", "MAPK1", "MAPK3", "CDK4", "CDK6"),
    "EGFR": ("ERBB2", "ERBB3", "MET", "KRAS", "SOS1", "PTPN11", "PIK3CA", "AKT1", "MTOR", "MAP2K1", "MAP2K2", "MAPK1",
             "MAPK3"),
    "PIK3CA": ("PIK3CB", "PIK3CD", "PIK3R1", "AKT1", "AKT2", "AKT3", "MTOR", "PDPK1"),
}
PATHWAY_BOOST = {"core": 1.0, "interactor": 0.5}  # added to the score; components are 0-3 each before weighting


def pathway_neighbourhood(kg: PrimeKG, gene: int, max_pathway_size: int = 60, min_shared: int = 2,
                          hub_degree: int = 1000) -> Dict[int, str]:
    """{gene node: 'core' | 'interactor'} around a mutated gene. core = the gene and its curated downstream axis
    (PATHWAY_AXES); interactor = a PPI partner that also shares at least min_shared specific (<= max_pathway_size
    genes) Reactome pathways with it, i.e. a partner in the same signalling module rather than any binder; PPI
    hubs (UBC) are skipped."""
    _require(kg, gene, GENE)
    out: Dict[int, str] = {int(gene): "core"}
    for sym in PATHWAY_AXES.get(kg.name(gene).upper(), ()):
        for i in kg._by_id.get(f"SYMBOL_{sym}", []):
            out[int(i)] = "core"
    shared: Counter = Counter()
    for p in kg.neighbor_set(gene, ["pathway_protein"], node_type="pathway"):
        members = kg.neighbor_set(int(p), ["pathway_protein"], node_type=GENE)
        if members.size <= max_pathway_size:
            shared.update(int(m) for m in members)
    for g in kg.neighbor_set(gene, ["protein_protein"], node_type=GENE):
        g = int(g)
        if shared[g] >= min_shared and int(kg.degree[g]) <= hub_degree * 10 and                 kg.neighbor_set(g, ["protein_protein"]).size <= hub_degree:
            out.setdefault(g, "interactor")
    return out


def repurposing(kg: PrimeKG, disease: int, limit: int = 20, n_random: int = 200, seed: int = 0,
                weights: Optional[Dict[str, float]] = None, include_experimental: bool = False,
                indications_from: Optional[Sequence[int]] = None, mutant_genes: Optional[Sequence[int]] = None
                ) -> Dict[str, Any]:
    """Rank drugs for a disease from three independent PrimeKG signals; known indications are scored like any
    drug and reported as a calibration check, then excluded from the candidate list. Salt / ester forms of
    one parent drug are collapsed into one candidate.

    indications_from: related disease nodes (indication_proxy) whose indications and contraindications count as
    this disease's own, for calibration and exclusion; the graph signals still come from `disease`.
    mutant_genes: genes the question names as mutated ("KRAS-mutant"); candidates targeting their pathway
    neighbourhood are tagged and ranked up by PATHWAY_BOOST.

    Ranking (documented change, 2026-10-16): calibration ranks every drug by the unmodified three-component score,
    so the calibration and the benchmark (scripts/benchmark_primekg_repurposing.py) are unaffected. The candidate
    list then (1) drops diagnostic / imaging agents, nutrients, inorganic salts and endogenous compounds
    (drug_exclusion; reported in excluded_non_therapeutic, never silently), (2) tags supportive-care drugs
    (antiemetics, cannabinoids, colony-stimulating factors) without moving them, (3) multiplies neuroendocrine /
    pituitary agents (somatostatin analogues, Lu-177 dotatate) by NET_AGENT_FACTOR unless the disease itself is
    neuroendocrine, tagged, and (4) adds PATHWAY_BOOST['core'] (+1.0) when a drug targets the mutated gene or its
    curated downstream axis, else PATHWAY_BOOST['interactor'] (+0.5) for a same-module PPI partner, discounted for
    promiscuous drugs as target_overlap is. The base score's components are 0-3 each before weighting, so +1 moves
    a candidate up without outranking strong multi-signal evidence. Core-axis drugs with little disease signal
    (MEK, mTOR, CDK4/6, KRAS G12C inhibitors) are also listed separately in pathway_candidates."""
    sig = repurposing_signals(kg, disease, n_random=n_random, seed=seed)
    indicated = set(kg.neighbor_set(disease, ["indication"], node_type=DRUG).tolist())
    contraindicated = set(kg.neighbor_set(disease, ["contraindication"], node_type=DRUG).tolist())
    off_label = set(kg.neighbor_set(disease, ["off-label use"], node_type=DRUG).tolist())
    proxies = [int(p) for p in (indications_from or []) if int(p) != int(disease) and kg.type_of(int(p)) == DISEASE]
    borrowed: Set[int] = set()
    for p in proxies:
        ind_p = set(kg.neighbor_set(p, ["indication"], node_type=DRUG).tolist())
        borrowed |= ind_p - indicated
        indicated |= ind_p
        contraindicated |= set(kg.neighbor_set(p, ["contraindication"], node_type=DRUG).tolist())
        off_label |= set(kg.neighbor_set(p, ["off-label use"], node_type=DRUG).tolist())

    rows: List[Dict[str, Any]] = []
    for drug, f in sig["drugs"].items():
        comp = score_repurposing(f, weights)
        score = sum(comp.values())
        if score > 0:
            rows.append({"drug": drug, "score": score, "components": comp, **f})
    rows.sort(key=lambda r: (-r["score"], r["d_c"] if r["d_c"] is not None else 99.0, kg.name(r["drug"])))
    rank_of = {r["drug"]: k + 1 for k, r in enumerate(rows)}
    calibration = calibration_stats(rank_of, indicated, sig["pool"])

    # Pathway neighbourhood of the mutated gene(s): drug -> (level, pathway genes it targets).
    hood: Dict[int, str] = {}
    for g in mutant_genes or []:
        for k, lvl in pathway_neighbourhood(kg, int(g)).items():
            if hood.get(k) != "core":
                hood[k] = lvl
    pathway_hits: Dict[int, List[int]] = defaultdict(list)
    for g in hood:
        for d in kg.neighbor_set(g, ["drug_protein"], node_type=DRUG, displays=["target"]).tolist():
            pathway_hits[int(d)].append(g)
    if pathway_hits:
        scored = {r["drug"] for r in rows}
        for d in pathway_hits:
            if d not in scored:  # a pathway drug with no base signal still enters, on the boost alone
                f = sig["drugs"].get(d) or {"hits": [], "hit_weight": 0.0, "n_targets": 0, "d_c": None, "z": None,
                                            "n_ppi": 0, "sims": []}
                rows.append({"drug": d, "score": 0.0, "components": {"target_overlap": 0.0, "network_proximity": 0.0,
                                                                       "similar_disease_indication": 0.0}, **f})
    net_disease = bool(_NET_DISEASE_RE.search(kg.name(disease)))
    for r in rows:
        r["base_score"] = r["score"]
        tags = drug_tags(kg, r["drug"])
        factor = 1.0
        if not net_disease and _net_agent(kg, r["drug"], r["hits"]):
            tags.append("neuroendocrine-tumour agent")
            factor *= NET_AGENT_FACTOR
        boost = 0.0
        genes = pathway_hits.get(r["drug"])
        if genes:
            core = any(hood[g] == "core" for g in genes)
            # The same promiscuity discount as target_overlap: a 28-target drug (aspirin) touching MAPK1 is
            # not a MAPK-pathway drug.
            n_t = max(r["n_targets"], len(genes))
            boost = PATHWAY_BOOST["core" if core else "interactor"] / math.sqrt(max(1.0, n_t / 5.0))
            tags.append(f"{'/'.join(kg.name(int(m)) for m in mutant_genes or [])} pathway")
            r["pathway_targets"] = sorted({kg.name(g) for g in genes})
        r["tags"] = tags
        r["pathway_boost"] = boost
        r["score"] = r["score"] * factor + boost
    rows.sort(key=lambda r: (-r["score"], r["d_c"] if r["d_c"] is not None else 99.0, kg.name(r["drug"])))
    auc = calibration["auroc_known_indications"]
    calibration["signal"] = ("unknown (no known indications)" if auc is None or not indicated else
                             "high" if auc >= 0.85 else "moderate" if auc >= 0.7 else "low")
    calibration["note"] = ("Known indications are scored like any drug; when they rank high (AUROC well above 0.5) "
                           "these graph signals carry information for this disease. Few or none ranked means low "
                           "confidence in the candidates.")

    def explain(r: Dict[str, Any], variants: List[int]) -> Dict[str, Any]:
        drug = r["drug"]
        prof = parse_drug_features(kg.name(drug), kg.drug_features.get(drug, {}))
        out = {
            **brief(kg, drug),
            "score": round(r["score"], 3),
            "components": r["components"],
            "approval_groups": prof.get("groups"),
            "targets_total": r["n_targets"],
            "targets_hitting_disease_genes": [kg.name(g) for g in r["hits"][:10]],
            "network_proximity": None if r["z"] is None else {
                "closest_distance": round(r["d_c"], 3), "z_score": round(r["z"], 2), "targets_in_ppi": r["n_ppi"]},
            "indicated_for_similar": [{"disease": kg.name(d), "curie": brief(kg, d).get("curie"), "similarity": round(s, 3)}
                                      for d, s in r["sims"][:3]],
            "status_for_disease": ("contraindicated" if drug in contraindicated else "off-label use" if drug in off_label
                                   else "no recorded relation"),
            "mechanism_of_action": prof.get("mechanism_of_action"),
        }
        # The supporting evidence in words, listing only the components that scored, so a reader (or the
        # synthesis model) cannot attribute a signal the drug does not have.
        basis = []
        if r["components"]["target_overlap"] > 0:
            basis.append(f"targets disease gene(s) {', '.join(out['targets_hitting_disease_genes'][:5])}")
        if r["components"]["network_proximity"] > 0 and r["z"] is not None:
            basis.append(f"targets lie close to disease genes in the PPI network (z = {r['z']:.2f})")
        if r["components"]["similar_disease_indication"] > 0 and r["sims"]:
            d, s = r["sims"][0]
            basis.append(f"indicated for the similar disease {kg.name(d)} (similarity {s:.2f})")
        if r.get("pathway_targets"):
            basis.append(f"targets {', '.join(r['pathway_targets'][:4])} in the "
                         f"{'/'.join(kg.name(int(m)) for m in mutant_genes or [])} pathway neighbourhood")
        out["basis"] = "; ".join(basis)
        if r.get("tags"):
            out["tags"] = list(r["tags"])
        if r.get("pathway_targets"):
            out["pathway_targets"] = r["pathway_targets"][:10]
        if r.get("score") != r.get("base_score"):
            out["base_score"] = round(r["base_score"], 3)
        if variants:
            out["also_scored_forms"] = [kg.name(v) for v in variants][:8]
        return out

    # Candidates are existing drugs (approved or investigational somewhere) unless experimental compounds are
    # asked for; experimental-only tool compounds are scored but not listed.
    groups: Dict[str, Tuple[Dict[str, Any], List[int]]] = {}
    experimental_skipped = 0
    excluded: List[Dict[str, Any]] = []
    excluded_total = 0
    # Drugs aimed at the mutated gene's core axis (MEK, ERK, PI3K, mTOR, CDK4/6 inhibitors) often carry little
    # graph signal for the disease itself (MAP2K1 is no pancreatic-cancer gene in PrimeKG), so the modest boost
    # does not lift them into the main list; they are listed separately, ranked by the same final score.
    pathway_rows = [r for r in rows if r.get("pathway_targets") and r["drug"] not in indicated
                    and r["drug"] not in contraindicated
                    and any(hood.get(g) == "core" for g in pathway_hits.get(r["drug"], []))
                    # selective for the pathway: aspirin's MAPK1/3 among 28 targets does not qualify
                    and (r["n_targets"] <= 5 or len(pathway_hits[r["drug"]]) >= 0.25 * r["n_targets"])
                    and (include_experimental or {"approved", "investigational"} & set(_drug_groups(kg, r["drug"])))
                    and not drug_exclusion(kg, r["drug"])]
    for r in rows:
        if r["drug"] in indicated or r["drug"] in contraindicated:
            continue
        if len(groups) >= limit and r["score"] <= 0:
            break
        if not include_experimental and not {"approved", "investigational"} & set(_drug_groups(kg, r["drug"])):
            experimental_skipped += 1
            continue
        why = drug_exclusion(kg, r["drug"])
        if why:
            excluded_total += 1
            if len(excluded) < 15 and len(groups) < limit:  # the ones that would have been listed
                excluded.append({**brief(kg, r["drug"]), "reason": why, "score": round(r["score"], 3)})
            continue
        stem = _SALT_TAIL.sub("", kg.name(r["drug"])).lower()
        if stem in groups:
            groups[stem][1].append(r["drug"])
        elif len(groups) < limit:
            groups[stem] = (r, [])
    return {
        "disease": brief(kg, disease),
        "disease_genes_total": int(sig["disease_genes"].size),
        "genes_borrowed_from_related_diseases": [kg.name(d) for d in sig["expanded_from"]][:10] or None,
        "candidate_filter": "all drugs" if include_experimental else "approved or investigational drugs",
        "experimental_compounds_not_listed": experimental_skipped,
        "excluded_non_therapeutic": excluded,
        "excluded_non_therapeutic_total": excluded_total,
        "indications_borrowed_from": [
            {**brief(kg, p), "indications": int(kg.neighbor_set(p, ["indication"], node_type=DRUG).size)}
            for p in proxies] or None,
        "borrowed_indications": [brief(kg, d) for d in sorted(borrowed, key=lambda d: rank_of.get(d, 10**6))][:25] or None,
        "mutant_genes": [kg.name(int(g)) for g in mutant_genes or []] or None,
        "pathway_candidates": [explain(r, []) for r in pathway_rows
                               if r["drug"] not in {g[0]["drug"] for g in groups.values()}][:8] or None,
        "pathway_neighbourhood_size": len(hood) or None,
        "similar_diseases": [{**brief(kg, d), "similarity": round(s, 3)} for d, s in sig["similar"][:10]],
        "candidates": [explain(r, v) for r, v in groups.values()],
        "contraindicated_but_scored": [explain(r, []) for r in rows if r["drug"] in contraindicated][:10],
        "already_indicated": [brief(kg, d) | {"rank": rank_of.get(d)}
                              for d in sorted(indicated, key=lambda d: rank_of.get(d, 10**6))][:25],
        "calibration": calibration,
        "weights": {**REPURPOSING_WEIGHTS, **(weights or {})},
        "method": "Weighted sum of three components, each 0-3 before weighting: target_overlap = specificity-weighted count of disease genes the drug "
                  "targets (genes shared by many diseases weigh less), discounted for drugs with many targets; "
                  "network_proximity = -z/1.5 of the mean shortest PPI distance from the drug's targets to the nearest "
                  f"disease gene against {n_random} degree-matched random target sets (Guney et al. 2016); "
                  "similar_disease_indication = 3 x the similarity (shared genes and phenotypes) of the closest "
                  "disease the drug is indicated for. Calibration ranks every drug by this sum. The candidate list "
                  "then drops diagnostic/imaging agents, nutrients, salts and endogenous compounds, tags "
                  "supportive-care drugs, scales neuroendocrine/pituitary agents by "
                  f"{NET_AGENT_FACTOR} (tagged), and adds +{PATHWAY_BOOST['core']} for targets in a mutated gene's "
                  f"core pathway axis (+{PATHWAY_BOOST['interactor']} for a same-module PPI partner).",
        "caveat": "Hypothesis generation only: PrimeKG edges record association, not efficacy or safety in this disease.",
    }


# -----------------------------------------------------------------------------
# phenotype-driven differential diagnosis
# -----------------------------------------------------------------------------

def phenotype_diagnosis(kg: PrimeKG, present: Sequence[int], absent: Sequence[int] = (), limit: int = 15) -> Dict[str, Any]:
    if not present:
        raise PrimeKGError("at least one present phenotype is required", kind="invalid")
    for p in [*present, *absent]:
        _require(kg, p, PHENOTYPE)
    pos, d_rows, p_cols = kg.incidence(DISEASE, PHENOTYPE, ["disease_phenotype_positive"])
    neg, _, _ = kg.incidence(DISEASE, PHENOTYPE, ["disease_phenotype_negative"])
    n_dis = max(1, int((np.diff(pos.indptr) > 0).sum()))
    df = np.asarray(pos.sum(axis=0)).ravel()
    ic = np.log((n_dis + 1) / (df + 1))
    col_of = {int(c): k for k, c in enumerate(p_cols)}

    # Each query phenotype contributes its IC on an exact match, half the neighbour's IC through a
    # parent/child phenotype (an annotation one level more general or more specific).
    pheno_adj = kg.adjacency(relations=["phenotype_phenotype"])
    weights = np.zeros(p_cols.size)
    owner = np.full(p_cols.size, -1, dtype=np.int64)
    for q in present:
        c = col_of[int(q)]
        if ic[c] > weights[c]:
            weights[c], owner[c] = ic[c], int(q)
        for nb in pheno_adj.indices[pheno_adj.indptr[q]:pheno_adj.indptr[q + 1]]:
            k = col_of.get(int(nb))
            if k is not None and 0.5 * ic[k] > weights[k]:
                weights[k], owner[k] = 0.5 * ic[k], int(q)
    score = np.asarray(pos @ weights).ravel()
    penalty = np.zeros(d_rows.size)
    for q in absent:
        c = col_of[int(q)]
        penalty += np.asarray(pos[:, c].todense()).ravel() * ic[c]
    for q in present:
        c = col_of[int(q)]
        penalty += np.asarray(neg[:, c].todense()).ravel() * ic[c]
    # Breadth matters: a disease matching every query phenotype outranks one matching a single rare one.
    covered = np.zeros(d_rows.size)
    for q in present:
        cols_q = np.flatnonzero(owner == int(q))
        if cols_q.size:
            covered += (np.asarray(pos[:, cols_q].sum(axis=1)).ravel() > 0)
    final = (score - penalty) * (0.5 + 0.5 * covered / len(present))
    max_possible = float(sum(ic[col_of[int(q)]] for q in present))
    order = np.argsort(-final)
    results = []
    for r in order[: limit * 3]:
        if final[r] <= 0:
            break
        cols = pos.indices[pos.indptr[r]:pos.indptr[r + 1]]
        matched = sorted({int(owner[c]) for c in cols if weights[c] > 0 and owner[c] >= 0})
        d = int(d_rows[r])
        results.append({
            **brief(kg, d),
            "score": round(float(final[r]), 3),
            "relative_score": round(float(final[r]) / max_possible, 3) if max_possible else None,
            "matched_query_phenotypes": [kg.name(m) for m in matched],
            "coverage": f"{len(matched)}/{len(present)}",
            "disease_phenotypes_total": int(cols.size),
            "contradicted": round(float(penalty[r]), 3) or None,
            "top_genes": [kg.name(g) for g in _top_by_degree(kg, kg.neighbor_set(d, ["disease_protein"], node_type=GENE), 5)],
            "indicated_drugs": [kg.name(g) for g in _top_by_degree(kg, kg.neighbor_set(d, ["indication"], node_type=DRUG), 5)],
        })
        if len(results) >= limit:
            break
    return {
        "present": [brief(kg, p) | {"information_content": round(float(ic[col_of[int(p)]]), 2)} for p in present],
        "absent": [brief(kg, p) for p in absent],
        "diseases_with_phenotypes": n_dis,
        "ranking": results,
        "method": "Sum of information content (log inverse annotation frequency across diseases) of matched "
                  "phenotypes; half credit through a parent/child HPO term; IC subtracted for phenotypes stated "
                  "absent that the disease has, and for present ones the disease is annotated as lacking; the result is "
                  "scaled by 0.5 + 0.5 x the fraction of query phenotypes the disease matches.",
        "caveat": "A ranking of PrimeKG phenotype annotations, not a diagnosis; annotation depth varies by disease.",
    }


def _top_by_degree(kg: PrimeKG, idxs: np.ndarray, k: int) -> List[int]:
    idxs = np.asarray(idxs)
    return [int(i) for i in idxs[np.argsort(-kg.degree[idxs], kind="stable")][:k]]


# -----------------------------------------------------------------------------
# polypharmacy
# -----------------------------------------------------------------------------

def polypharmacy(kg: PrimeKG, drugs: Sequence[int], diseases: Sequence[int] = ()) -> Dict[str, Any]:
    drugs = list(dict.fromkeys(int(d) for d in drugs))
    if len(drugs) < 2:
        raise PrimeKGError("polypharmacy needs at least two drugs", kind="invalid")
    for d in drugs:
        _require(kg, d, DRUG)
    roles = {d: {role: set(kg.neighbor_set(d, ["drug_protein"], node_type=GENE, displays=[role]).tolist())
                 for role in DRUG_ROLES} for d in drugs}
    effects = {d: set(kg.neighbor_set(d, ["drug_effect"], node_type=PHENOTYPE).tolist()) for d in drugs}
    pairs = []
    for x in range(len(drugs)):
        for y in range(x + 1, len(drugs)):
            a, b = drugs[x], drugs[y]
            interaction = "synergistic interaction" in kg.edge_labels(a, b) or bool(
                [r for r, _ in kg.edge_relations(a, b) if r == "drug_drug"])
            shared = {role: sorted(kg.name(g) for g in roles[a][role] & roles[b][role]) for role in DRUG_ROLES}
            common_fx = effects[a] & effects[b]
            flags = []
            if interaction:
                flags.append("recorded drug-drug interaction")
            if shared["enzyme"]:
                flags.append(f"shared metabolising enzymes ({', '.join(shared['enzyme'][:6])})")
            if shared["transporter"]:
                flags.append(f"shared transporters ({', '.join(shared['transporter'][:6])})")
            if shared["target"]:
                flags.append(f"shared targets ({', '.join(shared['target'][:6])})")
            pairs.append({
                "drugs": [brief(kg, a), brief(kg, b)],
                "recorded_interaction": interaction,
                "shared": {k: v[:15] for k, v in shared.items()},
                "shared_side_effects_total": len(common_fx),
                "shared_side_effects": _ranked(kg, common_fx, 12),
                "flags": flags,
            })
    disease_checks = []
    for dis in diseases:
        _require(kg, dis, DISEASE)
        contra = set(kg.neighbor_set(dis, ["contraindication"], node_type=DRUG).tolist())
        hits = [kg.name(d) for d in drugs if d in contra]
        if hits:
            disease_checks.append({"disease": brief(kg, dis), "contraindicated_drugs": hits})
    return {
        "drugs": [brief(kg, d) for d in drugs],
        "pairs": sorted(pairs, key=lambda p: (-int(p["recorded_interaction"]), -len(p["flags"]))),
        "disease_contraindications": disease_checks,
        "caveat": "PrimeKG drug-drug edges come from DrugBank and carry no severity; shared enzymes indicate a "
                  "pharmacokinetic interaction mechanism to check, not a confirmed interaction.",
    }


# -----------------------------------------------------------------------------
# enrichment
# -----------------------------------------------------------------------------

ENRICHMENT_COLLECTIONS: Dict[str, Tuple[str, str, Optional[str]]] = {
    "pathway": ("pathway", "pathway_protein", None),
    "biological_process": ("biological_process", "bioprocess_protein", None),
    "molecular_function": ("molecular_function", "molfunc_protein", None),
    "cellular_component": ("cellular_component", "cellcomp_protein", None),
    "disease": ("disease", "disease_protein", None),
    "phenotype": (PHENOTYPE, "phenotype_protein", None),
    "anatomy": ("anatomy", "anatomy_protein_present", None),
    "drug": (DRUG, "drug_protein", "target"),
}


def _bh(p: np.ndarray) -> np.ndarray:
    n = p.size
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * n / np.arange(1, n + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.minimum(q, 1.0)
    return out


def enrichment(kg: PrimeKG, genes: Sequence[int], collections: Sequence[str] = ("pathway", "biological_process", "disease"),
               limit: int = 10, min_size: int = 3, max_size: int = 1500) -> Dict[str, Any]:
    genes = sorted(set(int(g) for g in genes))
    for g in genes:
        _require(kg, g, GENE)
    out: Dict[str, Any] = {"genes": [brief(kg, g) for g in genes], "collections": {}}
    for name in collections:
        if name not in ENRICHMENT_COLLECTIONS:
            raise PrimeKGError(f"unknown enrichment collection {name!r}; choose from {', '.join(ENRICHMENT_COLLECTIONS)}")
        term_type, relation, display = ENRICHMENT_COLLECTIONS[name]
        mat, term_idx, gene_idx = kg.incidence(term_type, GENE, [relation], displays=[display] if display else None)
        annotated = np.asarray(mat.sum(axis=0)).ravel() > 0
        universe = int(annotated.sum())
        col_of = {int(g): k for k, g in enumerate(gene_idx)}
        q_cols = [col_of[g] for g in genes if g in col_of and annotated[col_of[g]]]
        n = len(q_cols)
        if n < 2:
            out["collections"][name] = {"universe": universe, "query_annotated": n, "terms": []}
            continue
        sizes = np.asarray(mat.sum(axis=1)).ravel()
        sub = mat[:, q_cols]
        k = np.asarray(sub.sum(axis=1)).ravel()
        ok = (k >= 2) & (sizes >= min_size) & (sizes <= max_size)
        p = np.ones(sizes.size)
        p[ok] = hypergeom.sf(k[ok] - 1, universe, sizes[ok], n)
        tested = np.flatnonzero(ok)
        q = np.ones(sizes.size)
        q[tested] = _bh(p[tested])
        terms = []
        sub_csr = sub.tocsr()
        for t in tested[np.argsort(p[tested], kind="stable")][:limit]:
            members = sub_csr.indices[sub_csr.indptr[t]:sub_csr.indptr[t + 1]]
            terms.append({
                **brief(kg, int(term_idx[t])),
                "overlap": int(k[t]), "term_size": int(sizes[t]),
                "fold_enrichment": round(float(k[t] / n / (sizes[t] / universe)), 2),
                "p_value": float(f"{p[t]:.3g}"), "q_value": float(f"{q[t]:.3g}"),
                "genes": sorted(kg.name(int(gene_idx[q_cols[m]])) for m in members),
            })
        out["collections"][name] = {"universe": universe, "query_annotated": n, "terms_tested": int(tested.size),
                                    "terms": terms}
    out["method"] = "One-sided hypergeometric test per term against genes annotated in that collection; " \
                    "Benjamini-Hochberg q-values within each collection."
    return out


# -----------------------------------------------------------------------------
# similarity
# -----------------------------------------------------------------------------

def similar(kg: PrimeKG, i: int, limit: int = 10, exclude_relations: Iterable[str] = DENSE_RELATIONS) -> Dict[str, Any]:
    t = kg.type_of(i)
    adj = kg.adjacency(exclude=tuple(exclude_relations))
    peers = kg.nodes_of_type(t)
    row = adj[i]
    inter = np.asarray((adj[peers] @ row.T).todense()).ravel()
    deg = np.diff(adj.indptr)
    union = deg[peers] + deg[i] - inter
    with np.errstate(divide="ignore", invalid="ignore"):
        jac = np.where(union > 0, inter / union, 0.0)
    jac[peers == i] = 0
    top = np.argsort(-jac)[:limit]
    mine = set(row.indices.tolist())
    items = []
    for k in top:
        if jac[k] <= 0:
            break
        j = int(peers[k])
        shared = np.array(sorted(mine & set(adj.indices[adj.indptr[j]:adj.indptr[j + 1]].tolist())), dtype=np.int64)
        by_type = Counter(kg.type_of(int(s)) for s in shared)
        items.append({
            **brief(kg, j), "jaccard": round(float(jac[k]), 4), "shared_neighbors": int(inter[k]),
            "shared_by_type": dict(by_type.most_common()),
            "most_specific_shared": [kg.name(int(s)) for s in shared[np.argsort(kg.degree[shared])][:6]] if shared.size else [],
        })
    return {"node": brief(kg, i), "excluded_relations": sorted(exclude_relations), "similar": items}


def resolve_many(kg: PrimeKG, names: Iterable[str], node_types: Optional[Sequence[str]] = None,
                 min_score: float = 0.75) -> Tuple[List[int], List[Dict[str, Any]], List[str]]:
    """(indices, resolution records, unresolved names)."""
    idx, records, missing = [], [], []
    for name in names:
        matches = kg.resolve(name, node_types=node_types, limit=3)
        if matches and matches[0].score >= min_score:
            m = matches[0]
            idx.append(m.index)
            records.append({"query": name, **brief(kg, m.index), "score": m.score, "matched_on": m.matched_on,
                            "alternatives": [kg.name(a.index) for a in matches[1:]]})
        else:
            missing.append(name)
    return idx, records, missing


def unique_preserve(seq: Iterable[int]) -> List[int]:
    seen: Set[int] = set()
    out = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out
