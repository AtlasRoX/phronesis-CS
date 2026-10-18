# Phronesis fixes, round 2: 18 October 2026

This round fixes the findings in `Phronesis_Source_Audit_2026-10-18.md` (F1–F10) and works through the twelve items of `Phronesis_Claude_Code_Repair_Prompt.md`. It also fixes defects found by live runs of `Phronesis_Next_Test_Query.txt` against the local backend. Round 1 is in `FIXES_2026-10-18.md`. All paths are relative to the repository root.

## Checks

| Check | Command | Result |
|---|---|---|
| Backend tests | `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests` | 1,797 passed, 0 failed, 8 skipped |
| Ruff, error rules | `uvx ruff@0.13.2 check --select E9,F63,F7,F82,F811 phronesis tests` | passed (the full style rule set has about 8,600 existing findings, not addressed) |
| Env reference drift | `python scripts/gen_env_reference.py --check` | passed |
| Web typecheck | `npx tsc --noEmit -p .` | passed |
| Web tests | `npm test` | 85 passed, 0 failed |
| Web lint | `npx eslint .` | 0 errors, 211 warnings (audit baseline: 208) |
| Web build | `npm run build` | passed |

## Live telemetry: matched runs

Every run used the same query (`Phronesis_Next_Test_Query.txt`), the Plan & Execute framework selected explicitly, medium effort, the model `nvidia/nemotron-3-super-120b-a12b`, web search on, and the same local backend. Each run is a single sample, so there are no p50/p95 figures. Model output varies between runs. "Before" is the code as delivered in round 1, with only the clarifier fix (L1) applied.

| Run | Wall time | First answer text | Longest event gap | Model calls | Tool calls | Ledger tokens (est.) | Sub-questions answered | Verification score | Claims supported | First draft | Rewrite round |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Before (round-1 code + L1) | 132 s | 66 s | 18.6 s | 11 | 6 | 35,951 | 1/3 | 0.35 | 1/3 | 29 s | 14 s |
| After, run 1 | 188 s | 94 s | 26.2 s | 9 | 7 | 46,683 | 2/3 | 0.15 | 5/9 | 54 s | 18 s |
| After, run 2 | 123 s | 51 s | 23.7 s | 11 | 8 | 33,805 | 1/3 | 0.25 | 5/8 | 40 s | 5 s |
| After, run 3 (final code) | 103 s | 52 s | 11.6 s | 12 | 3 | 28,836 | 2/3 | 0.8 | 7/9 | 32 s | 8 s |

How to read this table:
- **Ledger tokens:** "Before" omits the first draft (F1). The after runs include it, so they undercount less.
- **Numbers retrieved:** Before, the exact lookup failed and all twelve requested numbers were "unavailable". In all three after runs, all twelve numbers match the verified abstract (2L: 102/58, 10.2 vs 6.0 months, HR 0.62; 2L+: 164/116, 10.2 vs 7.2 months, HR 0.65).
- **Correction:** the correction is reported as existing and retrieved, with no text, so it was not applied. That sub-question stays "partial" because the erratum's content is unavailable.
- **Final code:** run 3 is on the final code before the last verifier rule for location lines. Replaying its answer through the final verifier gives no unsupported claims (`tests/test_verifier_grounding.py::test_third_live_answer_has_no_false_findings`).
- **Exclusions:** neither hypothesis generation nor debate ran in any after run. In the before run, hypothesis generation ran.
- **Rewrite rounds:** runs 2 and 3 used targeted section repair (2 sections each). Run 1 used a whole rewrite that changed nothing.
- **Comparability:** these are single live samples against a live model and live sources, so the timing differences are indicative, not statistically established. The earlier 14-minute Auto/Ultra run was a different query and settings and is not compared here.

## What was fixed

### Found by live runs (not in the audit)

| ID | Defect | Fix | Files | Tests |
|---|---|---|---|---|
| L1 | "No FDA sweep, simulation, omics … is needed" was read as a simulation request, so the run stopped to ask "Which model should I simulate?" | A list-form ban pattern that also consumes the rest of the list | `phronesis/agents/query_planner.py` | `tests/test_live_regressions.py` |
| L2 | The hypothesis phase ran although the query excluded it; debate could also run when excluded | One exclusion detector (`detect_excluded_modules`) covering hypothesis, debate, FDA sweep, omics, simulation and single-cell, honoured by the hypothesis phase, debate escalation, the Debate phase itself, Auto routing, the openFDA sweep and the omics tools | `query_planner.py`, `agentic/hypothesis.py`, `agentic/router.py`, `agentic/topologies.py`, `drugintel/sources.py`, `agentic/plan_graph.py`, `agentic/specialists.py` | `tests/test_retrieval_obligations.py`, `tests/test_live_regressions.py` |
| L3 | A correct answer scored 0.15. The retrieved erratum's PMID counted as "not found"; correction-status sentences were judged against the paper; "Source: … URL …" and "All figures are taken verbatim from …" lines were audited as claims | Exact-lookup and correction outcomes join the run's tool log, and retrieved correction notices count as retrieved records. Correction status and linkage are operational claims grounded in that log. Source-location lines are not audited, though their identifiers still are. | `phronesis/phronesis/agents/verifier.py` | `tests/test_verifier_grounding.py` (replays both live answers, saved in `tests/fixtures/primary/live_answer*_pmid_39369609.md`) |
| L4 | Agentic literature documents had no title or URL, so one run reported the title as "not provided" | `<document>` carries `title` and the record `url` | `phronesis/agentic/evidence.py` | `tests/test_live_regressions.py` |
| L5 | "(see `<literature_limitations>`)" leaked into the prose | References to snake_case internal evidence sections are stripped | `phronesis/agents/authoritative_orchestrator.py` (`strip_evidence_markup`) | `tests/test_live_regressions.py` |

### Audit findings F1–F10

| ID | Finding | Fix | Files | Tests |
|---|---|---|---|---|
| F1 | The first draft bypassed the run ledger | Every agentic model call (first draft, agent JSON calls, rewrites and their stream retries, verifier revision, router features, section repairs) is admitted and settled through `RunBudget.admit()` / `settle()`. Provider-reported usage is kept separately from character estimates. A first draft the budget cannot admit gives a deterministic partial result with no model call. | `agentic/budget.py`, `agentic/llm.py`, `agents/orchestration/draft.py`, `agent_rounds.py`, `verify.py`, `authoritative_orchestrator.py`, `agentic/router.py` | `tests/test_budget_admission.py` |
| F2 | Concurrent outputs weren't bounded by their reservations | Each call is sent exactly its admitted `output_cap`, never the global remainder; unused allocation is released on success, failure, timeout or cancellation. Regression A (10k budget, 3 calls): ledger went from 14,712 to 9,144. Regression B (3k final reserve): the reserve now stays intact. | `agentic/budget.py`, `agentic/llm.py` | `tests/test_budget_admission.py` |
| F3 | The 1,024-token rewrite floor exceeded what the budget had left | The floor is removed (1,100 left with a 1,000-token prompt now gives 100, not 1,024). A whole rewrite that can't get room for the full draft is skipped with a reason. Rewrites use the optional pool, so they can't drain the final reserve. | `agents/orchestration/agent_rounds.py` (`generation_allocation`), `verify.py` | `tests/test_budget_admission.py`, `tests/test_bounded_runs.py` |
| F4 | Streamed stages ignored the run deadline | `RunContext.watched` races each long call against cancellation and the absolute deadline (`RunDeadlineExceeded`), with a grace period for providers that don't cooperate. The 1,800 s stream cap is bounded by the remaining run time. | `agents/orchestration/context.py`, `utils/llm/llm_factory.py` | `tests/test_budget_admission.py` |
| F5 | Gap dedup dropped distinct clinical questions | Structured requirement keys (disease, variant, drug, mono vs combination, line, jurisdiction, year, endpoint, identifiers) are compared before word overlap. Gap outcomes are typed (filled, off_topic, already_seen, empty, incomplete, error), with one bounded alternative. Off-topic results are filtered at fetch time. | `agentic/gaps.py`, `agentic/specialists.py` | `tests/test_retrieval_obligations.py` |
| F6 | FDA eligibility trusted URLs embedded in text | Shared typed authority policy: the record's own canonical or final host must be exactly fda.gov or a subdomain. Lookalike hosts, userinfo tricks, IP literals, missing URLs and snippet-only links are rejected. The same policy is used by the verifier, prompts, citations, evidence and exports. | `utils/source_authority.py` (new), `phronesis/agents/verifier.py`, `agents/prompts.py`, `agents/citations.py`, `agentic/evidence.py`, `tools/web_search.py`, `export/report_model.py` | `tests/test_verifier_grounding.py` |
| F7 | Population checks skipped tool records | Typed tool records (variant/CIViC, cBioPortal, label indications, registry conditions) are checked through their own population fields. | `phronesis/agents/verifier.py` | `tests/test_verifier_grounding.py` |
| F8 | Matching numbers overrode an explicit subgroup warning | Sample sizes are bound to the group they count (`denominator_mismatch`). A judge verdict naming a concrete scope error is honoured (`NUMERIC_RELATION_MISMATCH`); an inconclusive judge isn't treated as an error. | `phronesis/agents/verifier.py` | `tests/test_verifier_grounding.py` |
| F9 | A real PMID was paired with synthetic values | Parser fixtures now use synthetic IDs. A verified primary-record fixture holds the real abstract's 2L and 2L+ arms, the hashes, the retrieval time and the correction status (erratum PMID 39984314 exists; its content was not retrieved or applied). | `tests/test_evidence_integrity.py`, `tests/fixtures/primary/pmid_39369609.json` | both files |
| F10 | Exact identifiers never reached the literature tool | `pinned_ids` is exposed and forwarded. Identifiers in the question become exact lookups that run before topical search, are exempt from relevance filters, and are always passed on Lithoscribe calls. A failed lookup is reported as not retrieved, with one NCBI efetch alternative and correction linkage. Root cause: `PMID:39369609` is not a Europe PMC field, so it ran as free text. | `lithoscribe/identifiers.py` (new), `lithoscribe/pubmed_fallback.py` (new), `lithoscribe/synthesis_engine.py`, `tools/registry.py`, `agents/query_context.py`, `agentic/planner.py`, `agentic/state.py` | `tests/test_retrieval_obligations.py`, `tests/test_live_regressions.py` |

### Repair-prompt items 11–12

| Item | Fix | Files | Tests |
|---|---|---|---|
| 11 Selective cards (the patch was not supplied, so it was built from the audit's description) | A deterministic disclosure policy: important evidence, low support, failed plans and partial runs open; routine dialogue and memory stay collapsed; exclusion clauses are honoured. The Card follows the policy until the reader toggles it, and that choice survives streaming. The verification notice lists every defect, keeps supported claims in their own compact list, and separates operational (run-status) claims. Checked in Playwright at 1280 and 390 px, including Enter/Space toggles. | `web/src/lib/artifactDisclosure.ts` (new), `components/agentic/ui.tsx`, `components/MessageArtifacts.tsx`, `components/VerificationNotice.tsx`, `lib/verificationState.ts`, `lib/entities.ts`, `app/page.tsx` | `web/tests/artifactDisclosure.test.mts` |
| 12 Targeted section repair, versions | Rewrite rounds first repair only the affected sections plus the summary. The sections are found from the sentences a critique quotes, hedged sentences a gap filled, sub-questions matched to their section, unsupported claims and changed evidence keys. Each section call is admitted, capped and watched. Replacements are validated (heading, length, citation keys) and the loop falls back to a whole rewrite when targeting isn't possible. `AnswerVersion` tracks answer and evidence versions and is exported as the `answer_version` artifact. | `agents/orchestration/section_repair.py` (new), `agents/orchestration/agent_rounds.py` | `tests/test_section_repair.py`, `tests/test_section_repair_integration.py` |

## Not finished

- **The classic framework has no run ledger.** Its stages are documented as unledgered, and the classic query planner's model call is unledgered too.
- **First-draft usage is estimated.** It is reconciled from character counts because the streaming API doesn't report provider usage.
- **Abandoned calls can still finish.** A provider HTTP call that ignores cancellation may complete in the background after being abandoned; the run stops waiting for it.
- **No population context for extension trial records.** Trial-registry documents from extension providers carry no typed population fields.
- **Cross-row comparisons can still be flagged.** A correct comparison across table rows ("longer median OS with sotorasib; HRs favour sotorasib") can still be judged unsupported by the model-based entailment judge. The judge wasn't loosened.
- **Some exclusions aren't fully honoured.** An explicitly selected Debate framework now skips its critics when the question excludes debate, but its plan-and-execute phase still runs. PMCID and DOI lookups have no alternative source.
- **Not visually checked.** Disclosure behaviour during live streaming wasn't visually checked, and no screen-reader testing was done.
- **The earlier 14-minute run is not shown fixed.** That needs a matched rerun of that run's own query and settings (Auto, Ultra).
