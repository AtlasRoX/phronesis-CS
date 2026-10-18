"""
Query-run repair WS-1 (entity side): generic biochemicals are not drugs ("receptor tyrosine kinase" is not
TYROSINE), acronyms the question defines as biomarkers are not genes ("(HRD)" is not TBCE), endpoints are not
diseases ("overall survival"), and coordinated disease phrases ("ovarian and prostate cancers") yield one disease
per site. Upstreams are replayed through httpx.MockTransport (tests.test_entity_resolver.Upstream). No network.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from phronesis.agents import query_planner as qp
from phronesis.agents.entity_lexicon import coordinated_disease_terms, defined_acronyms
from phronesis.utils.biocontext.entity_resolver import EntityResolver
from tests.fakes import build_recording_registry
from tests.test_entity_resolver import Upstream, _by_mention, _resolve_question, _resolver
from tests.test_query_planner import _ctx, _resolver_tool


def _hit(id_: str, name: str, entity: str, score: float = 1.0) -> Dict[str, Any]:
    return {"id": id_, "name": name, "entity": entity, "score": score}


def _hgnc_doc(symbol: str, hgnc_id: str, ensembl: str, **extra: Any) -> Dict[str, Any]:
    doc = {"symbol": symbol, "hgnc_id": hgnc_id, "ensembl_gene_id": ensembl, **extra}
    return {"response": {"numFound": 1, "docs": [doc]}}


_EMPTY_HGNC = {"response": {"numFound": 0, "docs": []}}

TYROSINE = [_hit("CHEMBL925", "TYROSINE", "drug")]
MRTX = [_hit("CHEMBL5081048", "MRTX1133", "drug")]
OVARIAN_CANCER = _hit("MONDO_0008170", "ovarian cancer", "disease")
PROSTATE_CANCER = _hit("MONDO_0008315", "prostate cancer", "disease")
OVERALL_SURVIVAL = [_hit("EFO_0000638", "overall survival", "disease")]


class RunUpstream(Upstream):
    """The recorded fixtures plus the mapIds and HGNC answers seen in the 2026-10-16 runs."""

    def __init__(self) -> None:
        super().__init__()
        self.ot.update({
            "tyrosine": TYROSINE,
            "MRTX1133": MRTX,
            "overall survival": OVERALL_SURVIVAL,
            "ovarian cancer": [OVARIAN_CANCER],
            "ovarian": [_hit("EFO_0003840", "ovarian disease", "disease")],
            "prostate cancer": [PROSTATE_CANCER],
            "prostate cancers": [PROSTATE_CANCER],
            "HRD": [_hit("ENSG00000116957", "TBCE", "target"), _hit("MONDO_0009461", "HRD syndrome", "disease")],
            "olaparib": [_hit("CHEMBL521686", "OLAPARIB", "drug")],
        })
        tbce = _hgnc_doc("TBCE", "HGNC:11582", "ENSG00000116957", alias_symbol=["HRD", "KCS"])
        self.hgnc.update({
            "symbol/HRD": _EMPTY_HGNC, "alias_symbol/HRD": tbce,
            "symbol/RSD": _EMPTY_HGNC, "alias_symbol/RSD": _hgnc_doc("TBCE", "HGNC:11582", "ENSG00000116957"),
            "symbol/WT1": _hgnc_doc("WT1", "HGNC:12796", "ENSG00000184937"), "alias_symbol/WT1": _EMPTY_HGNC,
            "symbol/PARP": _EMPTY_HGNC, "alias_symbol/PARP": _EMPTY_HGNC, "prev_symbol/PARP": _EMPTY_HGNC,
        })


PDAC_Q = (
    "Which receptor tyrosine kinase bypass tracks drive resistance to MRTX1133 in KRAS G12D pancreatic cancer?"
)
HRD_Q = (
    "How effective are PARP inhibitors such as olaparib in homologous recombination repair-deficient (HRD) "
    "ovarian and prostate cancers, and what overall survival benefit was reported?"
)


def _types(out: Dict[str, Any], kind: str) -> List[str]:
    return [e["label"] for e in out["entities"] if e["type"] == kind]


# =============================================================================
# 1.1 Generic biochemicals are not drugs
# =============================================================================

@pytest.mark.asyncio
async def test_tyrosine_in_receptor_tyrosine_kinase_is_not_a_drug_but_mrtx1133_is():
    out = await _resolve_question(_resolver(RunUpstream()), PDAC_Q)
    drugs = _types(out, "drug")
    assert "TYROSINE" not in drugs
    assert "MRTX1133" in drugs
    assert _by_mention(out)["MRTX1133"]["ids"] == {"chembl_id": "CHEMBL5081048"}


@pytest.mark.parametrize(
    "query, term, accepted",
    [
        ("Role of tyrosine in melanin synthesis", "tyrosine", False),  # generic biochemical, any context
        ("serine/threonine kinase signaling", "serine", False),
        ("Is zinc supplementation useful?", "zinc", False),
        ("adenosine receptor signaling in the tumor microenvironment", "adenosine", False),
        ("Is adenosine infusion safe for SVT?", "adenosine", True),  # a real drug in a therapeutic context
        ("insulin receptor isoforms in breast cancer", "insulin", False),
        ("Does insulin therapy raise cancer risk?", "insulin", True),
        ("vitamin D levels in colorectal cancer", "vitamin D", False),
        ("high-dose vitamin D supplementation in colorectal cancer", "vitamin D", True),
        ("imatinib resistance in GIST", "imatinib", True),
        ("Olaparib in ovarian cancer", "olaparib", True),
    ],
)
def test_biochemical_stoplist_and_phrase_context(query, term, accepted):
    hits = [_hit("CHEMBL1", term.upper(), "drug")]
    assert (EntityResolver._accept_drug(query, term, hits) is not None) is accepted


# =============================================================================
# 1.2 Acronyms defined as biomarkers are not genes
# =============================================================================

def test_defined_acronyms_classify_long_forms():
    assert defined_acronyms("homologous recombination repair-deficient (HRD) tumours") == {"HRD": "concept"}
    assert defined_acronyms("tumor mutational burden (TMB)") == {"TMB": "concept"}
    assert defined_acronyms("mismatch repair deficient (dMMR)") == {"DMMR": "concept"}
    assert defined_acronyms("microsatellite instability-high (MSI-H)")["MSI"] == "concept"
    # Gene and protein long forms, and lexicon symbols, are never listed.
    assert defined_acronyms("epidermal growth factor receptor (EGFR) mutations") == {}
    assert defined_acronyms("B7 homolog 3 (B7-H3)") == {}
    assert defined_acronyms("Wilms tumor 1 (WT1) expression") == {"WT1": "name"}


def test_hrd_is_not_a_gene_candidate():
    cands = qp.extract_candidates(HRD_Q)
    assert "HRD" not in cands.gene_tokens
    # Even an acronym missing from NON_GENE_ACRONYMS is dropped when the question defines it as a concept.
    cands = qp.extract_candidates("Is replication stress deficiency (RSD) targetable?")
    assert "RSD" not in cands.gene_tokens


@pytest.mark.asyncio
async def test_defined_acronym_alias_hit_is_rejected_but_approved_symbol_is_kept():
    res = _resolver(RunUpstream())
    q = "Is replication stress deficiency (RSD) targetable?"
    out = await res.resolve(q, ["RSD"], ["RSD"])
    assert "TBCE" not in _types(out, "gene")
    out = await res.resolve(q.replace("RSD", "HRD").replace("replication stress", "homologous recombination"), ["HRD"], ["HRD"])
    assert "TBCE" not in _types(out, "gene") and "HRD syndrome" not in _types(out, "disease")
    # A gene the question spells out ("Wilms tumor 1 (WT1)") still resolves through its approved symbol.
    out = await res.resolve("Wilms tumor 1 (WT1) expression in AML", ["WT1"], ["WT1"])
    assert "WT1" in _types(out, "gene")


# =============================================================================
# 1.3 Endpoints are not diseases
# =============================================================================

@pytest.mark.parametrize(
    "term",
    ["overall survival", "progression-free survival", "disease-free survival", "event-free survival",
     "objective response rate", "response rate", "hazard ratio", "adverse events", "quality of life",
     "time to progression", "OS", "PFS", "HRD"],
)
def test_endpoints_are_not_diseases(term):
    hits = [_hit("EFO_0000638", term, "disease"), _hit("MONDO_0009807", "osteosarcoma", "disease")]
    assert EntityResolver._accept_disease(term, hits) is None


def test_real_diseases_still_accepted():
    assert EntityResolver._accept_disease("ovarian cancer", [OVARIAN_CANCER]) == OVARIAN_CANCER
    imm = _hit("EFO_0000001", "immune-related adverse event", "disease")
    assert EntityResolver._accept_disease("immune-related adverse event", [imm]) == imm


# =============================================================================
# 1.4 Coordinated disease phrases
# =============================================================================

def test_coordinated_disease_phrases_expand_to_one_term_per_site():
    terms = [t for t, _m, _s in coordinated_disease_terms(HRD_Q)]
    assert terms == ["ovarian cancer", "prostate cancer"]
    assert [t for t, _m, _s in coordinated_disease_terms("in breast, ovarian, and prostate cancer")] == [
        "breast cancer", "ovarian cancer", "prostate cancer",
    ]
    assert coordinated_disease_terms("patients receiving treatment and prostate cancer outcomes") == []
    for _term, mention, span in coordinated_disease_terms(HRD_Q):
        assert HRD_Q[span[0]:span[1]] == mention
    cands = qp.extract_candidates(HRD_Q)
    assert {"ovarian cancer", "prostate cancer", "prostate cancers"} <= set(cands.terms)


def test_disease_phrases_survive_the_term_cap():
    filler = " ".join(f"word{i} alpha{i} beta{i}" for i in range(60))
    q = f"{filler} in ovarian and prostate cancers"
    terms = qp.extract_candidates(q).terms
    assert len(terms) == qp.MAX_TERMS
    assert {"ovarian cancer", "prostate cancer", "prostate cancers"} <= set(terms)


@pytest.mark.asyncio
async def test_hrd_question_context_has_both_diseases_and_no_spurious_entities():
    data = await _resolve_question(_resolver(RunUpstream()), HRD_Q)
    reg, _calls = build_recording_registry(_resolver_tool(data))
    ctx = await _ctx(HRD_Q, registry=reg)
    diseases = {d.label: d for d in ctx.diseases}
    assert {"ovarian cancer", "prostate cancer"} <= set(diseases)
    assert diseases["ovarian cancer"].ids == {"ontology_id": "MONDO_0008170"}
    assert HRD_Q[slice(*diseases["ovarian cancer"].span)] == diseases["ovarian cancer"].mention
    assert "overall survival" not in diseases and "ovarian disease" not in diseases and "HRD syndrome" not in diseases
    assert "TBCE" not in [g.label for g in ctx.genes]
    assert [d.label for d in ctx.drugs] == ["OLAPARIB"]


def test_gene_family_acronym_is_not_a_disease():
    from phronesis.utils.biocontext.entity_resolver import EntityResolver

    hits = [{"entity": "disease", "id": "EFO_0000305", "name": "breast carcinoma"},
            {"entity": "disease", "id": "MONDO_0004989", "name": "invasive breast carcinoma"}]
    assert EntityResolver._accept_disease("BRCA", hits) is None
    pdac = [{"entity": "disease", "id": "EFO_0002618", "name": "pancreatic adenocarcinoma"}]
    assert EntityResolver._accept_disease("PDAC", pdac) is not None


def test_trial_names_are_not_gene_tokens():
    from phronesis.agents.query_planner import extract_candidates

    assert "SOLO" not in extract_candidates("summarize the SOLO-1 phase 3 trial for olaparib in BRCA2 ovarian cancer").gene_tokens
    assert extract_candidates("KEYNOTE-716 trial of pembrolizumab in melanoma").gene_tokens == []
    assert "EGFR" in extract_candidates("EGFR T790M in NSCLC phase 3 trials").gene_tokens
