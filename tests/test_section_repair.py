"""Targeted section repair (phronesis/agents/orchestration/section_repair.py): only affected sections are rewritten,
untouched text stays byte-identical, invalid replacements keep the original, and versions stay consistent."""

from __future__ import annotations

import asyncio
from typing import List, Tuple

from phronesis.agents.orchestration.section_repair import (
    AnswerVersion,
    affected_sections,
    citation_keys,
    claims_to_reverify,
    repair_sections,
    splice,
    split_sections,
)

# Synthetic answer: identifiers and numbers are test strings, not real study results.
ANSWER = """**Bottom line**
Drug A improves survival in disease X (PMID: 11111111); drug B is under study (NCT00000001).

### Efficacy of drug A
In the phase III trial, median OS was 12.0 months with drug A versus 9.0 months with placebo (PMID: 11111111).

| Arm | n | Median OS |
|---|---|---|
| Drug A | 100 | 12.0 |
| Placebo | 98 | 9.0 |

### Drug B
Drug B showed a 30% response rate in a phase II study (PMID: 22222222).

### Safety
Grade 3 rash occurred in 5% of patients (PMID: 11111111).

### Evidence limitations
No head-to-head trial compares drug A and drug B.
"""


def ids(sections):
    return [s.id for s in sections]


def test_split_keeps_preamble_tables_and_reproduces_text():
    secs = split_sections(ANSWER)
    assert ids(secs) == ["bottom-line", "efficacy-of-drug-a", "drug-b", "safety", "evidence-limitations"]
    assert "".join(s.text for s in secs) == ANSWER
    eff = secs[1]
    assert "| Drug A | 100 | 12.0 |" in eff.text and eff.level == 3  # the table stays inside its section
    assert secs[0].is_summary and not eff.is_summary
    assert citation_keys(eff.text) == {"PMID:11111111"}


def test_split_preamble_without_heading_and_code_fence():
    text = "Opening sentence (PMID: 33333333).\n\n## Details\n```\n## not a heading\n```\nMore.\n"
    secs = split_sections(text)
    assert ids(secs) == ["preamble", "details"] and secs[0].is_summary
    assert "## not a heading" in secs[1].text and "".join(s.text for s in secs) == text


def test_one_quoted_claim_repairs_its_section_and_the_summary():
    secs = split_sections(ANSWER)
    fb = ['[major] Claim: "Drug B showed a 30% response rate in a phase II study" - the passage reports 25% '
          "(evidence: PMID:22222222; raised by methodologist)"]
    d = affected_sections(secs, fb)
    assert d.mode == "targeted"
    assert list(d.sections) == ["bottom-line", "drug-b"]  # in answer order
    assert any(r.startswith("summary restates") for r in d.sections["bottom-line"])


def _fake_generate(rewrites, calls: List[Tuple[str, int]]):
    async def generate(system: str, user: str, max_tokens: int) -> str:
        section = user.split("<section>\n", 1)[1].split("\n</section>", 1)[0]
        calls.append((section.splitlines()[0], max_tokens))
        for old, new in rewrites:
            if old in section:
                return section.replace(old, new)
        return section
    return generate


def test_repair_changes_only_affected_sections_byte_identically_elsewhere():
    calls: List[Tuple[str, int]] = []
    gen = _fake_generate([("30% response rate", "25% response rate"), ("drug B is under study", "drug B had a 25% response")], calls)
    fb = ['Claim: "Drug B showed a 30% response rate in a phase II study" - wrong value (evidence: PMID:22222222)']
    version = AnswerVersion.start(ANSWER, ["PMID:11111111", "PMID:22222222", "NCT:NCT00000001"])
    out, report = asyncio.run(repair_sections(ANSWER, fb, gen, allowed_keys=["PMID:11111111", "PMID:22222222", "NCT:NCT00000001"],
                                              per_section_max_tokens=500, version=version))
    assert report.mode == "targeted" and sorted(report.applied) == ["bottom-line", "drug-b"]
    assert "25% response rate" in out and "drug B had a 25% response" in out
    before, after = split_sections(ANSWER), split_sections(out)
    for b, a in zip(before, after):
        if b.id not in ("bottom-line", "drug-b"):
            assert a.text == b.text  # untouched sections are byte-identical
    assert calls[-1][0] == "**Bottom line**"  # the summary is rewritten last, after the body sections
    assert all(m == 500 for _, m in calls) and report.calls == 2
    assert version.answer_version == 2 and version.verification_stale
    assert sorted(report.version["sections_changed"]) == ["bottom-line", "drug-b"]
    version.mark_verified()
    assert not version.verification_stale


def test_invalid_replacements_keep_the_original():
    secs = split_sections(ANSWER)
    drug_b = next(s for s in secs if s.id == "drug-b")
    bad = {
        "drug-b": "### Something else\nDrug B text (PMID: 22222222).",  # changed heading
    }
    res = splice(secs, bad, allowed_keys=["PMID:22222222"])
    assert res.applied == [] and "heading" in res.rejected["drug-b"] and res.markdown == ANSWER
    for reply, why in [
        ("", "empty"),
        (drug_b.heading + "\nDrug B is promising (PMID: 99999999).", "outside the evidence"),
        (drug_b.heading + "\nx", "length"),
        (drug_b.heading + "\nDrug B 25% (PMID: 22222222).\n### Safety\nDuplicate.", "heading"),
    ]:
        res = splice(secs, {"drug-b": reply}, allowed_keys=["PMID:22222222"])
        assert res.applied == [] and why in res.rejected["drug-b"], (reply, res.rejected)
        assert res.markdown == ANSWER


def test_a_reply_without_its_heading_is_restored():
    secs = split_sections(ANSWER)
    res = splice(secs, {"drug-b": "Drug B showed a 25% response rate in a phase II study (PMID: 22222222)."},
                 allowed_keys=["PMID:22222222"])
    assert res.applied == ["drug-b"] and "### Drug B\nDrug B showed a 25%" in res.markdown


def test_too_many_sections_or_unplaceable_feedback_falls_back_to_whole():
    secs = split_sections(ANSWER)
    many = affected_sections(secs, new_evidence_keys=["PMID:11111111", "PMID:22222222"])
    assert many.mode == "whole" and "sections are affected" in many.reason
    vague = affected_sections(secs, ["Gap re-retrieval added new evidence. Use the new gap-search documents."])
    assert vague.mode == "whole" and "name no section" in vague.reason
    assert affected_sections(split_sections("One paragraph only."), ["x"]).mode == "whole"
    assert affected_sections(secs).mode == "none"

    async def never(*_a):  # whole/none decisions make no model call
        raise AssertionError("generate must not be called")
    out, report = asyncio.run(repair_sections(ANSWER, ["Use the new documents."], never))
    assert out == ANSWER and report.mode == "whole" and report.calls == 0


def test_changed_evidence_and_unsupported_claims_select_sections():
    secs = split_sections(ANSWER)
    d = affected_sections(secs, new_evidence_keys=["PMID: 22222222"])
    assert list(d.sections) == ["bottom-line", "drug-b"]
    d = affected_sections(secs, unsupported_claims=["Grade 3 rash occurred in 5% of patients"])
    assert list(d.sections) == ["bottom-line", "safety"]
    removal = splice(secs, {"safety": "### Safety\nNo safety data were retrieved."}, reasons=d.sections)
    assert removal.applied == ["safety"]  # a removal reason allows a much shorter section


def test_parallel_generation_is_bounded():
    active, peak = [0], [0]

    async def gen(system, user, max_tokens):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.01)
        active[0] -= 1
        return user.split("<section>\n", 1)[1].split("\n</section>", 1)[0]

    text = "**Bottom line**\nSummary.\n\n" + "".join(f"### S{i}\nClaim {i} cites PMID: 1000000{i}.\n\n" for i in range(8))
    keys = [f"PMID:1000000{i}" for i in range(3)]
    out, report = asyncio.run(repair_sections(text, [], gen, new_evidence_keys=keys, max_parallel=2, whole_fraction=0.9))
    assert report.mode == "targeted" and report.calls == 4 and peak[0] <= 2 and out == text


def test_failed_section_generation_keeps_original_and_others_apply():
    async def gen(system, user, max_tokens):
        section = user.split("<section>\n", 1)[1].split("\n</section>", 1)[0]
        if section.startswith("### Drug B"):
            raise TimeoutError("provider timed out")
        return section.replace("Drug A improves", "Drug A may improve")

    fb = ['Claim: "Drug B showed a 30% response rate in a phase II study" - check (evidence: PMID:22222222)']
    out, report = asyncio.run(repair_sections(ANSWER, fb, gen))
    assert "drug-b" in report.failed and "TimeoutError" in report.failed["drug-b"]
    assert report.applied == ["bottom-line"] and "Drug A may improve" in out
    assert "30% response rate" in out


def test_claims_to_reverify_and_evidence_version():
    claims = [{"claim_text": "Drug B showed a 30% response rate in a phase II study"},
              {"claim_text": "Grade 3 rash occurred in 5% of patients"}]
    assert claims_to_reverify(ANSWER, ["drug-b"], claims) == claims[:1]
    v = AnswerVersion.start(ANSWER, ["PMID:1", "PMID:2"])
    assert not v.bump_evidence(["PMID:2", "pmid: 1"]) and v.bump_evidence(["PMID:3"])
    entry = v.record_repair(ANSWER, ANSWER, "targeted", {})
    assert entry["to_version"] == entry["from_version"] == 1 and entry["sections_changed"] == []
