from types import SimpleNamespace

from phronesis.agents.authoritative_orchestrator import prioritise_question_diseases, question_disease_words


def test_question_disease_association_comes_first():
    ctx = SimpleNamespace(diseases=[SimpleNamespace(mention="pancreatic cancer", label="exocrine pancreatic carcinoma")])
    words = question_disease_words(ctx)
    assocs = [{"disease_name": n} for n in (
        "Noonan syndrome", "cardiofaciocutaneous syndrome", "lung adenocarcinoma", "colorectal cancer",
        "acute myeloid leukemia", "pancreatic adenocarcinoma", "pancreatic ductal adenocarcinoma")]
    out = prioritise_question_diseases(assocs, words)
    assert [a["disease_name"] for a in out[:2]] == ["pancreatic adenocarcinoma", "pancreatic ductal adenocarcinoma"]
    assert len(out) == 5 and out[2]["disease_name"] == "Noonan syndrome"


def test_no_question_disease_keeps_global_order():
    assocs = [{"disease_name": f"d{i}"} for i in range(8)]
    assert prioritise_question_diseases(assocs, []) == assocs[:5]
    assert question_disease_words(SimpleNamespace(diseases=[])) == []


def test_inherited_forms_follow_the_plain_disease():
    ctx = SimpleNamespace(diseases=[SimpleNamespace(mention="pancreatic cancer", label="exocrine pancreatic carcinoma")])
    assocs = [{"disease_name": n} for n in ("Noonan syndrome", "familial pancreatic carcinoma", "pancreatic adenocarcinoma")]
    out = prioritise_question_diseases(assocs, question_disease_words(ctx))
    assert [a["disease_name"] for a in out[:2]] == ["pancreatic adenocarcinoma", "familial pancreatic carcinoma"]
