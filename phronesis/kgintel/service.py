"""
String-in / JSON-out operations over PrimeKG, shared by the orchestrator tools, the Synapta LangChain tool
and the CLI. Names or identifiers are resolved here; every result carries the resolution records and the
store provenance. Errors are PrimeKGError(kind=not_built|not_found|invalid).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from . import analytics as an
from .graph import PrimeKG, PrimeKGError, canonical_type, get_primekg


def _one(kg: PrimeKG, query: str, node_type: Optional[str], records: List[Dict[str, Any]]) -> int:
    types = [canonical_type(node_type)] if node_type else None
    idx, recs, missing = an.resolve_many(kg, [query], node_types=types)  # type: ignore[arg-type]
    if missing:
        hint = kg.resolve(query, node_types=types, limit=3)  # type: ignore[arg-type]
        alt = f"; closest: {', '.join(kg.name(m.index) for m in hint)}" if hint else ""
        raise PrimeKGError(f"no PrimeKG {node_type or 'node'} matches {query!r}{alt}", kind="not_found")
    records.extend(recs)
    return idx[0]


def _many(kg: PrimeKG, queries: Sequence[str], node_type: str, records: List[Dict[str, Any]],
          unresolved: List[str]) -> List[int]:
    idx, recs, missing = an.resolve_many(kg, queries, node_types=[node_type])
    records.extend(recs)
    unresolved.extend(missing)
    return an.unique_preserve(idx)


def _wrap(kg: PrimeKG, op: str, data: Dict[str, Any], records: List[Dict[str, Any]],
          unresolved: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"operation": op, "resolved": records, "unresolved": unresolved or [], "result": data,
            "provenance": kg.provenance()}


def resolve(query: str, node_types: Optional[Sequence[str]] = None, limit: int = 5, kg: Optional[PrimeKG] = None) -> Dict[str, Any]:
    kg = kg or get_primekg()
    types = [canonical_type(t) for t in node_types] if node_types else None
    matches = kg.resolve(query, node_types=types, limit=limit)  # type: ignore[arg-type]
    data = {"query": query, "matches": [{**an.brief(kg, m.index), "score": m.score, "matched_on": m.matched_on,
                                         "degree": int(kg.degree[m.index])} for m in matches]}
    return _wrap(kg, "resolve", data, [])


def drug_profile(drug: str, limit: int = 25, kg: Optional[PrimeKG] = None) -> Dict[str, Any]:
    kg = kg or get_primekg()
    rec: List[Dict[str, Any]] = []
    return _wrap(kg, "drug_profile", an.drug_profile(kg, _one(kg, drug, "drug", rec), limit), rec)


def _optional_diseases(kg: PrimeKG, names: Optional[Sequence[str]]) -> List[int]:
    """Disease nodes for optional context names or ids (unresolvable ones are ignored)."""
    if not names:
        return []
    idx, _recs, _missing = an.resolve_many(kg, list(names), node_types=["disease"])
    return an.unique_preserve(idx)


def disease_profile(disease: str, limit: int = 25, indications_from: Optional[Sequence[str]] = None,
                    kg: Optional[PrimeKG] = None) -> Dict[str, Any]:
    """A disease's profile; indications_from names related diseases whose drugs are shown (labelled) when this
    node records no indications (see analytics.indication_proxy)."""
    kg = kg or get_primekg()
    rec: List[Dict[str, Any]] = []
    d = _one(kg, disease, "disease", rec)
    return _wrap(kg, "disease_profile", an.disease_profile(kg, d, limit, indications_from=_optional_diseases(
        kg, indications_from) or None), rec)


def gene_profile(gene: str, limit: int = 25, context_diseases: Optional[Sequence[str]] = None,
                 kg: Optional[PrimeKG] = None) -> Dict[str, Any]:
    """A gene's profile; context_diseases (names or ids of the question's diseases) rank related diseases and
    drugs first. Context names that do not resolve are ignored (the default degree ranking applies)."""
    kg = kg or get_primekg()
    rec: List[Dict[str, Any]] = []
    g = _one(kg, gene, "gene/protein", rec)
    context: List[int] = []
    if context_diseases:
        idx, _recs, _missing = an.resolve_many(kg, list(context_diseases), node_types=["disease"])
        context = an.unique_preserve(idx)
    return _wrap(kg, "gene_profile", an.gene_profile(kg, g, limit, context=context or None), rec)


def neighbors(entity: str, node_type: Optional[str] = None, relation: Optional[str] = None,
              neighbor_type: Optional[str] = None, limit: int = 50, kg: Optional[PrimeKG] = None) -> Dict[str, Any]:
    kg = kg or get_primekg()
    rec: List[Dict[str, Any]] = []
    i = _one(kg, entity, node_type, rec)
    return _wrap(kg, "neighbors", an.neighbors(kg, i, relation=relation, node_type=canonical_type(neighbor_type),
                                               limit=limit), rec)


def paths(source: str, target: str, source_type: Optional[str] = None, target_type: Optional[str] = None,
          max_hops: int = 3, limit: int = 10, include_drug_interactions: bool = False,
          kg: Optional[PrimeKG] = None) -> Dict[str, Any]:
    kg = kg or get_primekg()
    rec: List[Dict[str, Any]] = []
    a, b = _one(kg, source, source_type, rec), _one(kg, target, target_type, rec)
    exclude = () if include_drug_interactions else tuple(an.DENSE_RELATIONS)
    return _wrap(kg, "paths", an.mechanism_paths(kg, a, b, max_hops=max_hops, limit=limit, exclude_relations=exclude), rec)


def drug_disease(drug: str, disease: str, kg: Optional[PrimeKG] = None) -> Dict[str, Any]:
    kg = kg or get_primekg()
    rec: List[Dict[str, Any]] = []
    return _wrap(kg, "drug_disease", an.drug_disease(kg, _one(kg, drug, "drug", rec), _one(kg, disease, "disease", rec)), rec)


def repurposing(disease: str, limit: int = 20, include_experimental: bool = False,
                indications_from: Optional[Sequence[str]] = None, mutant_genes: Optional[Sequence[str]] = None,
                kg: Optional[PrimeKG] = None) -> Dict[str, Any]:
    """Repurposing hypotheses. indications_from: related diseases whose indications count as known for
    calibration and exclusion; mutant_genes: genes named as mutated in the question (pathway-targeting candidates
    are tagged and ranked up). Names that do not resolve are ignored."""
    kg = kg or get_primekg()
    rec: List[Dict[str, Any]] = []
    d = _one(kg, disease, "disease", rec)
    genes: List[int] = []
    if mutant_genes:
        idx, _recs, _missing = an.resolve_many(kg, list(mutant_genes), node_types=["gene/protein"])
        genes = an.unique_preserve(idx)
    return _wrap(kg, "repurposing", an.repurposing(
        kg, d, limit=limit, include_experimental=include_experimental,
        indications_from=_optional_diseases(kg, indications_from) or None, mutant_genes=genes or None), rec)


def phenotype_diagnosis(present: Sequence[str], absent: Sequence[str] = (), limit: int = 15,
                        kg: Optional[PrimeKG] = None) -> Dict[str, Any]:
    kg = kg or get_primekg()
    rec: List[Dict[str, Any]] = []
    missing: List[str] = []
    pos = _many(kg, present, "effect/phenotype", rec, missing)
    neg = _many(kg, absent, "effect/phenotype", rec, missing)
    if not pos:
        raise PrimeKGError(f"none of the phenotypes resolved to HPO terms in PrimeKG: {', '.join(present)}", kind="not_found")
    return _wrap(kg, "phenotype_diagnosis", an.phenotype_diagnosis(kg, pos, neg, limit=limit), rec, missing)


def polypharmacy(drugs: Sequence[str], diseases: Sequence[str] = (), kg: Optional[PrimeKG] = None) -> Dict[str, Any]:
    kg = kg or get_primekg()
    rec: List[Dict[str, Any]] = []
    missing: List[str] = []
    d_idx = _many(kg, drugs, "drug", rec, missing)
    dis_idx = _many(kg, diseases, "disease", rec, missing)
    if len(d_idx) < 2:
        raise PrimeKGError(f"fewer than two drugs resolved in PrimeKG (unresolved: {', '.join(missing) or 'none'})",
                           kind="not_found")
    return _wrap(kg, "polypharmacy", an.polypharmacy(kg, d_idx, dis_idx), rec, missing)


def enrichment(genes: Sequence[str], collections: Sequence[str] = ("pathway", "biological_process", "disease"),
               limit: int = 10, kg: Optional[PrimeKG] = None) -> Dict[str, Any]:
    kg = kg or get_primekg()
    rec: List[Dict[str, Any]] = []
    missing: List[str] = []
    g_idx = _many(kg, genes, "gene/protein", rec, missing)
    if len(g_idx) < 2:
        raise PrimeKGError("enrichment needs at least two genes that resolve in PrimeKG", kind="not_found")
    return _wrap(kg, "enrichment", an.enrichment(kg, g_idx, collections=collections, limit=limit), rec, missing)


def similar(entity: str, node_type: Optional[str] = None, limit: int = 10, kg: Optional[PrimeKG] = None) -> Dict[str, Any]:
    kg = kg or get_primekg()
    rec: List[Dict[str, Any]] = []
    return _wrap(kg, "similar", an.similar(kg, _one(kg, entity, node_type, rec), limit=limit), rec)


def stats(kg: Optional[PrimeKG] = None) -> Dict[str, Any]:
    kg = kg or get_primekg()
    return _wrap(kg, "stats", kg.stats(), [])


OPERATIONS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "resolve": resolve,
    "drug_profile": drug_profile,
    "disease_profile": disease_profile,
    "gene_profile": gene_profile,
    "neighbors": neighbors,
    "paths": paths,
    "drug_disease": drug_disease,
    "repurposing": repurposing,
    "phenotype_diagnosis": phenotype_diagnosis,
    "polypharmacy": polypharmacy,
    "enrichment": enrichment,
    "similar": similar,
    "stats": stats,
}


def run(operation: str, **kwargs: Any) -> Dict[str, Any]:
    fn = OPERATIONS.get(operation)
    if fn is None:
        raise PrimeKGError(f"unknown PrimeKG operation {operation!r}; choose from {', '.join(OPERATIONS)}")
    return fn(**{k: v for k, v in kwargs.items() if v is not None})
