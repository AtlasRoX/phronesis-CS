"""The one sentence splitter (phronesis.utils.text.sentence_spans) and the one source-name registry
(phronesis.sources.names), and the call sites that now use them."""

from __future__ import annotations

from types import SimpleNamespace

from phronesis.agentic import approval, nli
from phronesis.agentic.counter_evidence import _sentences as counter_sentences
from phronesis.agentic.gaps import hedged_sentences
from phronesis.agents import citations
from phronesis.drugintel import checks as drug_checks
from phronesis.drugintel import sources as drug_sources
from phronesis.evals import scoring
from phronesis.export.report_model import _reproducibility
from phronesis.lithoscribe.synthesis_engine import _section_sentences
from phronesis.phronesis.agents import verifier
from phronesis.sources import names
from phronesis.utils import text
from phronesis.utils.memory.clinical_memory import classify_instructions
from phronesis.variants import render as variant_render


def _cut(t: str):
    return [t[s:e] for s, e in text.sentence_spans(t)]


# --- sentence splitter -------------------------------------------------------------------------------------------

def test_sentence_spans_lives_in_utils_text_and_is_reexported():
    assert verifier.sentence_spans is text.sentence_spans
    assert citations.sentence_spans is text.sentence_spans


def test_sentence_spans_abbreviations_newlines_and_tables():
    assert _cut("Smith et al. found it. Next one, e.g. this. Fig. 2 shows it.") == [
        "Smith et al. found it.", "Next one, e.g. this.", "Fig. 2 shows it.",
    ]
    # "No." ends a sentence unless a number follows.
    assert _cut("It did not. No. 5 did.") == ["It did not.", "No. 5 did."]
    assert _cut("Heading\n\nFirst. Second!") == ["Heading", "First.", "Second!"]
    assert _cut("| a. b | c. d |\nAfter.") == ["| a. b | c. d |", "After."]
    assert text.sentence_spans("") == [] and text.sentence_spans("   \n ") == []


def test_verifier_split_sentences_matches_spans():
    t = "KRAS G12C is common. Sotorasib (e.g. in CodeBreaK) works.\n- A bullet."
    assert verifier.ClinicalVerifier._split_sentences(t) == [s.strip() for s in _cut(t)]


def test_nli_split_sentences_uses_shared_splitter():
    t = "Response was seen, e.g. in mice. p53 loss followed.\nNew line."
    assert nli.split_sentences(t) == ["Response was seen, e.g. in mice.", "p53 loss followed.", "New line."]
    assert nli.split_sentences(None) == []  # type: ignore[arg-type]


def test_counter_evidence_sentences_keep_five_word_filter_and_abbreviations():
    t = "Too short here. Smith et al. reported a negative phase 3 trial. Another long enough sentence is here."
    assert counter_sentences(t) == [
        "Smith et al. reported a negative phase 3 trial.", "Another long enough sentence is here.",
    ]


def test_hedged_sentences_split_by_shared_splitter():
    draft = "Efficacy is unclear in KRAS mutant tumours, e.g. in PDAC. Sotorasib is approved for NSCLC."
    assert hedged_sentences(draft) == ["Efficacy is unclear in KRAS mutant tumours, e.g. in PDAC."]


def test_section_sentences_filter_and_abbreviations():
    body = "Short one. Smith et al. showed that the inhibitor reduced growth. Another sentence that is long enough."
    assert _section_sentences(body) == [
        "Smith et al. showed that the inhibitor reduced growth.", "Another sentence that is long enough.",
    ]


def test_drugintel_checks_take_verifier_sentences():
    assert drug_checks._sentences(SimpleNamespace(sentences=["a", "b"])) == ["a", "b"]
    assert drug_checks._sentences(SimpleNamespace(sentences=None)) == []


def test_kept_local_splitters_differ_on_purpose():
    # Memory splits at ';' so each clause is its own instruction with its own scope.
    scopes = dict(classify_instructions("Only use human data; from now on exclude preprints."))
    assert scopes == {"Only use human data;": "current_turn", "from now on exclude preprints.": "investigation"}
    # The refusal check reads across a leading heading line (no newline break).
    assert scoring.opens_with_refusal("## Answer\nNo relevant evidence was retrieved. More text.")


# --- source registry ---------------------------------------------------------------------------------------------

def test_public_names_and_aliases():
    assert names.public_name("synapta") == "OpenTargets/KEGG"
    assert names.public_name("clinical_trials") == names.public_name("clinicaltrials") == "ClinicalTrials.gov"
    assert names.public_name("structure_intel") == names.public_name("structintel") == "AlphaFold DB / RCSB PDB"
    assert names.public_name("chembl") == "ChEMBL"
    assert names.public_name("openfda") == "openFDA"
    assert names.public_name("pubtator") == "PubTator3/LitVar2"
    assert names.public_name("websurfer") == "web search"
    assert names.public_name("nope") is None and names.public_name("nope", "x") == "x"
    assert names.public_name(None) is None
    assert names.agent_label("chembl") == "Synapta" and names.agent_label("openfda") == "Lithoscribe"
    assert names.version_label("clinicaltrials") == "ClinicalTrials.gov data timestamp"
    assert names.description("primekg") == "PrimeKG knowledge-graph analysis"
    assert set(names.INTERNAL_ONLY_KEYS).isdisjoint(names.SOURCE_REGISTRY)


def test_call_site_maps_keep_their_strings():
    assert approval.SOURCE_LABELS == {
        "synapta": "Target-disease associations and pathways (OpenTargets, KEGG)",
        "lithoscribe": "Literature search (Europe PMC, Semantic Scholar)",
        "websurfer": "Web search",
        "vivarix": "Kinetic model simulation (Vivarix, COPASI)",
        "lumicell": "Single-cell analysis of the attached dataset (Lumicell, Scanpy)",
        "clinical_trials": "Clinical trials registry (ClinicalTrials.gov)",
        "chembl": "Drug mechanisms and bioactivity (ChEMBL)",
        "openfda": "Drug safety reports and labels (openFDA)",
        "disease_targets": "Disease-to-target ranking (OpenTargets)",
        "primekg": "PrimeKG knowledge-graph analysis",
        "variant_intel": "Variant evidence (ClinVar, CIViC, cBioPortal, gnomAD)",
    }
    assert drug_sources._SOURCE_NAMES == {"chembl": "ChEMBL", "primekg": "PrimeKG"}
    assert variant_render.SOURCE_LABELS == {"clinvar": "ClinVar", "civic": "CIViC", "cbioportal": "cBioPortal", "gnomad": "gnomAD"}


def test_report_reproducibility_labels_unchanged():
    versions = {k: {"value": "v1"} for k in ("opentargets", "kegg", "europepmc", "clinicaltrials", "chembl", "openfda", "other")}
    rows = _reproducibility({"source_versions": versions})
    assert [r[0] for r in rows] == [
        "Open Targets data release", "KEGG last update", "EuropePMC API version", "ClinicalTrials.gov data timestamp",
        "ChEMBL release", "openFDA last updated", "other",
    ]
