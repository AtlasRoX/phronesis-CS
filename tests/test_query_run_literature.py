"""Query-run repair, literature (plan items 2.7-2.10 and the ingest part of 3.7). Offline: EuropePMC is a stub.

- 2.7: disease relevance (off-disease demoted, other named cancers excluded), conference bundles excluded,
  pivotal-trial and trial-name searches with reserved slots, relevance ranking before the max_articles cut.
- 2.8: in-vivo text cues outrank a MeSH in-vitro heading; trial protocols get their own study type.
- 2.9: DepMap intent accepts "synthetic-lethal" and SL wording.
- 2.10 / 3.7: titles and passages are plain text: tags become spaces, entities are decoded once.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from phronesis.agents.search_plan import (
    SearchPlan,
    build_search_plan,
    classify_question,
    clean_trial_names,
    disease_relevance_terms,
    disease_search_terms,
    merge_llm_plan,
    pivotal_trial_query,
    trial_name_query,
)
from phronesis.lithoscribe.evidence_grading import GradeBasis, StudyType, grade_article
from phronesis.lithoscribe.synthesis_engine import (
    LiteratureSynthesisEngine,
    _plain_text,
    cancer_families,
    is_conference_bundle,
)
from phronesis.sources_extra.depmap import INTENT_RE, SYNTHETIC_LETHAL_RE

MELANOMA_Q = (
    "What is the efficacy of adjuvant pembrolizumab or nivolumab in resected stage II cutaneous melanoma, "
    "including hazard ratios from pivotal trials?"
)


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------

def _entity(type_: str, mention: str, query: str, label: Optional[str] = None, aliases: Tuple[str, ...] = ()) -> Any:
    start = query.index(mention)
    return SimpleNamespace(
        type=type_, mention=mention, label=label or mention, aliases=list(aliases), span=(start, start + len(mention)),
        ambiguous_with=[], protein_change=None, gene=None, variant_kind=None,
    )


def _ctx(entities: List[Any]) -> Any:
    return SimpleNamespace(entities=entities, primary_gene=None)


def _art(pmid: Optional[str], title: str, abstract: str, **extra: Any) -> Dict[str, Any]:
    art = {
        "id": pmid or extra.pop("id", title[:20]), "source": extra.pop("source", "MED" if pmid else "PMC"),
        "pmid": pmid, "doi": "", "pmcid": "", "title": title, "abstractText": abstract, "pubYear": "2022",
        "journalTitle": "J", "authorString": "Author A, Author B", "pubType": extra.pop("pubType", "journal article"),
    }
    art.update(extra)
    return art


class _Client:
    """EuropePMC stand-in that answers each planned query by what it searches for."""

    def __init__(self, routes: List[Tuple[str, List[Dict[str, Any]]]], default: Optional[List[Dict[str, Any]]] = None):
        self.routes, self.default, self.calls = routes, default or [], []

    async def async_query_europepmc(self, query: str, limit: int = 5, sort: Any = None) -> Dict[str, Any]:
        self.calls.append({"query": query, "limit": limit, "sort": sort})
        for needle, arts in self.routes:
            if needle in query:
                return {"articles": [dict(a) for a in arts[:limit]]}
        return {"articles": [dict(a) for a in self.default[:limit]]}

    async def async_fetch_europepmc_fulltext(self, pmcid: str) -> str:  # pragma: no cover - no PMCIDs used
        return ""


def _engine(client: _Client) -> LiteratureSynthesisEngine:
    return LiteratureSynthesisEngine(biocontext_client=client)  # type: ignore[arg-type]


# Off-topic records observed in the adjuvant-melanoma run, and the pivotal papers it missed.
NSCLC_COST = _art("30000001", "Cost-effectiveness of pembrolizumab in advanced NSCLC",
                  "A cost-effectiveness model of pembrolizumab versus chemotherapy in non-small cell lung cancer trial data.")
HN_REVIEW = _art("30000002", "Nivolumab in recurrent head and neck squamous cell carcinoma: a review",
                 "We review nivolumab trials in head and neck cancer, including efficacy and hazard ratios.")
CRC_MSI = _art("30000003", "Pembrolizumab in MSI-H colorectal cancer",
               "Pembrolizumab improved progression-free survival in MSI-H colorectal cancer; approved also for melanoma.")
BUNDLE = _art(None, "BNOS 2016 Abstracts: Poster Presentations",
              "P01 pembrolizumab ... P02 nivolumab adjuvant trial ... melanoma brain metastases.",
              id="PMC5000000", pubType="Abstract")
KEYNOTE_716 = _art("35367007", "Pembrolizumab versus placebo as adjuvant therapy in completely resected stage IIB or IIC melanoma (KEYNOTE-716)",
                   "In this randomised phase 3 trial, adjuvant pembrolizumab improved recurrence-free survival (HR 0.65) in resected melanoma.",
                   pubType="Randomized Controlled Trial")
CHECKMATE_76K = _art("37625419", "Adjuvant nivolumab in resected stage IIB/C melanoma: primary results from the randomized, phase 3 CheckMate 76K trial",
                     "Nivolumab improved recurrence-free survival versus placebo (HR 0.42) in stage IIB/C melanoma.",
                     pubType="Randomized Controlled Trial")
IRAE = _art("30000004", "Immune-related adverse events of pembrolizumab and nivolumab",
            "A pharmacovigilance study of pembrolizumab and nivolumab adverse events in efficacy trials.")
MELANOMA_OTHER = _art("30000005", "Pembrolizumab in advanced melanoma: long-term follow-up",
                      "Long-term survival with pembrolizumab in advanced melanoma in a phase 3 trial.")


# -----------------------------------------------------------------------------
# 3.7 / 2.10: plain text at ingest
# -----------------------------------------------------------------------------

def test_plain_text_spaces_tags_and_decodes_entities_once():
    assert _plain_text("KRAS<sup>G12D</sup> inhibition") == "KRAS G12D inhibition"
    assert _plain_text("KRAS&lt;sup&gt;G12D&lt;/sup&gt; and &lt;i&gt;in vitro&lt;/i&gt;") == "KRAS G12D and in vitro"
    assert _plain_text("OS improved (p &lt;0.001; HR&lt;1 and OR &gt; 2) &amp; DFS") == "OS improved (p <0.001; HR<1 and OR > 2) & DFS"
    once = _plain_text("p&lt;0.05 and HR &gt; 1")
    assert once == "p<0.05 and HR > 1" and _plain_text(once) == once  # a decoded "<" is never re-stripped as a tag


@pytest.mark.asyncio
async def test_graded_titles_and_passages_are_plain_text():
    rec = _art("36216931", "KRAS<sup>G12D</sup> inhibition with MRTX1133",
               "MRTX1133 suppressed KRAS<sup>G12D</sup> signalling (p &lt;0.001) in pancreatic cancer models.")
    client = _Client([], default=[rec])
    res = await _engine(client).execute_literature_synthesis("MRTX1133 KRAS G12D", check_integrity=False)
    assert res.success, res.error
    graded = res.data["graded_articles"]
    assert graded[0]["title"] == "KRAS G12D inhibition with MRTX1133"
    texts = " ".join(p["passage_text"] for p in res.data["ranked_passages"])
    assert "(p <0.001)" in texts and "&lt;" not in texts and "KRAS G12D signalling" in texts


# -----------------------------------------------------------------------------
# 2.7: search plan
# -----------------------------------------------------------------------------

def test_hazard_ratio_wording_is_a_trials_question_not_statistics():
    assert classify_question(MELANOMA_Q) == "trials"
    assert classify_question("How do I calculate hazard ratios from Kaplan-Meier curves?") == "statistics"
    assert classify_question("How do I calculate sample size for a non-inferiority trial?") == "statistics"


def test_plan_merges_alternative_drugs_and_adds_a_pivotal_query():
    ents = [_entity("drug", "pembrolizumab", MELANOMA_Q), _entity("drug", "nivolumab", MELANOMA_Q),
            _entity("disease", "cutaneous melanoma", MELANOMA_Q)]
    plan = build_search_plan(_ctx(ents), MELANOMA_Q)
    assert plan.concepts[0] == ["pembrolizumab", "nivolumab"]  # alternatives, not an AND of two drugs
    assert any("melanoma" in g for g in plan.concepts)  # the broader disease name is searched too
    assert plan.pivotal and plan.intervention_terms[:2] == ["pembrolizumab", "nivolumab"]
    queries = plan.europepmc_queries(year=2026)
    pivotal = next(q for q in queries if q["label"] == "pivotal")
    assert pivotal["tier"] == 1 and pivotal["sort"] == "CITED desc" and pivotal["slots"] >= 2 and pivotal["limit"] >= 8
    assert '(TITLE_ABS:"pembrolizumab" OR TITLE_ABS:"nivolumab")' in pivotal["query"]
    assert 'TITLE_ABS:"melanoma"' in pivotal["query"] and 'PUB_TYPE:"Randomized Controlled Trial"' in pivotal["query"]
    assert 'PUB_TYPE:"Clinical Trial, Phase III"' in pivotal["query"] and 'TITLE_ABS:"phase 3"' in pivotal["query"]
    assert all(q["disease"] == plan.disease_terms for q in queries)  # the ranking step learns the disease
    # The planner model's plan keeps the disease, interventions and trial names.
    merged = merge_llm_plan(plan, {"concepts": [["pembrolizumab", "Keytruda"]], "question_type": "trials"}, MELANOMA_Q)
    assert merged.pivotal and merged.disease_terms == plan.disease_terms and merged.intervention_terms == plan.intervention_terms


def test_disease_phrase_is_split_from_its_staging_words():
    plan = build_search_plan(_ctx([]), MELANOMA_Q)
    assert "cutaneous melanoma" in plan.disease_terms
    assert ["cutaneous melanoma", "melanoma"] in plan.concepts
    parp = build_search_plan(_ctx([]), "Which PARP inhibitors improve progression-free survival in ovarian cancer and prostate cancer?")
    assert parp.concepts[0] == ["ovarian cancer", "prostate cancer"]  # listed diseases are one concept
    assert parp.pivotal and parp.intervention_terms == ["PARP inhibitors"]
    pq = next(q for q in parp.europepmc_queries(year=2026) if q["label"] == "pivotal")
    assert 'TITLE_ABS:"PARP inhibitor"' in pq["query"]  # singular form too ("a PARP inhibitor, olaparib")
    # No trial or efficacy wording and no drug: no pivotal search.
    assert not build_search_plan(_ctx([]), "Is MYC amplification a therapeutic target in glioblastoma?").pivotal


def test_disease_search_and_relevance_terms():
    assert disease_search_terms(["pancreatic ductal adenocarcinoma"]) == [
        "pancreatic ductal adenocarcinoma", "pancreatic adenocarcinoma", "pancreatic cancer"]
    pdac = disease_relevance_terms(["pancreatic ductal adenocarcinoma", "PDAC"])
    assert "pancrea*" in pdac and "PDAC" in pdac and "adenocarcinoma" not in pdac
    mel = disease_relevance_terms(["cutaneous melanoma"])
    assert "melanoma" in mel and "cutaneous" not in mel
    assert cancer_families("Nivolumab in NSCLC") == {"lung"} and cancer_families("Uveal melanoma") == {"melanoma"}


def test_trial_name_query_and_validation():
    assert clean_trial_names(["KEYNOTE-716", " CheckMate 76K ", "keynote-716", "x", '"; DROP', 42]) == ["KEYNOTE-716", "CheckMate 76K"]
    q = trial_name_query(["KEYNOTE-716", "CheckMate 76K", "PRIMA"], ["ovarian cancer"])
    assert q is not None and q["label"] == "trial_names" and q["tier"] == 1 and q["trial_names"] == ["KEYNOTE-716", "CheckMate 76K", "PRIMA"]
    assert '(TITLE_ABS:"KEYNOTE-716" OR TITLE_ABS:"CheckMate 76K")' in q["query"]
    assert '(TITLE_ABS:"PRIMA" AND TITLE_ABS:"ovarian cancer")' in q["query"]  # a bare word needs the disease
    assert trial_name_query([], ["melanoma"]) is None and pivotal_trial_query([], ["melanoma"]) is None
    plan = SearchPlan(question_type="trials", concepts=[["pembrolizumab"]], trial_names=["KEYNOTE-716"])
    assert any(q["label"] == "trial_names" for q in plan.europepmc_queries(year=2026))


# -----------------------------------------------------------------------------
# 2.7: ranking and filtering in the engine
# -----------------------------------------------------------------------------

def test_conference_bundles_are_recognised():
    assert is_conference_bundle(BUNDLE)
    assert is_conference_bundle(_art(None, "ESMO Congress 2019 highlights", "x", id="PMC1"))
    assert is_conference_bundle(_art("123", "Meeting Abstracts from the 2020 SNO annual meeting", "x"))
    assert not is_conference_bundle(KEYNOTE_716)
    assert not is_conference_bundle(_art("124", "Melanoma", "x", pubType="Abstract"))  # an indexed paper with a PMID


@pytest.mark.asyncio
async def test_adjuvant_melanoma_literature_is_on_disease_and_keeps_pivotal_trials():
    ents = [_entity("drug", "pembrolizumab", MELANOMA_Q), _entity("drug", "nivolumab", MELANOMA_Q),
            _entity("disease", "cutaneous melanoma", MELANOMA_Q)]
    plan = build_search_plan(_ctx(ents), MELANOMA_Q)
    client = _Client(
        [
            ("PUB_TYPE:", [KEYNOTE_716, CHECKMATE_76K]),  # the pivotal search
            ("PUB_YEAR:[2023", [BUNDLE, IRAE, MELANOMA_OTHER]),  # the recent search
        ],
        default=[NSCLC_COST, HN_REVIEW, CRC_MSI],  # the landmark search (and any wider tier)
    )
    res = await _engine(client).execute_literature_synthesis(
        MELANOMA_Q, max_articles=4, check_integrity=False,
        search_queries=plan.europepmc_queries(year=2026), keywords=plan.keywords(),
    )
    assert res.success, res.error
    titles = [a["title"] for a in res.data["graded_articles"]]
    assert KEYNOTE_716["title"] in titles and CHECKMATE_76K["title"] in titles
    for bad in (NSCLC_COST, HN_REVIEW, CRC_MSI, BUNDLE):
        assert bad["title"] not in titles
    reasons = {o["title"]: o["reason"] for o in res.data["off_topic_articles"]}
    assert "different cancer type" in reasons[NSCLC_COST["title"]] and "lung" in reasons[NSCLC_COST["title"]]
    assert "different cancer type" in reasons[CRC_MSI["title"]]
    assert "conference abstract" in reasons[BUNDLE["title"]]
    # The record that never mentions melanoma ranks below every on-melanoma passage.
    passages = res.data["ranked_passages"]
    irae = [i for i, p in enumerate(passages) if p["title"] == IRAE["title"]]
    on = [i for i, p in enumerate(passages) if "melanoma" in p["title"].lower()]
    assert on and (not irae or min(irae) > max(on))
    retrieval = res.data["retrieval"]
    assert retrieval["ranking"]["order"] == "relevance" and retrieval["ranking"]["reserved_trial_slots"] == 2
    assert retrieval["off_disease_demoted"] == 1 and retrieval["disease_terms"] == plan.disease_terms
    assert any(q["label"] == "pivotal" and q["hits"] == 2 for q in retrieval["queries"])


@pytest.mark.asyncio
async def test_trial_names_are_searched_and_guaranteed_a_place():
    filler = [_art(str(31000000 + i), f"Pembrolizumab melanoma adjuvant efficacy study {i}",
                   "Adjuvant pembrolizumab melanoma recurrence-free survival efficacy hazard ratio 0.7.") for i in range(5)]
    named = _art("35367007", "Adjuvant therapy in stage II disease", "Results of the KEYNOTE-716 study of pembrolizumab in melanoma.")
    client = _Client([('"KEYNOTE-716"', [named])], default=filler)
    res = await _engine(client).execute_literature_synthesis(
        "adjuvant pembrolizumab melanoma efficacy", max_articles=2, check_integrity=False, trial_names=["KEYNOTE-716"],
        disease_terms=["melanoma"],
    )
    assert res.success, res.error
    assert any('TITLE_ABS:"KEYNOTE-716"' in c["query"] for c in client.calls)
    assert named["title"] in [a["title"] for a in res.data["graded_articles"]]
    assert res.data["retrieval"]["trial_names"] == ["KEYNOTE-716"]


@pytest.mark.asyncio
async def test_articles_are_ranked_by_relevance_before_the_cut_with_a_minimum_score():
    weak = _art("1", "Sepsis registry", "A registry of sepsis admissions.")  # 1 of 5 keywords
    strong = _art("2", "Lactate clearance and vasopressor timing in septic shock",
                  "Sepsis with septic shock: lactate clearance guided vasopressor timing and mortality.")
    middling = _art("3", "Septic shock vasopressor", "Sepsis and septic shock: vasopressor choice.")
    plan = SearchPlan(question_type="treatment", concepts=[["sepsis"]])
    client = _Client([], default=[weak, middling, strong])  # retrieval order puts the weakest first
    res = await _engine(client).execute_literature_synthesis(
        "sepsis", max_articles=2, check_integrity=False, search_queries=plan.europepmc_queries(year=2026),
        keywords=["sepsis", "septic", "lactate", "vasopressor", "mortality"],
    )
    assert res.success, res.error
    titles = [a["title"] for a in res.data["graded_articles"]]
    assert titles[0] == strong["title"] and weak["title"] not in titles
    assert res.data["retrieval"]["ranking"]["below_min_relevance"] == 1
    assert any("minimum relevance" in o["reason"] for o in res.data["off_topic_articles"])


@pytest.mark.asyncio
async def test_planned_search_widens_when_tier_one_is_off_disease():
    off = [_art(str(40000000 + i), f"Pembrolizumab in NSCLC {i}", "Lung cancer.") for i in range(5)]
    plan = SearchPlan(question_type="treatment", concepts=[["pembrolizumab"]], disease_terms=["melanoma"])
    client = _Client([("PUB_YEAR", off)], default=[MELANOMA_OTHER])
    engine = _engine(client)
    found_by: Dict[str, List[str]] = {}
    found, _ = await engine._planned_search(
        plan.europepmc_queries(year=2026), 8, found_by=found_by,
        useful=lambda a: "melanoma" in a["title"].lower(),
    )
    assert any(c["query"].endswith("HAS_ABSTRACT:y") and "PUB_YEAR" not in c["query"] for c in client.calls)  # tier 2 ran
    assert MELANOMA_OTHER["id"] in found and "landmark" in found_by[off[0]["id"]]


# -----------------------------------------------------------------------------
# 2.8: study design
# -----------------------------------------------------------------------------

def _mesh(*names: str) -> Dict[str, Any]:
    return {"meshHeading": [{"descriptorName": n} for n in names]}


def test_in_vivo_text_outranks_a_mesh_in_vitro_heading():
    hallin = _art("36216931", "Anti-tumor efficacy of a potent and selective non-covalent KRASG12D inhibitor",
                  "MRTX1133 showed tumor regression in cell-line-derived and patient-derived xenograft models.",
                  meshHeadingList=_mesh("Cell Line, Tumor", "Humans"))
    g = grade_article(hallin)
    assert g.study_type == StudyType.PRECLINICAL_IN_VIVO and g.label == "Preclinical in vivo"
    assert g.basis == GradeBasis.HEURISTIC and g.confidence == "medium" and g.cues == ['abstract: "xenograft"']
    assert "Cell Line, Tumor" in g.design_note
    only_cells = _art("2", "KRAS signalling in cell lines", "Cell lines were treated; in vivo studies are needed to confirm.",
                      meshHeadingList=_mesh("Cell Line, Tumor"))
    assert grade_article(only_cells).study_type == StudyType.IN_VITRO


def test_trial_protocols_have_their_own_study_type():
    tagged = _art("3", "Pembrolizumab trial", "Design of a trial.", pubTypeList={"pubType": ["Clinical Trial Protocol"]})
    g = grade_article(tagged)
    assert g.study_type == StudyType.TRIAL_PROTOCOL and g.label == "Trial protocol" and g.tier == 6
    titled = _art("4", "Adjuvant olaparib: study protocol for a randomised controlled trial", "We describe the protocol.")
    assert grade_article(titled).study_type == StudyType.TRIAL_PROTOCOL


# -----------------------------------------------------------------------------
# 2.9: DepMap intent
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "What are the synthetic-lethal partners of KRAS G12D?", "synthetic-lethality with PARP", "synthetically lethal with BRCA1",
    "SL partners of ARID1A", "essentiality of MYC", "KRAS dependency in PDAC", "genetic dependencies of ARID1A-mutant cancers",
])
def test_depmap_intent_accepts_hyphenated_and_sl_wording(text):
    assert INTENT_RE.search(text)


def test_synthetic_lethal_pattern():
    assert SYNTHETIC_LETHAL_RE.search("synthetic-lethal partners") and not SYNTHETIC_LETHAL_RE.search("synthetic biology")


def test_question_trial_names_feed_the_literature_plan():
    from types import SimpleNamespace

    from phronesis.agents.authoritative_orchestrator import question_trial_names

    ctx = SimpleNamespace(
        genes=[SimpleNamespace(mention="CDK6", label="CDK6")],
        drugs=[SimpleNamespace(mention="MRTX1133", label="MRTX1133")],
        variants=[],
    )
    q = ("Compare AURA3 and KEYNOTE-716 with CheckMate 76K in phase 3; MRTX1133 and CDK6 are not trials, "
         "nor is COVID-19 or ECOG 1.")
    assert question_trial_names(ctx, q) == ["AURA3", "KEYNOTE-716", "CheckMate 76K"]
