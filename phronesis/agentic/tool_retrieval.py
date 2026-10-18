"""
Tool retrieval: choose the few registry tools a planner should see for a (sub-)question.

- Each tool document is its name, description, domain, tags and argument names, expanded at index time with
  synthetic intent phrases per domain and per known tool (Re-Invoke, Chen et al., Findings EMNLP 2024,
  arXiv:2408.01875; Toolshed's pre-retrieval tool enrichment, Lumer et al. 2024, arXiv:2410.14594).
- The query is expanded with a small biomedical intent lexicon before ranking (Toolshed's intra-retrieval
  query rewriting and expansion), with the expansion terms weighted below the user's own words.
- Ranking is BM25 over two views of each tool (the full document, and an "intent view" of name, tags and
  synthetic queries only), fused by reciprocal rank with an optional dense ranking from a locally cached
  sentence-transformers model (PHRONESIS_TOOL_EMBEDDINGS=<model id>; never downloaded at query time). This is
  the API-retriever step of ToolLLM (Qin et al., ICLR 2024, arXiv:2307.16789) and the Tool Finder of
  ToolUniverse (Gao et al. 2025, arXiv:2509.23426), done lexically: ToolRet (Shi et al., ACL 2025,
  arXiv:2503.01763) shows generic embedders are weak on tool retrieval, so the lexical ranking is the default
  and recall@k is measured on our own catalogue by tests/test_tool_retrieval.py.
- tool_card() is the compact, schema-accurate description shown to planners (Gorilla, Patil et al.,
  NeurIPS 2024, arXiv:2305.15334: ground calls in the tool's own documentation).

Public API (used by other packages; keep stable): retrieve_tools, tool_card, tool_document, recall_at_k,
tokenize, DOMAIN_INTENTS.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from has have in into is it its of on or that the to was were will with what which "
    "how does do can this these those via using use".split()
)

# Domain intent phrases used to expand tool documents (synthetic queries per domain).
DOMAIN_INTENTS: Dict[str, str] = {
    "synapta": "target disease association gene drug target pathway knowledge graph opentargets kegg network",
    "vivarix": "simulate kinetic ode model biomodels sbml time course steady state parameter scan dynamics concentration",
    "lumicell": "single cell rna seq clustering marker genes expression cell type atlas h5ad scanpy",
    "lithoscribe": "literature papers publications evidence pubmed europepmc abstract clinical trials review",
    "websurfer": "web search news recent online sources",
    "structure": "protein structure alphafold pdb plddt docking ligand binding pose molecule generation",
    "omics": "gwas variant association trait expression tissue gtex protein interaction string reactome pathway "
    "compound chemistry pubchem preprint biorxiv",
    "analysis": "analyse data statistics code python dataframe plot differential expression survival regression",
    "general": "",
}

# Synthetic queries per known tool (Re-Invoke: written once at index time, not per request). Tools not listed
# here are still indexed from their own description, tags and argument names.
TOOL_INTENTS: Dict[str, str] = {
    "synapta_opentargets_search": "find the ensembl id of a gene look up a target by name search opentargets entity",
    "synapta_opentargets_associations": "which diseases is this gene associated with target disease association "
    "scores genetic evidence for a target",
    "synapta_kegg_pathways": "which pathways is this gene in kegg pathway membership signaling pathway of a gene",
    "synapta_resolve_entities": "normalize gene drug disease variant names resolve synonyms aliases to identifiers hgnc",
    "synapta_variant_intelligence": "is this mutation pathogenic clinvar classification oncogenic variant civic "
    "cbioportal frequency gnomad biomarker mutation",
    "vivarix_simulate_time_course": "simulate the model over time kinetic ode time course trajectory concentration "
    "dynamics biomodels sbml",
    "vivarix_compute_steady_state": "steady state concentrations equilibrium fluxes of the model",
    "vivarix_parameter_scan": "vary a parameter sensitivity analysis dose response sweep parameter scan knockdown "
    "overexpression perturbation",
    "vivarix_calibrate_parameters": "fit model parameters to data calibrate estimate parameters identifiability "
    "profile likelihood experimental time series",
    "lumicell_analyze_single_cell": "analyse single cell rna seq dataset clusters marker genes cell types h5ad",
    "lithoscribe_literature_synthesis": "find papers publications literature evidence studies pubmed abstracts "
    "citations review what is known",
    "lithoscribe_clinical_trials_search": "clinical trials registry recruiting trials nct phase enrollment results "
    "clinicaltrials gov studies of a drug in a condition",
    "websurfer_search": "search the web news recent online pages websites",
    "chembl_resolve_drugs": "resolve a drug name trade name to chembl id parent molecule compound identity",
    "chembl_drug_profile": "drug mechanism of action potency ic50 ki bioactivity against a target approved "
    "indications inhibitor",
    "openfda_drug_safety": "drug side effects adverse events safety toxicity boxed warning contraindications faers label "
    "fda approval approved indications regulatory status drugs@fda nda bla prescribing information",
    "opentargets_disease_targets": "which targets are associated with a disease druggable targets tractability "
    "drugs in development for a disease",
    "primekg_investigate": "knowledge graph analysis drug repurposing mechanism paths between drug and disease "
    "phenotypes indications contraindications",
    "primekg_query": "knowledge graph lookup neighbors profile of a drug disease gene in primekg",
    "analysis_run_python": "analyse the attached data with code statistics regression plot differential "
    "expression python table csv",
}

# Query expansion lexicon (Toolshed's query rewriting): a query word -> tool-vocabulary terms. Keys are prefixes
# of the raw query word; values are added to the query at a lower weight.
QUERY_EXPANSIONS: Dict[str, str] = {
    "paper": "literature europepmc publications",
    "publication": "literature europepmc",
    "article": "literature europepmc",
    "study": "literature publications",
    "evidence": "literature",
    "review": "literature",
    "pubmed": "literature europepmc",
    "trial": "clinical_trials clinicaltrials nct",
    "nct": "clinical_trials",
    "recruit": "clinical_trials",
    "side": "safety adverse",
    "adverse": "safety faers",
    "safety": "faers adverse",
    "toxic": "safety adverse",
    "warning": "safety label",
    "contraindication": "safety label",
    "fda": "fda approval label regulatory",
    "approv": "fda approval label",
    "regulator": "fda approval regulatory",
    "indication": "label approval indications",
    "drug": "drugs chembl",
    "compound": "drugs chembl",
    "inhibitor": "drugs chembl mechanism",
    "potency": "bioactivity ic50 chembl",
    "ic50": "bioactivity potency chembl",
    "mutation": "variant clinvar",
    "variant": "clinvar mutation",
    "snp": "variant",
    "pathogenic": "variant clinvar",
    "simulat": "simulation ode kinetics time course",
    "dynamic": "simulation kinetics",
    "kinetic": "simulation ode",
    "steady": "steady_state",
    "scan": "parameter_scan sensitivity",
    "sensitivity": "parameter_scan",
    "sweep": "parameter_scan",
    "perturb": "parameter_scan",
    "knockdown": "parameter_scan perturbation",
    "calibrat": "calibration parameter estimation fit",
    "fit": "calibration",
    "single": "single_cell",
    "scrna": "single_cell",
    "cluster": "single_cell clustering",
    "marker": "single_cell markers",
    "pathway": "pathways kegg",
    "signal": "pathways",
    "web": "web search",
    "news": "web",
    "online": "web",
    "repurpos": "primekg knowledge_graph repurposing",
    "graph": "knowledge_graph primekg",
    "phenotype": "primekg phenotypes",
    "associat": "associations",
    "disease": "diseases",
    "druggab": "tractability targets",
    "tractab": "targets",
    "synonym": "normalization resolve",
    "alias": "normalization resolve",
    "identifier": "resolve",
    "structur": "structure alphafold pdb",
    "fold": "alphafold structure",
    "dock": "docking ligand",
    "ligand": "docking",
    "gwas": "gwas trait",
    "tissue": "gtex expression",
    "interact": "string interaction network",
    "partner": "interaction",
    "preprint": "biorxiv",
    "analys": "analysis",
    "statistic": "analysis",
    "regression": "analysis",
    "csv": "analysis",
}

_EXPANSION_WEIGHT = 0.5


def tokenize(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP and len(t) > 1]


_STEM_LEN = 6


def _stem(token: str) -> str:
    """Plural stripping plus truncation stemming (first 6 characters), so 'papers'/'paper',
    'simulate'/'simulation' and 'associated'/'association' share a term."""
    if token.isdigit():
        return token
    if len(token) > 4 and token.endswith("ies"):
        token = token[:-3] + "y"
    elif len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "is", "us")):
        token = token[:-1]
    return token[:_STEM_LEN]


def _terms(text: str) -> List[str]:
    return [_stem(t) for t in tokenize((text or "").replace("_", " "))]


def _schema_fields(entry: Any) -> List[Tuple[str, str, bool, Any, str]]:
    """(name, type, required, default, description) for the tool's input schema."""
    out: List[Tuple[str, str, bool, Any, str]] = []
    schema = getattr(entry, "input_schema", None)
    fields = getattr(schema, "model_fields", {}) or {}
    for name, f in fields.items():
        ann = getattr(f, "annotation", None)
        tname = getattr(ann, "__name__", None) or str(ann).replace("typing.", "")
        required = bool(f.is_required()) if hasattr(f, "is_required") else False
        default = None if required else getattr(f, "default", None)
        try:
            json.dumps(default)
        except (TypeError, ValueError):
            default = str(default)
        out.append((name, tname, required, default, str(getattr(f, "description", "") or "")))
    return out


def _model_of(ann: Any) -> Any:
    """The pydantic model inside an annotation (Optional[X], List[X]) or None."""
    if hasattr(ann, "model_fields"):
        return ann
    for arg in getattr(ann, "__args__", ()) or ():
        m = _model_of(arg)
        if m is not None:
            return m
    return None


def output_fields(entry: Any, max_fields: int = 10, max_nested: int = 6) -> List[str]:
    """Reference paths a plan can use into this tool's result data: ["drugs[*].chembl_id", "drugs[*].pref_name", ...].

    Without them the planner guesses ("$n1.results") and the reference fails at run time.
    """
    schema = getattr(entry, "output_schema", None)
    fields = getattr(schema, "model_fields", {}) or {}
    out: List[str] = []
    for name, f in list(fields.items())[:max_fields]:
        ann = getattr(f, "annotation", None)
        is_list = "List" in str(ann) or "list[" in str(ann)
        nested = _model_of(ann)
        sub = list(getattr(nested, "model_fields", {}) or {})[:max_nested] if nested is not None else []
        if sub:
            out.extend(f"{name}{'[*]' if is_list else ''}.{s}" for s in sub)
        else:
            out.append(f"{name}{'[*]' if is_list else ''}")
    return out


def tool_card(entry: Any) -> Dict[str, Any]:
    """Compact planner-facing description: name, purpose, domain, exact argument schema and output fields."""
    args = {
        name: {"type": t, "required": req, **({"default": d} if not req and d is not None else {}), **({"description": desc[:160]} if desc else {})}
        for name, t, req, d, desc in _schema_fields(entry)
    }
    return {
        "name": entry.name,
        "domain": entry.domain,
        "description": (entry.description or "")[:400],
        "args": args,
        **({"returns": returns} if (returns := output_fields(entry)) else {}),
        "timeout_seconds": entry.timeout_seconds,
    }


def tool_document(entry: Any) -> str:
    fields = " ".join(f"{n} {d}" for n, _t, _r, _dv, d in _schema_fields(entry))
    name_words = entry.name.replace("_", " ")
    return " ".join(
        [
            name_words,
            name_words,  # the name counts twice: it is the most specific signal
            entry.description or "",
            entry.domain or "",
            " ".join(entry.tags or []),
            fields,
            DOMAIN_INTENTS.get((entry.domain or "").lower(), ""),
            TOOL_INTENTS.get(entry.name, ""),
        ]
    )


def _intent_view(entry: Any) -> str:
    """Name, tags and synthetic queries only: the Re-Invoke 'intent' index, free of schema boilerplate."""
    return " ".join(
        [
            entry.name.replace("_", " "),
            " ".join(t.replace("_", " ") for t in (entry.tags or [])),
            TOOL_INTENTS.get(entry.name, ""),
            (entry.description or "")[:200],
        ]
    )


class _BM25:
    def __init__(self, docs: Sequence[List[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.docs = docs
        self.k1, self.b = k1, b
        self.avgdl = sum(len(d) for d in docs) / max(1, len(docs))
        df: Counter = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.tf = [Counter(d) for d in docs]

    def score(self, query: List[str], i: int, weights: Optional[Dict[str, float]] = None) -> float:
        tf, dl = self.tf[i], len(self.docs[i])
        s = 0.0
        for t in query:
            if t not in tf:
                continue
            f = tf[t]
            w = (weights or {}).get(t, 1.0)
            s += w * self.idf.get(t, 0.0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / max(1e-9, self.avgdl)))
        return s


def expand_query(query: str) -> Tuple[List[str], Dict[str, float]]:
    """Stemmed query terms plus lexicon expansions, and the weight of each term (expansions weigh less)."""
    raw = tokenize((query or "").replace("_", " "))
    weights: Dict[str, float] = {_stem(t): 1.0 for t in raw}
    for t in raw:
        for key, extra in QUERY_EXPANSIONS.items():
            if t.startswith(key):
                for e in _terms(extra):
                    weights.setdefault(e, _EXPANSION_WEIGHT)
    return list(weights), weights


_EMBEDDER: Any = None
_EMBEDDER_FAILED = False


def _embedder() -> Any:
    global _EMBEDDER, _EMBEDDER_FAILED
    model_id = os.getenv("PHRONESIS_TOOL_EMBEDDINGS", "").strip()
    if not model_id or _EMBEDDER_FAILED:
        return None
    if _EMBEDDER is None:
        try:
            from sentence_transformers import SentenceTransformer

            _EMBEDDER = SentenceTransformer(model_id, local_files_only=True)  # type: ignore[call-arg]
        except Exception as exc:  # noqa: BLE001
            _EMBEDDER_FAILED = True
            logger.info("Tool embeddings unavailable (%s); using BM25 only", exc)
            return None
    return _EMBEDDER


def _dense_ranking(query: str, docs: List[str]) -> Optional[List[int]]:
    model = _embedder()
    if model is None:
        return None
    try:
        import numpy as np

        vecs = model.encode([query, *docs], normalize_embeddings=True)
        sims = np.asarray(vecs[1:]) @ np.asarray(vecs[0])
        return [int(i) for i in np.argsort(-sims)]
    except Exception as exc:  # noqa: BLE001
        logger.info("Dense tool ranking failed: %s", exc)
        return None


def rank_tools(registry: Any, query: str, entries: Optional[List[Any]] = None) -> List[Tuple[Any, float]]:
    """Every candidate entry with its fused score, best first (used by retrieve_tools)."""
    entries = list(entries if entries is not None else registry.list_tools())
    if not entries:
        return []
    docs = [tool_document(e) for e in entries]
    q, weights = expand_query(query)
    full = _BM25([_terms(d) for d in docs])
    intent = _BM25([_terms(_intent_view(e)) for e in entries])
    full_scores = [full.score(q, i, weights) for i in range(len(entries))]
    intent_scores = [intent.score(q, i, weights) for i in range(len(entries))]
    rankings = [
        sorted(range(len(entries)), key=lambda i: (-full_scores[i], i)),
        sorted(range(len(entries)), key=lambda i: (-intent_scores[i], i)),
    ]
    dense = _dense_ranking(query, docs)
    if dense is not None:
        rankings.append(dense)
    fused: Dict[int, float] = {}
    for ranking in rankings:
        for rank, i in enumerate(ranking):
            fused[i] = fused.get(i, 0.0) + 1.0 / (60 + rank + 1)
    # Post-retrieval rerank (Toolshed): a tool whose name words all occur in the query is a direct mention.
    qset = set(q)
    for i, e in enumerate(entries):
        name_terms = set(_terms(e.name))
        if name_terms and name_terms <= qset:
            fused[i] += 0.01
        if full_scores[i] <= 0 and intent_scores[i] <= 0:
            fused[i] -= 0.005  # no lexical overlap at all: rank after every matching tool
    order = sorted(fused, key=lambda i: (-fused[i], i))
    return [(entries[i], fused[i]) for i in order]


def retrieve_tools(
    registry: Any,
    query: str,
    k: int = 8,
    domains: Optional[Iterable[str]] = None,
    exclude: Optional[Iterable[str]] = None,
    always: Optional[Iterable[str]] = None,
) -> List[Any]:
    """Top-k registry entries for query (multi-view BM25 with query expansion, RRF-fused with a dense ranking
    when configured).

    `always` names are included first when registered (e.g. the literature tool for any research question).
    """
    excluded = set(exclude or [])
    wanted = {d.lower() for d in domains} if domains else None
    entries = [
        e for e in registry.list_tools()
        if e.name not in excluded and (wanted is None or (e.domain or "").lower() in wanted)
    ]
    if not entries:
        return []
    ranked = rank_tools(registry, query, entries)
    picked: List[Any] = []
    by_name = {e.name: e for e in entries}
    for name in always or []:
        if name in by_name and by_name[name] not in picked:
            picked.append(by_name[name])
    for entry, _score in ranked:
        if k > 0 and len(picked) >= k:
            break
        if entry not in picked:
            picked.append(entry)
    return picked[:k] if k > 0 else picked


def recall_at_k(registry: Any, cases: Sequence[Tuple[str, str]], k: int = 5) -> float:
    """Fraction of (query, expected_tool) cases whose tool is in the top k (ToolRet's recall@k)."""
    if not cases:
        return 0.0
    hits = sum(1 for q, tool in cases if tool in {e.name for e in retrieve_tools(registry, q, k=k)})
    return hits / len(cases)
