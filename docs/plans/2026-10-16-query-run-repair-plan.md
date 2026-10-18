# Query-run repair plan (2026-10-16)

Source: four full transcripts run on 2026-10-16:

- **Q1** KRAS G12D / MRTX1133 resistance in PDAC (Plan & execute)
- **Q2** EGFR T790M / C797S in NSCLC (Supervisor)
- **Q3** MYC target-to-lead in GBM (Swarm)
- **Q4** Adjuvant PD-1 in stage IIB/IIC melanoma (Debate)
- **Q5** PARP inhibitors in HRD ovarian and prostate cancer (Classic)

Baseline before any change: `pytest` gives 834 passed, 6 skipped.

Severity:

- **P0**: wrong science reaches the answer, or the requested mode silently does not run.
- **P1**: important evidence is missing or misattributed.
- **P2**: UX or noise.

---

## WS-0 LLM timeouts (P0, the root cause of most "failed" steps)

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 0.1 | Every non-streaming agent phase dies at about 21 s with `APITimeoutError`: plan_execute, hypothesis, supervisor, swarm, debate, gaps, nli_precompute, confidence. Debate, supervisor and swarm effectively never ran. | `utils/llm/llm_factory.py:164,183` default `LLM_TIMEOUT` = 10 s with `max_retries=1`. `AgentLLM` never passed its own 90 s timeout to the client. | Default 60 s (`DEFAULT_LLM_TIMEOUT_S`). `AgentLLM` passes `timeout=self.timeout_seconds` to the factory. **Done.** |
| 0.2 | A timeout skips every phase's fallback and kills the whole phase. | `AgentLLM.complete` converted only `asyncio.TimeoutError`. `openai.APITimeoutError` escaped `except AgentLLMUnavailable` in `specialists.ask_json`, `nli.llm_label`, `confidence.stated_confidence`, `hypothesis`, `plan_execute`. | `complete()` maps any client exception to `AgentLLMUnavailable`. **Done.** |
| 0.3 | Steps such as "Planning follow-up tool calls" and "Checking the draft for evidence gaps" spin forever. A separate "X failed" row appears next to them. | The phase opens step `agent_plan`, but the runtime reports the failure as `agent_plan_execute`. Nothing closes leftover running steps. | `AgentState` tracks running steps. `run_stage` resolves steps a phase left open, in place, with the failure text. **Done.** Still to do: an end-of-run sweep in the orchestrator `emit` and in `web/src/app/page.tsx` on `done`. |
| 0.4 | Run exceeds its budget (432/420 s). Phases run strictly one after another. | The budget is cooperative and `phase.run` is not bounded. `llm.complete` allows at least 5 s past the budget. `nli_precompute` scores a draft that is about to be replaced. Gap and counter-evidence queries run sequentially. | Bound each phase with `wait_for(remaining)`. Skip `nli_precompute` when a rewrite is pending. `asyncio.gather` the per-query searches in gaps and counter-evidence. |

## WS-1 Reading the question (P0)

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1.1 | "receptor **tyrosine** kinase" becomes the drug TYROSINE (CHEMBL925). It then goes to PrimeKG, to trials (`MRTX1133 OR TYROSINE`), to bioRxiv, and onto a card titled "CHEMBL5081048 · TYROSINE". | `query_planner._ngrams` sends every 1-gram to the resolver. `entity_resolver._accept_drug` accepts any exact name of 3+ letters. There is no stoplist for generic biochemicals. A longer phrase does not suppress the word inside it across entity types. | Add a stoplist for generic biochemical words (amino acids, nucleotides, ions, metabolites, vitamins, "glucose", "oxygen", ...) as drugs, unless they appear as a standalone therapeutic mention. Reject a drug whose mention sits inside a recognised multi-word biological phrase ("tyrosine kinase", "receptor tyrosine", "serine/threonine"). |
| 1.2 | "(HRD)" resolves to the gene TBCE. | `HRD` is missing from `NON_GENE_ACRONYMS`. The `_hgnc_token` alias fallback accepts a single alias hit. `_fold_defined_acronyms` only folds acronyms whose expansion was resolved. | Add biomarker-concept acronyms: HRD, HRR, MSI, MSI-H, MSS, TMB, dMMR, pMMR, LOH, CIN, CNV, TNBC, ER, PR (context-aware). Treat any acronym defined in the question as `long form (ACR)` as non-gene when the long form is not a gene name. Require stronger evidence for an alias-only gene hit, such as gene context words. |
| 1.3 | "overall survival" is tagged as a DISEASE. | `_accept_disease` exact multi-word matches skip the `_NON_DISEASE_WORDS` filter. EFO measurement terms are accepted. | Apply the non-disease filter to multi-word exact hits. Reject endpoint and measurement phrases (overall survival, progression-free survival, response rate, hazard ratio, ...). |
| 1.4 | "ovarian **and** prostate cancers" keeps only prostate. Ovarian trials and hypotheses are absent. | `trial_query.py:222` uses `ctx_diseases[0]`. `extract_condition` stops at "and". There is no handling of coordinated nouns. The `disease_targets`, PrimeKG and planner code take `diseases[:1]`. | Expand coordinated disease phrases ("X and Y cancers" gives X cancer and Y cancer). Trials run one search per disease (up to 3) and merge. PrimeKG profiles up to 2 diseases. |
| 1.5 | Only T790M is looked up in the variant databases; C797S gets nothing ("no variant-level data for C797S"). | `variants/source.py:25-43` returns the first usable variant. The input model holds a single variant. | Look up up to 3 variants concurrently and render one variant card per variant. |
| 1.6 | Cards show "PrimeKG profile of MONDO_0005184". | `extensions/primekg.py:49-51` passes the MONDO id as the label. `kgintel/planner.py:156` uses it verbatim. | Carry `(id, name)`. The reason text uses the name. |
| 1.7 | The drug card header shows the ChEMBL id instead of the name ("CHEMBL5081048"). | `drugintel/names.py:94-97` queries by ChEMBL id, so `query_name` becomes the id. The mention is lost. `DrugSafetyArtifact.tsx:321` falls back to `query_name`. | Carry `mention` through. The UI title is `pref_name || mention || query_name`. |

## WS-2 Evidence retrieval (P0/P1)

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 2.1 | Melanoma adjuvant trials: shows 10 of 705, mostly phase 1. Misses KEYNOTE-716 (NCT03553836) and CheckMate 76K (NCT04099251). | `trial_query.py` ignores setting words (adjuvant, resected, stage, randomized). There is no `sort`, `pageSize=10`, and half the slots go to "any trial with results". There is no re-ranking. | Extract setting terms into `query.term`. Fetch about 50 and re-rank locally: phase 3 before phase 2 before phase 1, setting-term match, enrollment, results posted. Then show the top 10. Results-only extras must also match the setting. |
| 2.2 | ChEMBL "contains no listed compounds" for MYC. The answer claims no MYC small molecules exist in ChEMBL. | `drugintel/engine.py:336-352`: with no drug named, it only queries approved mechanisms (`max_phase=4`). `/activity` is never queried by target. | Add a target-only activity query (top compounds by pChEMBL) when no drug is named or no approved drug exists. The evidence text separates "no approved drugs" from "no bioactive compounds". |
| 2.3 | Niraparib mechanisms are duplicated (salt forms). | No dedupe in `engine.py:404-415`. | Dedupe by `(action_type, target_chembl_id, moa)`, preferring the parent. |
| 2.4 | FAERS top reactions are DEATH, MALIGNANT NEOPLASM PROGRESSION, OFF LABEL USE, ... | No filter on the `count` query. | Exclude non-reaction and indication MedDRA PTs (death, disease progression, off-label use, product use issues, drug ineffective, ...). Request top 25 and keep 10. |
| 2.5 | PrimeKG profile lists always start with the same hub diseases and promiscuous drugs (HBOC, breast neoplasm, Lidocaine, Zinc). | `kgintel/analytics._ranked` sorts by global degree. The question's disease is never passed. | `gene_profile` accepts context diseases and puts matches (the disease and its MONDO descendants or name overlap) first. Drugs sort by approval/indication overlap before degree. |
| 2.6 | CIViC "top 6 by rating" mixes lung and colorectal evidence into a pancreatic question. | `_civic_chain` ignores `cancer_keyword`. The UI sorts by rating only. | Pass the disease keyword. Rank matching-disease items first and label them "for <disease>". |
| 2.7 | Literature is off-topic (NSCLC cost-effectiveness, H&N review, an MSI-H CRC paper used for melanoma). It includes the "BNOS 2016 Abstracts" bundle and misses pivotal trial papers. | `search_plan.europepmc_queries` ANDs groups but the disease is not required. There is no relevance threshold. Conference bundles without a PMID pass. There are no trial-acronym or pivotal-trial queries. Only about 5 results are kept per query. | (a) Require the disease (or a synonym) in title or abstract when the question names one, by demoting off-disease papers below on-disease ones. (b) Drop records with pubType "Abstract" or "Meeting" and no PMID. (c) Add a "pivotal trial" query (`drug AND disease AND (phase 3 OR randomized)`, `PUB_TYPE:"Clinical Trial"`). (d) Trial acronyms found in registry results (KEYNOTE-716) become literature queries. |
| 2.8 | Study type: Hallin 2022 is labelled "In vitro". Trial protocols are labelled "Unclassified". | A MeSH in-vitro label overrides text cues showing in-vivo work (xenograft). There is no protocol type. | An in-vivo text cue overrides a MeSH in-vitro label. Add a `TRIAL_PROTOCOL` study type. |
| 2.9 | Synthetic-lethality question: no DepMap or SL source. | `depmap.INTENT_RE` misses "synthetic-lethal". DepMap needs a local `PHRONESIS_DEPMAP_DIR` that is not configured. | Accept the hyphen form in the regex. When SL is asked and DepMap is unavailable, emit an explicit limitation (no invented partners). Configuration (download CRISPRGeneEffect.csv) is a deployment step. |
| 2.10 | "KRAS<sup>G12D</sup>" renders as "KRASG12D". | `EvidenceSources.tsx:80-83 plainText` strips tags without adding a space. | Replace `<sup>`/`<sub>` tags with a thin space or space before stripping. |

## WS-3 Claim checking (P0)

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 3.1 | Headings ("### Phase III…") and table rows, including the header row, are scored as claims, with 79-93% confidence or SUPPORTED. | `verifier._split_sentences` splits on newlines with no markdown awareness. `confidence.gather_claims` uses the verifier claims unfiltered. | Skip headings, table separator and header rows, and bare table rows. A table data row is turned into a sentence ("<first cell>: <metric header> <value>") only when it has a number, and keeps its row's NCT/PMID. Strip list markers. Protect "et al." and "e.g." from splitting. |
| 3.2 | A claim with no salient terms and no metrics is "supported" by the first document (an OS row credited to `tool:synapta`). | `_clinical_verdict` uses `needed = min(2, len(terms))`, which is 0. | With no terms and no metrics, the verdict is `not_checked`. Numbers in a claim must appear in the supporting doc (bare-number metric extraction for table rows). |
| 3.3 | Data from another disease or population is accepted as support (MSI-H CRC irAE rates in a melanoma answer). | No population check exists anywhere. | Add a `population_mismatch` check. When the claim's evidence doc names a disease that differs from the question's disease and the claim does not state the other population, flag a `POPULATION_MISMATCH` defect (major). The correction prompt must then either name the source population or drop the claim. |
| 3.4 | Absence-claim "contradicted" by keyword co-occurrence ("the retrieved passage mentions biological rationale chosen duration"). | `_absence_verdict` uses bag-of-words. | Require that the content words appear within one sentence window, and drop to `not_checked` (not contradicted) when the passage itself is hedged or negated. |
| 3.5 | A contradicted claim survives "Revised the answer". | Acceptance is `passed or score >= old`, with no check that flagged claims are gone. | Accept a revision only if each critical claim is removed or re-verified. Otherwise deterministically remove the contradicted sentence and add a one-line limitation. |
| 3.6 | "Verified (score 1.00) — identifiers, numbers, and citations match" in 0.2 s. | Hard-coded text whenever `passed`. | The text reports what was actually checked: "N claims checked: S supported, U not checked…". It says "fully verified" only if `fully_verified`. |
| 3.7 | Truncated claims ("…survival f", "[0") and internal tags (`[CLAIM_CONTRADICTED]`, `[CLINICAL_CLAIM]`) in step text. `&lt;0` in claims. | `[:240]` and `[:160]` slices. `defect_lines` prefixes the category. HTML entities in abstracts, plus `escape(draft)` in the correction prompt, leak into the answer. | Truncate on a word boundary with an ellipsis. Map categories to human labels ("Contradicted by a source:"). `html.unescape` passage text at ingest and unescape the final answer before verification and display. |
| 3.8 | Confidence: T=1 and every claim lands at 79-93% "high". | Hand-set weights. A verifier "supported" (term overlap) alone pushes above 0.75. | Lower the `verifier_supported` weight. "High" requires NLI entailment or an exact identifier and number match. Pure term-overlap support is capped at medium. |
| 3.9 | Counter-evidence always checks "3 claims against 10 passages", taken from passages rather than the answer. | `extract_claims` runs before the draft exists. | Keep the augment pass and add an after-draft pass over the top answer claims (the numeric and clinical ones). |

## WS-4 Orchestration modes (P0)

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 4.1 | Supervisor: "1 turn · 1 agent", and no specialist ran. | No fallback when the ledger call fails. Delegation only happens through the LLM reply. | Deterministic fallback: dispatch the `task_ledger()` ranked plan (top 2-3 specialists) when the supervisor reply is unusable. Drop the Simulation specialist when simulation was not requested. |
| 4.2 | Swarm: started with Chemistry instead of Target, stopped after one bad reply, and openFDA was never called. | `_start` uses the tool-retrieval top hit. `if not isinstance(turn, SwarmTurn): break`. | Start from the first step the question names (order of mention: target, then chemistry, then clinical). Retry one unusable reply, then follow the deterministic handoff chain. Ensure the clinical/safety leg runs `openfda_drug_safety` and trials when asked. |
| 4.3 | "Requested operation completed" shows even when the requested debate failed. The Debate badge looks normal. | `_task_outcome` ignores `phases_ran`. | When the framework's core phase (debate, supervisor, swarm, plan_execute) has status != ok, the outcome is `partial` with the reason "Debate did not run: model call failed". Show a warning chip on the Agent run badge. |
| 4.4 | "Execution plan: 0 nodes… planner produced no runnable steps" when the planner timed out. | `PlanGraphArtifact.tsx:79-81` is hard-coded. `plan_execute` never writes `error`. | Write `error` into the artifact. The UI shows "The planner did not run (model call failed); the pipeline's own evidence was used." |
| 4.5 | "Model calibration not run" is counted as an issue and its card is rendered. The question said "calibrated therapeutic strategy". | `CALIBRATION_INTENT_RE` matches any `calibrat`. | Require modelling context (calibrate + model/parameters/ODE, model calibration, ...). |
| 4.6 | Gap filling searches for "kinetic simulation". Every answer lists Vivarix and Lumicell limitations. | `gaps.hedged_sentences` treats the limitations section as gaps. Prompts require limitations for non-run components. `discovery.py:255-262` boilerplate. | Gaps ignore the "Evidence limitations" section and modules skipped as not requested. Prompts say to list limitations only for components that were requested or relevant and failed. Drop "not requested" scope notes from `<tool_status>`. |
| 4.7 | Answers narrate internal tool names ("Lithoscribe retrieved PMID…", "Synapta KEGG analysis…"). | `prompts.py:99-101` instructs it. | Cite the underlying source (EuropePMC, OpenTargets, KEGG, ClinicalTrials.gov), not internal component names, in the answer prose. |
| 4.8 | Hypotheses are templated ("Inhibiting EGFR will decrease NSCLC-associated disease activity"). The footnote appears twice. | The template fallback in `_evidence_hypotheses`. `HypothesesArtifact.tsx` hard-codes a note and also renders `data.note`. | Template hypotheses become mechanism-aware where possible (variant, resistance, combination). Render only one note. |

## WS-5 Rendering (P2)

| # | Symptom | Fix |
|---|---|---|
| 5.1 | Wide tables overflow and the last column is cut off. There is an empty band above the header row. | `th { vertical-align: middle }`. Allow wrapping (`word-break: normal; overflow-wrap: anywhere` on long cells), `table-layout: auto` with max width, and a visible scroll shadow. |
| 5.2 | Steps still spinning after `done`. | `page.tsx` on `done` resolves any `running` thought to `done`. |

## Order of work

1. WS-0 (done: 0.1-0.3). WS-4.5. **Done in the first pass.**
2. In parallel, by file ownership:
   - WS-1 entity (query_planner, entity_resolver, entity_lexicon)
   - WS-1.4/2.1 trials + WS-1.5/2.6 variants
   - WS-2.2-2.5 drugintel + PrimeKG
   - WS-2.7-2.10 literature
   - WS-3 verifier/confidence
   - WS-4 + WS-5 orchestration/prompts/frontend
3. Full `pytest` plus `web` typecheck and tests. Re-run the five questions live and compare.

## Not fixable in code alone

- DepMap needs `PHRONESIS_DEPMAP_DIR` pointing at a downloaded `CRISPRGeneEffect.csv`.
- NLI uses the LLM judge unless the cross-encoder is in the local HF cache. Pre-download it for a non-LLM fallback.
- Confidence temperature stays uncalibrated until `PHRONESIS_CONFIDENCE_CALIBRATION` is fitted on labelled runs.

---

## Status (2026-10-16, end of first implementation pass)

Every item in WS-0 to WS-5 is implemented, with offline regression tests in:

- `tests/test_query_run_entities.py`
- `tests/test_query_run_trials_variants.py`
- `tests/test_query_run_drug_kg.py`
- `tests/test_query_run_literature.py`
- `tests/test_query_run_verification.py`
- `tests/test_query_run_orchestration.py`
- `web/tests/queryRun.test.mts`

Existing tests changed because they asserted the old, buggy behaviour:

- `test_drug_intel.py`: three FAERS assertions. DEATH is now `deaths_reported`, not the top reaction.
- `test_evidence_grading.py`: a protocol is now `TRIAL_PROTOCOL`.
- `test_agentic_verification.py`: a contradicted claim is now removed from the answer.
- `test_prompts.py`: the scope note no longer names Vivarix.

Notes and remaining limits:

- **Trials client.** `ClinicalTrialsGovClient()` keeps the old unranked behaviour when built without `fetch_size`. Production (`search_trials`) uses `fetch_size=50` with re-ranking.
- **Named trials.** Trial names written in the question (`question_trial_names`) feed the literature plan's `trial_names` query. Trial acronyms found only in registry results are not yet fed back into literature, because the stages run concurrently.
- **Per-site disease expansion.** Only single-word sites are expanded ("ovarian and prostate cancers"). Multi-word sites ("non-small cell lung and pancreatic cancers") are not.
- **Prompt size.** The composed system prompt is close to the 13,000-byte limit in `test_prompts`.
- **Component names.** `_sanitize_agent_names` is unchanged. The extension component lines still map evidence blocks to component names ("Synapta (ChEMBL)"). A prompt rule now forbids narrating those names in the answer.
- **Not yet done.** The five questions have not been re-run against live sources and the live model.

---

## WS-6 New issues from the repurposing question (2026-10-16, 14:04 run)

Question Q6: "Design a step-by-step pipeline to identify FDA-approved drugs that could be repurposed for treating KRAS-mutant pancreatic cancer, incorporating clinical-trial status, in-silico binding predictions, and real-world EHR safety data."

The run used the backend started at 11:07, before any fix. Its timeouts, "score 1.00" and heading-claims are therefore WS-0 to WS-5 issues that are already fixed. The six issues below are new.

| # | Symptom | Fix | Owner |
|---|---|---|---|
| 6.1 | "pancreatic cancer" resolved to *exocrine pancreatic carcinoma* (MONDO_0005192), a PrimeKG node with no indications ("0/0 known indications"). | Fall back to the related node that has indications (the parent, or the node matching the user's mention). Say so in the reason text. | kgintel agent |
| 6.2 | Repurposing candidates: a PET tracer (Ga-68 edotreotide), SSTR agents for neuroendocrine tumours, and cannabinoid antiemetics. The answer called them "FDA-approved candidates". | Exclude diagnostic, imaging and radiotracer agents and nutrients. Tag supportive-care classes. Modestly up-rank candidates whose targets lie in the mutated gene's pathway. | kgintel agent |
| 6.3 | "Found 0 approved drugs acting on KRAS": the ChEMBL call timed out (ReadTimeout), and sotorasib and adagrasib exist. | Report a failed lookup as failed, never as 0. Retry with backoff. | drugintel agent |
| 6.4 | openFDA was skipped ("no drug named") although the question asks for real-world safety data. | When safety is asked and no drug is named, run FAERS and the label for up to 3 candidates from ChEMBL or PrimeKG. State that FAERS is not EHR data. | drugintel agent |
| 6.5 | Trials were searched with condition "exocrine pancreatic carcinoma": 1 result. | Use the user's own disease mention ("pancreatic cancer") for the condition. | kgintel agent |
| 6.6 | "In-silico binding predictions" was skipped as "no structure question". | `STRUCTURE_INTENT_RE` and `DOCKING_RE` accept in-silico binding, binding predictions and virtual screening. Docking without a SMILES string reports that it needs a ligand. **Done.** | — |

### WS-6 status

All six items are done: `pytest` gives 1041 passed, 6 skipped, and the web typecheck and tests pass.

- **6.1 (wrong disease node)**
  - The real store has no "pancreatic cancer" node.
  - Repurposing keeps the genes of *exocrine pancreatic carcinoma* and borrows the indications of its child, *pancreatic adenocarcinoma*. This is labelled in the reason text.
  - Calibration goes from "unknown 0/0" to AUROC 0.774.
- **6.2 (bad candidates)**
  - Diagnostics, radiotracers and nutrients are excluded and listed with a reason.
  - Supportive-care drugs are tagged.
  - Neuroendocrine-tumour agents score half outside neuroendocrine diseases.
  - KRAS-pathway drugs are listed separately: dabrafenib, vemurafenib, sotorasib, binimetinib, adagrasib, encorafenib, sirolimus.
  - The PrimeKG repurposing benchmark is unchanged (mean AUROC 0.811).
- **6.3 (ChEMBL timeout reported as 0)**
  - A failed lookup shows as "unknown" everywhere: step, evidence, card and scope notes.
  - One retry, with a 12 s read timeout.
  - Live, the KRAS mechanism query returned sotorasib and adagrasib; the ChEMBL API itself was degraded during testing.
- **6.4 (no safety data)**
  - openFDA runs on up to 3 candidates. It uses ChEMBL's approved drugs for the target, falling back to PrimeKG approved candidates, waiting on those sources through the new `SourceRunState.source_result`.
  - The answer states that FAERS is not EHR data.
- **6.5 (only 1 trial)**
  - The trials condition uses the user's wording. Live: "pancreatic cancer" + KRAS gives 191 trials; "exocrine pancreatic carcinoma" gave 1.
- **6.6 (binding predictions skipped)**
  - Binding-prediction wording now routes to the structure lookup.
- **Also: OpenTargets "top: Noonan syndrome"**
  - Associations are fetched at size 25 when the question names a disease, and that disease is put first (`prioritise_question_diseases`).

---

## WS-7 First run on the new code (Q6 again, after the restart)

**Working now:**

- Trials: 191 found, with RASolute 303/305 present.
- openFDA ran on the candidates.
- Steps resolve; none keep spinning.
- The banner honestly reads "partly completed — Plan & execute did not run".
- The revision fixed the flagged claim.
- The verify step reports a real claim tally.

**Fixed in this round:**

| # | Symptom | Fix |
|---|---|---|
| 7.1 | Plan & execute took 90.1 s and hit the agent call limit ("did not run"). | Nemotron 3 reasons by default. Agent calls (plans, critiques, gap checks, JSON verdicts) now send `chat_template_kwargs.enable_thinking=false`, measured 17.1→7.5 s and 11.0→8.1 s. The final answer keeps its reasoning. `PHRONESIS_AGENT_THINKING=on` restores reasoning for agent calls. The option is dropped on the OpenAI fallback. |
| 7.2 | "Number does not match the simulation output: - Balsalazide: 1 454 total FAERS reports": a false defect with the wrong label. | Space-grouped counts ("1 454") are accepted when the joined value equals a retrieved count. More Unicode space separators are recognised. The NUMERICAL label now reads "Number does not match the evidence". List markers are stripped from defect lines. |
| 7.3 | Every sub-question was marked "answered", including in-silico binding scores, which the draft said were not retrieved. | A draft sentence sharing 3 content stems with a sub-question counts as hedging it, so the status becomes partial or missing. |
| 7.4 | Generic pancreatic phase 3 trials outranked KRAS-targeted ones. | Trials with the searched term in their title, conditions, keywords or interventions get +25. |

**Still open:**

- **ChEMBL outage.** ChEMBL returned HTTP 500 during the run. That is EBI's side; it is now reported as "unknown".
- **Budget overrun (491/420 s).** The synthesis rewrite and correction rounds after the budget runs out are not bounded. Faster agent calls (7.1) should keep the run within budget.
- **Weak sub-question.** The decomposer still produced "What is the list of all FDA-approved drugs?". That is the model's decomposition quality.

## WS-8 No time limit (user request, 2026-10-16)

- **No run time limit by default.**
  - `RunBudget.max_seconds` defaults to 0, which means unlimited. `remaining_seconds()` then reports `UNLIMITED_SECONDS` (24 h).
  - A limit chosen in the UI settings, or set with `PHRONESIS_AGENT_MAX_SECONDS`, still applies.
  - The run panel shows "· no limit" and no red bar.
- **Model-call cap** raised from 24 to 40.
- **Agent call timeout** raised from 90 s to 180 s (`PHRONESIS_AGENT_CALL_TIMEOUT`).
- **Answer streaming** keeps its own 120 s idle and 600 s total limits per answer.
- **OpenTargets associations:** inherited forms ("familial pancreatic carcinoma") are ordered after the plain disease unless the question asks for them.

## WS-9 Q6 run after the no-limit restart (run_5b9437dcd8974fb1)

**Working now:**

- Plan & execute ran: 3 nodes, 1 replan.
- No failed steps and no time cutoff.
- KRAS-specific phase 3 trials come first: RASolute 303/305/302/304/309, DAWN-303, MAPKeeper 301.
- UniProt P01116 was resolved, with 556 PDB entries.

**Fixed:**

| # | Symptom | Fix |
|---|---|---|
| 9.1 | ChEMBL drug and target names missing (approved KRAS drugs shown only as CHEMBL IDs). | Batch request with a per-ID fallback (6 s hedge, 25 s cap) and name caching. The resolve step's target name is passed on. An unnamed drug is never labelled by its mechanism text. Live: SOTORASIB, ADAGRASIB, GTPase KRas. |
| 9.2 | openFDA picked balsalazide, mesalazine and dronabinol instead of the ChEMBL drugs; mesalazine gave 0 FAERS reports. | ChEMBL drugs are used first, with names looked up by ID. PrimeKG fallback order is pathway approved drugs, then pathway-tagged, then the rest; supportive-care, neuroendocrine and non-therapeutic candidates are skipped. 55 international→US name synonyms, with retries on the active-substance and generic-name fields. Live: mesalazine → mesalamine (63,326 reports). |
| 9.3 | The answer showed `total_reports = 1454`, `(structure_intel)`, `(clinical_trials)`, `(tool_status note)`. | A prompt rule (the composed prompt is 12,935 bytes) plus a deterministic `clean_internal_keys`. It is applied to the first draft, rewrites and revisions, and a `content_reset` keeps the screen, the verifier and the stored answer identical. |
| 9.4 | Duplicate SRC/dasatinib hypotheses; hypotheses claiming EHR or trial data that did not exist; step text cut mid-word. | `dedupe_ranked` (same target, intervention and direction, or ≥80 % word overlap). `GROUNDED_STATEMENT_RULE` in the generation and evolution prompts. `truncate_words` in the hypothesis and PrimeKG step text. |
| 9.5 | Off-topic literature (EUS-FNA, ddPCR, KRAS diagnostic assays). | "Design a pipeline" made it a methods question whose `guide*` matched "EUS-guided". New intent detection (therapy / repurposing / in-silico / safety), per-aspect queries, and demotion of diagnostic or technical titles for therapeutic questions. Unicode hyphens are normalised. Live: Strickler NEJM 2023 (sotorasib, PDAC), a KRAS-therapy review, repurposing and docking papers. PARP now finds PROfound. Melanoma and EGFR results are unchanged. |
| 9.6 | "Found 0 web results". | DuckDuckGo answers this machine with HTTP 202 anti-bot pages. This is now reported as "Web search failed: … blocked". The query is short and clean. Optional providers: `TAVILY_API_KEY` (or `PHRONESIS_SEARXNG_URL`). |
| 9.7 | "BRCA" resolved as the disease invasive breast carcinoma (TCGA cohort code). | `_GENE_FAMILY_ACRONYMS` is never accepted as a disease. |

**Not fixable in code:** DuckDuckGo is blocking this IP. A web-search provider key or a SearXNG instance is needed for web results.

## WS-10 Research, new data sources, output format, live audit and end-to-end runs

**Research (2026-10-16).** These papers informed this round: PaperQA2 (RCS summaries, ContraCrow), ALCE (citation precision and recall), OpenScholar (self-critique, then targeted retrieval), TxAgent/ToolUniverse (ToolRAG), co-scientist (Elo tournaments), STORM, Biomni, Robin, LAB-Bench and BixBench. Adopted so far:

- a targeted retrieval round for gaps (this already existed)
- per-passage evidence levels
- grounded-statement rules for hypotheses

**New sources, all free with no key, verified live:**

| Source | What it adds | Live result |
|---|---|---|
| openFDA Drugs@FDA | Approval status, first approval date and efficacy supplements per drug. New verifier check `drug_fda_approval`. | Olaparib NDA208558 (2014-12-19, 15 supplements). Pembrolizumab BLA125514 (108 supplements). |
| DGIdb 5 | Drug–gene interactions | KRAS → 126 drugs, adagrasib and sotorasib first |
| Pharos/TCRD | Target development level | MYC Tchem, KRAS and EGFR Tclin |
| BindingDB | Affinities matched to named drugs by structure, with a mutant caveat | EGFR/erlotinib IC50 0.029 nM (PMID 37197473). The service later had an empty-body outage, which is now reported as a failure. |
| PubTator3 + LitVar2 | Entity-precise literature and variant literature | KRAS + PDAC gives 10 PMIDs. T790M → rs121434569 (12,196 papers). |
| SynLethDB 3.0 | Synthetic-lethality pairs from a local bulk file | Zenodo file downloaded to the C: cache. `PHRONESIS_SYNLETHDB_DIR` added to .env. |

**Answer format.**

- Bottom line first, with "Confidence: high/moderate/low (reason)".
- Evidence levels after citations, with `evidence_level` carried on every graded passage.
- Effect sizes given with endpoint, population, comparator, CI/p and follow-up.
- FDA approval kept separate from trial phase.
- Conflicting evidence gets its own paragraph.
- "What would change this conclusion".
- A summary table is required for comparison and repurposing questions.
- `<retrieval_date>` is in the request, and time-sensitive facts say "as of".
- The prompt cap is raised to 14,500 bytes.

**Live audit fixes.**

- **Lumicell:** annotation went from 57 s to 0.05 s (whole run 8–30 s); timeout raised to 180 s.
- **Entity resolver:** it is now a failure when every upstream fails, with a 12 s budget.
- **ClinVar:** records are ranked, so KRAS G12C → rs121913530.
- **EFO→MONDO:** handled through OLS4.
- **GWAS:** a gene + disease query with no hits falls back to a labelled disease-only query.

**End-to-end round 1** (test backend on port 8001; scripts in the scratchpad `e2e/`):

| Run | Result |
|---|---|
| Q3 MYC swarm | Clean, score 1.00 |
| Q6 repurposing | Passed 1.00; a lung-cancer figure was correctly removed by POPULATION_MISMATCH |
| Q4 melanoma | Now finds KEYNOTE-716 and CheckMate 76K with their RFS HRs |

Fixes from round 1:

- **Trials tool input was too strict.** It now accepts "Phase 3", "Active, not recruiting" and lists of interventions; an unknown status only errors when nothing valid remains.
- **Contradicted claims were not removed.** The model's typography (U+202F, U+2011) defeated sentence lookup. `normalize_typography` is now applied to the answer.
- **Internal keys leaked.** Bare `snake_case` keys in parentheses are removed.
- **Literature filtered out everything.** A literal code given only as an example (MRTX1133) is relaxed when no record names it, and a `<search_note>` tells the model.
- **Plan steps showed false errors.** A step whose input is empty is now marked skipped, with a neutral icon, instead of an error.

**Configuration still needed:**

- A web-search provider (`TAVILY_API_KEY`, or `PHRONESIS_SEARXNG_URL`), since DuckDuckGo blocks this host.
- `PHRONESIS_DEPMAP_DIR` for DepMap.
- The NLI cross-encoder in the HF cache.
- Disk space: A: has about 400 MB free.

## WS-11 Round-2 and live-verification fixes (2026-10-17)

**End-to-end round 2:** Q5 PARP and Q4 melanoma passed at 1.00. In round 1 they scored 0.45 and 0.00.

**Fixes since:**

| Area | Problem | Fix |
|---|---|---|
| Web search | DuckDuckGo blocks this host | **Tavily** provider (`TAVILY_API_KEY`, set in .env), tried first, with DuckDuckGo as fallback. Brave was removed. Verified live. |
| Claim audit | The "Confidence:" line, "*Evidence level:*", the FAERS caveat, "H1: … will …", "would/could…" proposals, pipeline descriptions and the "What would change" / "Next experiments" sections were audited as claims | Treated as non-findings (`_CONFIDENCE_LINE_RE`, `_PROPOSAL_PATTERN`, `_NON_FINDING_SECTION_RE`) |
| Claim audit | Claims judged against the wrong source (PrimeKG claim vs OpenTargets doc) | `prefer_named_sources`: a claim naming PrimeKG/FAERS/DGIdb/ChEMBL/… is checked against that tool first |
| Claim audit | Correct "Sotorasib FDA-approved (NDA 214665, 2021-05-28)" removed as contradicted (checked against adagrasib's record) | `claim_id_tokens`: NDA/BLA numbers and ISO dates must appear in the supporting record. Nested record sections (`approval`) get their own document. |
| Claim audit | Exact posted-result figures overridden by the judge, which only saw the first 1,500 characters | `judge_window` centres on the claim's figures. When figures and IDs already match, an inconclusive judge cannot downgrade the claim, and a contradiction only lowers it to unsupported. |
| Plan execution | Empty references (`ingredient=[]`), blank drug args, planner pointing at the wrong field | An empty value is treated as no input. Drug arguments fall back to `run_known_drugs` (ChEMBL approved drugs for the target, then resolved drugs, then DGIdb approved, then question drugs). Blank drug args are filled at run time instead of the step being rejected. |
| Agents | Trivially wrong tool args (over-long lists, limit > 50, objects where names were expected) | `tools/arg_repair.py`, opt-in for agent-written calls only. Only size fields are clamped, never scientific values. ChEMBL takes up to 8 names. |
| Agents | Repeated identical call shown as an error | Reuses the earlier result with status "skipped" |
| openFDA | Failed when ChEMBL was down | Falls back to querying by the user's drug name, and says so |
| Synthesis | Rewrite/revision hit the 600 s stream cap; ReadError mid-revision | Edit rounds run with Nemotron reasoning off (`extra_body` is now forwarded in streaming). Stream cap 1800 s. A buffered edit stream is retried once after a dropped connection. |
| Prose | "BioDiscovery" in the answer text | Rewritten to "the evidence-weighted ranking" (whole word only) |
| Hypotheses | Off-disease fallback hypothesis (Noonan syndrome for NSCLC) | Kept to the question's disease when it names one |

**Live verification question (KRAS G12C NSCLC, sotorasib vs adagrasib).** The run on 2026-10-17 01:59 had every fact right:

- NDA 214665 / 2021-05-28
- NDA 216340 / 2022-12-12
- CodeBreaK 200 HR 0.663 (0.509–0.864)
- KRYSTAL-12 HR 0.58 (0.45–0.76)
- rs121913530
- Pharos Tclin

The claim-audit issues it exposed are fixed above.

**Still open:**

- **DGIdb includes questionable interactions.** For example, HCV protease inhibitors are listed as KRAS "inhibitors". This is upstream data; the answer did not use them.
- **Optional configuration:** DepMap file; NLI cross-encoder (blocked by disk space: A: has about 400 MB free).

### Final verification (2026-10-17)

Fixes after WS-11:

- **Approval claims.** NDA/BLA numbers and dates count toward a match only together with the drug name, so another drug's record never supports a claim. A table row covering several records is checked part by part. "[Synapta]"-style bracket tags are rewritten. Instruction sentences ("Optionally, mine …", "Extract …") are not audited.

Verification question (KRAS G12C NSCLC, sotorasib vs adagrasib, Plan & execute), run end to end through `/api/chat` on the final code:

| Result | Value |
|---|---|
| Verification score | **1.00**, passed |
| Claims | 6 checked, 6 supported |
| Failed steps | 0 |
| Internal names in the answer | 0 |
| Run time | 238 s |
| Facts present | NDA 214665 / 2021-05-28, NDA 216340 / 2022-12-12, HR 0.663 and 0.58, Tclin |
| Format | Comparison table and confidence line present |

`pytest`: 1240 passed, 6 skipped. Web: typecheck clean, 29/29.

**Still needs the owner:**

- Free disk space on A: (about 400 MB free; `phronesis_codebase.zip`, 368 MB, is a candidate).
- Optionally, DepMap `CRISPRGeneEffect.csv` (`PHRONESIS_DEPMAP_DIR`).
- Optionally, the NLI cross-encoder model in the HF cache.

## WS-12 Second verification question and streaming (2026-10-17)

**Question:** BRCA2 synthetic lethality, olaparib and niraparib approvals, SOLO-1. Run in Plan & execute and in Supervisor.

| Problem | Fix |
|---|---|
| Correct approval lines removed as a "population mismatch with a leukemia study" (the olaparib label warns about MDS/AML) | The population check applies only to literature passages, never to tool records or approval IDs or dates |
| Approval dates of two drugs in one sentence were unsupported | Each NDA number or date is matched to the drug named just before it, in that drug's record |
| A PMID carried inside the SynLethDB record was "not checked" | A tool record that contains the cited ID is used as the evidence (`_tool_docs_naming`) |
| Honest caveats ("established background … not present in the retrieved evidence") counted as failures | `_DISCLOSED_GAP_RE`: disclosed gaps and labelled background are not audited |
| "What evidence would change…" heading not recognised | Wider `_NON_FINDING_SECTION_RE` |
| Mixed-source comparison-table rows (approval + HR + FAERS) could never be supported | Whole row first; otherwise each cell is checked against its own source, and every figure must match (a wrong CI is still caught) |
| FAERS table check read "2014" (approval year column) as a report count | Columns whose header is a year, approval, NDA, phase, potency or ratio are skipped |
| "SOLO-1" read as an ambiguous gene "SOLO" | Trial names followed by trial or phase wording are not gene tokens |
| Hypotheses said "inhibiting BRCA2" (a tumour suppressor) | `is_loss_of_function_target`: tumour suppressors and genes the question calls "lost" or "deficient" get synthetic-lethal partner hypotheses from SynLethDB experimental pairs ("Inhibiting PARP1 is predicted to be selectively lethal in BRCA2-deficient …"). The LLM hypothesis rule also forbids inhibiting such genes. |
| DGIdb listed HCV protease inhibitors as KRAS inhibitors | Single-source, no-publication interactions are marked `evidence="single source, no publication"`, ranked last, and the model is told not to present them as acting on the gene |
| Plan steps skipped when a gene or target reference came back empty | `run_known_genes` (SynLethDB experimental partners when the reference pointed at SynLethDB, else the question's genes). A ChEMBL target ID is resolved through `chembl_resolve_drugs` before the call. |
| Streaming looked bursty | Smooth reveal (`web/src/lib/smoothText.ts`, `useSmoothText.ts`, `SmoothMarkdown.tsx`): about 220 chars/s, adaptive, at most 1.5 s of lag, word-boundary steps, prefix-preserving revisions, cross-fade on full rewrites, stable partial markdown, caret, reduced-motion support |

**Supervisor-mode run** of the BRCA2 question on the final code:

- 26+ dialogue turns, 0 failed steps.
- 6 of 6 claims supported.
- All facts correct: PARP1; NDA 206162 / 2014-12-19; NDA 208447 / 2017-03-27; SOLO-1 HR 0.30 (0.23–0.41).
- No "inhibit BRCA2".
- The one remaining defect (the approval-year column) is fixed above.

`pytest` 1247 passed, 6 skipped. Web 46/46.

### Final verification (2026-10-17)

Fixes after WS-11:

- **Approval claims.** NDA/BLA numbers and dates count toward a match only together with the drug name, so another drug's record never supports a claim. A table row covering several records is checked part by part. "[Synapta]"-style bracket tags are rewritten. Instruction sentences ("Optionally, mine …", "Extract …") are not audited.

Verification question (KRAS G12C NSCLC, sotorasib vs adagrasib, Plan & execute), run end to end through `/api/chat` on the final code:

| Result | Value |
|---|---|
| Verification score | **1.00**, passed |
| Claims | 6 checked, 6 supported |
| Failed steps | 0 |
| Internal names in the answer | 0 |
| Run time | 238 s |
| Facts present | NDA 214665 / 2021-05-28, NDA 216340 / 2022-12-12, HR 0.663 and 0.58, Tclin |
| Format | Comparison table and confidence line present |

`pytest`: 1240 passed, 6 skipped. Web: typecheck clean, 29/29.
