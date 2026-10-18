"""
A class disease term ("cancer") is dropped next to a specific disease of its family, and agent evidence keys
carry readable labels ("Europe PMC (gap round 1)") for the run card. No network.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from phronesis.agentic.gaps import GapPhase
from phronesis.agentic.state import SubQuestion, evidence_key_label
from phronesis.agents import query_planner as qp
from phronesis.agents.authoritative_orchestrator import prioritise_question_diseases, question_disease_words
from phronesis.agents.query_context import ResolvedEntity


def _d(mention: str, label: str = "") -> ResolvedEntity:
    return ResolvedEntity(type="disease", mention=mention, label=label or mention)


def _names(ents: List[ResolvedEntity]) -> List[str]:
    return [e.mention for e in ents]


# drop_generic_diseases ----------------------------------------------------------------------------------------------


def test_generic_cancer_dropped_next_to_specific_cancer() -> None:
    assert _names(qp.drop_generic_diseases([_d("cancer"), _d("ovarian cancer")])) == ["ovarian cancer"]


def test_generic_kept_when_only_disease() -> None:
    assert _names(qp.drop_generic_diseases([_d("cancer")])) == ["cancer"]
    assert _names(qp.drop_generic_diseases([_d("solid tumors")])) == ["solid tumors"]


@pytest.mark.parametrize("generic", ["cancer", "Cancers", "tumor", "tumours", "neoplasm", "carcinoma", "malignancy", "solid tumors", "disease"])
def test_each_generic_term_dropped(generic: str) -> None:
    out = qp.drop_generic_diseases([_d(generic), _d("breast cancer")])
    assert _names(out) == ["breast cancer"]


def test_same_family_specific_without_shared_word() -> None:
    # "melanoma" and "NSCLC" are cancers although neither contains the word "cancer".
    assert _names(qp.drop_generic_diseases([_d("cancer"), _d("melanoma")])) == ["melanoma"]
    assert _names(qp.drop_generic_diseases([_d("tumor"), _d("NSCLC", "non-small cell lung carcinoma")])) == ["NSCLC"]


def test_generic_cancer_kept_next_to_unrelated_disease() -> None:
    # A non-oncology disease is not a more specific form of "cancer".
    assert _names(qp.drop_generic_diseases([_d("cancer"), _d("type 2 diabetes")])) == ["cancer", "type 2 diabetes"]
    # ...but the bare word "disease" says nothing next to any specific one.
    assert _names(qp.drop_generic_diseases([_d("disease"), _d("type 2 diabetes")])) == ["type 2 diabetes"]


def test_specific_label_keeps_entity() -> None:
    # The resolver mapped the mention "cancer" to a specific label: not a class term.
    ents = [_d("cancer", "ovarian cancer"), _d("prostate cancer")]
    assert _names(qp.drop_generic_diseases(ents)) == ["cancer", "prostate cancer"]
    # Generic mention with a generic ontology label ("neoplasm") is still generic.
    assert _names(qp.drop_generic_diseases([_d("cancer", "neoplasm"), _d("ovarian cancer")])) == ["ovarian cancer"]


def test_specific_diseases_untouched() -> None:
    ents = [_d("ovarian cancer"), _d("Alzheimer's disease"), _d("pancreatic adenocarcinoma")]
    assert _names(qp.drop_generic_diseases(ents)) == _names(ents)


def test_drop_generic_disease_names_for_condition_strings() -> None:
    assert qp.drop_generic_disease_names(["cancer", "ovarian cancer"]) == ["ovarian cancer"]
    assert qp.drop_generic_disease_names(["cancer"]) == ["cancer"]
    assert qp.drop_generic_disease_names(["ovarian", "prostate cancer"]) == ["ovarian", "prostate cancer"]


def test_planner_context_drops_generic_cancer(monkeypatch: pytest.MonkeyPatch) -> None:
    q = ("Identify druggable synthetic-lethal partners of BRCA2 loss in cancer, and how SOLO-1 supports olaparib "
         "maintenance in BRCA-mutated ovarian cancer.")
    # The live run's resolver named both "cancer" and "ovarian cancer"; the offline lexicon stands in for it here.
    found = [(m.group(0), m.group(0), m.span()) for m in re.finditer(r"(?<!ovarian )\bcancer\b|ovarian cancer", q)]
    assert [f[0] for f in found] == ["cancer", "ovarian cancer"]
    monkeypatch.setattr(qp, "find_offline_diseases", lambda _q: found)
    ctx = asyncio.run(qp.build_query_context(
        query=q, clean_query=q, registry=None, enable_web_search=False, attachments=[], attachment_errors=[],
        user_id="u", thread_id="t", model_name="m", api_key=None,
    ))
    assert [d.label for d in ctx.diseases] == ["ovarian cancer"]


# prioritise_question_diseases / question_disease_words ------------------------------------------------------------


def test_question_disease_words_skip_class_terms() -> None:
    ctx = SimpleNamespace(diseases=[_d("cancer"), _d("solid tumors"), _d("malignancy"), _d("ovarian cancer")])
    assert question_disease_words(ctx) == [{"ovarian"}]


def test_prioritise_puts_specific_disease_first() -> None:
    assocs: List[Dict[str, Any]] = [{"disease_name": "breast carcinoma"}, {"disease_name": "ovarian carcinoma"}]
    out = prioritise_question_diseases(assocs, [{"ovarian"}], keep=2)
    assert out[0]["disease_name"] == "ovarian carcinoma"


# Evidence key labels -------------------------------------------------------------------------------------------------


def test_evidence_key_label_names_tool_and_phase() -> None:
    assert evidence_key_label("agent.gaps.4.lithoscribe", 0) == "Europe PMC (gap round 1)"
    assert evidence_key_label("agent.gaps.5.websurfer", 1) == "web search (gap round 2)"
    assert evidence_key_label("agent.gaps.5.websurfer") == "web search (gap search)"
    assert evidence_key_label("agent.plan_execute.2.chembl") == "ChEMBL (plan step)"
    assert evidence_key_label("synlethdb") == "SynLethDB"
    assert evidence_key_label("tool:dgidb") == "DGIdb"
    assert evidence_key_label("PMID:12345") == "PMID:12345"
    assert evidence_key_label("agent.debate.3.unknown_tool") == "unknown tool (debate)"


def test_sub_question_dict_has_parallel_labels() -> None:
    q = SubQuestion(id="q1", text="?")
    q.add_evidence("agent.gaps.4.lithoscribe", "Europe PMC (gap round 1)")
    q.add_evidence("agent.gaps.4.lithoscribe")  # no duplicate
    q.evidence_keys.append("agent.plan_execute.7.primekg")  # a key added without a label
    d = q.to_dict()
    assert d["evidence_keys"] == ["agent.gaps.4.lithoscribe", "agent.plan_execute.7.primekg"]
    assert d["evidence_labels"] == ["Europe PMC (gap round 1)", "PrimeKG (plan step)"]


def test_gap_phase_labels_sub_question_evidence_with_round() -> None:
    sq = SubQuestion(id="q1", text="Which partners are druggable?")
    items = [{"id": "q1", "sq": sq, "status": "partial"}]
    GapPhase._update_sub_questions(
        items, {"q1": ["agent.gaps.4.lithoscribe", "agent.gaps.5.websurfer"]}, searched={"q1"}, round_no=0
    )
    d = sq.to_dict()
    assert d["evidence_labels"] == ["Europe PMC (gap round 1)", "web search (gap round 1)"]
