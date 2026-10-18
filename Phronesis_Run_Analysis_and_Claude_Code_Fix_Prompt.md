# Phronesis: observed run analysis and Claude Code repair prompt

Date: 18 October 2026. Run shown: `run_a1675cbac94c4352` (transcribe from the original export before using as an automated lookup key).

## Verdict

This run demonstrates a slow, budget-exhausting research workflow with both useful transparency and material evidence-handling defects. The strongest observed performance problem is repeated large answer generation around a persistent regulatory-evidence gap. A separate, confirmed content error transfers an NSCLC study's finding to colorectal cancer. The citation-review interface also displays inappropriate evidence links and rejects some values that match a primary abstract.

The evidence supports concrete behavioral findings, not exact source-code line numbers: the repository and ZIP remain unavailable. No code was edited. This report is based on 18 supplied screenshots, the long PNG capture supplied as `38003a6e-e74b-4f92-a239-8bf344756451`, the earlier approval screenshots, and independent primary-source checks. The long attachment is an image, not a machine-readable trace or code archive. Text extraction was checked against the images for the findings reported here.

**Provisional reviewer assessment: 46 assessed points out of the 94 answer-quality points available from this evidence; the remaining 6 reproducibility/tool-execution points are unassessed, giving 46–52/100.** This is a judgment from one run and spot checks, not a validated benchmark, a statistical confidence interval, or a score for the whole codebase. A full claim audit can change the assessed points too. Research evidence quality gate: FAIL pending correction of material context and provenance errors.

## 1. Timing and resource evidence

| Metric | Displayed observation | Interpretation |
|---|---:|---|
| Overall activity duration | 14m 13s | 853 seconds for this run; not a measured p95 |
| Resolved framework | Auto → Multi-agent debate | The screenshots show Auto, not a manually selected Plan-and-execute run |
| Activity steps | 44 | UI events are not necessarily independent tool calls |
| Model calls | 38 / 100 | Considerable LLM activity for one response |
| Tool calls | 30 / 150 | Tool and model counts are different metrics |
| Estimated token use | 247.8k / 250k | A cumulative run budget, not proof of a model context-window limit |
| Hard stop explanation | Next prompt 3,262 tokens; only 2,215 remain | The next call was prevented by the run budget; actual billing is unavailable |
| Run panel duration | 14m 7s | Slightly different accounting boundary from the overall activity header |
| Run panel phase-chart total | 4m 36s | Chart does not account for all of the displayed overall duration |
| Subquestions | 5 / 7 answered | Regulatory question partial; final compound hypothesis/instructions question open |
| Initial model simulation | 0.5s | Not the dominant observed latency |
| Approval event | 2.5s | Human approval waiting does not explain this run's 14 minutes |

### Where much of the time went

The activity screenshots display:

| Text-generation event | Duration | Draft length where shown |
|---|---:|---:|
| Initial answer | 2m 26s | 1,651 words |
| Rewrite with new evidence | 1m 14s | 2,237 words |
| Another rewrite | 1m 13s | 2,121 words |
| Another rewrite | 1m 23s | 1,949 words |
| Another rewrite | 1m 17s | 1,971 words |
| Final answer revision | 1m 32s | Not shown in that event |
| **Sum of these displayed durations** | **9m 05s** | |

That sum is about 64% of the overall 14m 13s **if the event durations are exclusive**. Raw spans are required to rule out nesting/overlap and determine the exact critical path. Do not add the phase-chart totals to these figures as if all measures were disjoint.

Additional displayed work includes five gap checks/search rounds in the aggregate panel, an initial 42s counter-evidence step, a 58s debate event, two rounds involving three critics, and a 24s semantic-check precomputation phase. The six critic entries do not prove six serial calls; parallelism is not visible.

The sequence repeatedly searches for FDA evidence, rewrites the whole answer, searches a similar gap again, and eventually reaches a search with zero new passages before another rewrite. The screenshots do not establish an infinite loop, a deadlock, or a provider outage. They establish repeated bounded work with poor completion efficiency in this run.

## 2. What explains the apparent stalls

### A. Auto routed a broad research brief to debate

The router displays Debate 56%, Plan & execute 44%, and a rationale that the question asks for hypotheses. It also displays feature weights such as `asks for hypotheses +2.4` and `contested claim +2.4`. These look like routing scores; their calibration is not demonstrated.

The user requested two hypotheses within a larger evidence brief. That can justify targeted criticism of those hypotheses, but does not by itself justify repeated whole-answer debate. Preserve the requested framework, explain Auto's cost tradeoff before approval, and evaluate whether a normal evidence-brief workflow plus one focused review achieves equal or better quality.

### B. Regulatory evidence was sought too late and repeatedly

The initial plan switches off openFDA because no drug was named, despite an explicit request for FDA-approved indications. Later retrieval finds FDA and manufacturer records through web search. Thus regulatory retrieval did happen; the finding is inefficient planning and incomplete source policy, not total absence of FDA evidence.

Resolve drug candidates from the class/biomarker question, then retrieve primary FDA approval/label evidence before drafting. openFDA is one possible adapter, not a mandatory exclusive solution. Direct official FDA records can satisfy the obligation. The plan should express the evidence requirement, not only a brittle drug-name trigger.

### C. Full rewrites consume time and cumulative tokens

The visible changes are repeatedly driven by one regulatory gap, yet each revision regenerates approximately 2,000 words. Retain stable structured evidence and patch the affected claim/section. Stop when the required evidence exists, when another attempt adds no usable evidence, or when the allowed repair pass is exhausted. Do not remove validation to obtain speed.

### D. The run spends its budget before finishing

The panel reports a 250k-token cap with insufficient remaining budget for the next 3,262-token prompt. Reserve enough budget for validation, a final response, and durable terminal state before launching optional work. Estimate prompt plus requested output, reconcile with actual provider usage, and allocate atomically under concurrency. A budget stop should produce a clearly marked partial result listing unresolved obligations.

### E. Progress reporting cannot yet explain every wait

The phase chart totals 4m 36s while the run panel reports 14m 7s. That does not prove computation was idle: drafting, provider queueing, streaming, retries, or other stages may simply be excluded. Instrument them. The screenshots also cannot show the longest no-update interval; an event timeline is needed to measure perceived stalling.

Provide named stage events, periodic liveness updates, reconnect support, and a truthful last-progress timestamp. A heartbeat means the worker is alive, not that scientific progress is being made. Stream independently checked sections or clearly labeled provisional content; do not make unchecked prose look final.

## 3. Evidence and answer-quality findings

| ID | Finding | Evidence and significance | Required change |
|---|---|---|---|
| E1 | Wrong disease context in final prose | Answer attributes gefitinib/erlotinib resistance evidence from PMID 23313110 to CRC; that study concerns advanced NSCLC [S1] | Bind claims to study disease, intervention, biomarker, and population; block or qualify cross-context transfer |
| E2 | Inappropriate citation links in verification UI | FDA-status summary is marked supported by `tool:vivarix`; another FDA-label claim by `tool:synapta`; several approval statements point to PMID 42636835, a different trial paper | Trace canonical evidence IDs through backend, serialization, claim matching, and renderer; inspect actual payload before deciding where mapping breaks |
| E3 | Correct values rejected by current support check | The UI rejects Johnson 2024 N=102/164, median OS=10.2, intervals, and HRs as not found; those values appear together in the public abstract of PMID 39369609 [S2] | Preserve source passage boundaries/structured fields and all relevant evidence links; investigate missing passages, parsing, truncation, units, and wrong pairing rather than calling these fabricated numbers |
| E4 | MAIC grouped as an RCT | Literature card and evidence labels place PMID 42173791 under randomized trials, but the actual comparison is matching-adjusted and indirect [S3] | Separate raw indexing tags from the design of the reported analysis. PubMed itself includes RCT/phase III tags here, so blindly copying metadata is an especially plausible cause, not a proven parser bug |
| E5 | Subgroup denominator error | Answer presents n=45 as the KRAS G12C subgroup size for He 2026. The paper identifies 45 as the entire KRAS-mutant group, with G12C and non-G12C subsets [S4] | Store denominator scope and subgroup identity with every effect estimate; do not attach a parent-cohort N to a subgroup result |
| E6 | Therapy category mixes interventions | An ICI-treated observational cohort appears under KRAS-targeted monotherapy; ifebemtinib + garsorasib is labeled CRC monotherapy despite being a combination | Derive setting from the actual regimen and context, not the cancer or gene alone |
| E7 | Simulation scope and dimensional consistency need review | A 60s cetuximab-resistance model supports generic model observations, not automatically a drug-specific cross-cancer hypothesis; the chart/table use concentration units while prose reports mmol | Validate SBML species meaning, units, mapping, solver, perturbations, and scope before including outputs as biomedical evidence |
| E8 | Conclusion depends on a negative retrieval inference | Final prose says CRC monotherapy remains investigational because no approval records were retrieved | Distinguish verified nonapproval from failure to retrieve. Use a sufficiently complete authoritative check or state current approval status is unverified |
| E9 | Experimental falsifier is too weak | Proposed SHP2 hypothesis uses absence of statistical significance as a falsifier | Prespecify effect size, design, uncertainty, and decision rule. A nonsignificant result alone does not establish no effect |
| E10 | Verification counters/status need versioning | Trace reports 9 claims unverified; final banner 8/30; support values also differ slightly across stages | Persist answer/evidence/verification version IDs and explain whether later edits changed the set. Do not assert a bug from differing snapshots alone |
| E11 | Misleading overall completion label | Top status says requested operation completed/time course, while run says budget exhausted and 5/7 answered | Separate tool completion from request completion; make terminal partial status and reason prominent |
| E12 | Potential memory contamination | Trace says 41 memory nodes were written after unresolved claims remained | Inspect what was actually saved. Do not assume all 41 are wrong; ensure confidence, verification state, source, scope, and version survive persistence and retrieval |

### Important findings that are not errors

- The answer's Hochmair 2024 values of n=104, ORR 32.7%, median PFS 5.4 months, and median OS 13.0 months match the primary abstract [S5]. The source also says the dose-comparison study was not powered for formal hypothesis testing; that qualification should be retained.
- The Johnson values mentioned above match the public abstract, although its linked corrigendum should also be checked before certifying a current extraction. Correct factual values can still be unsupported by the passages actually provided to the verifier. That is a retrieval/provenance problem, not proof of scientific fabrication.
- FDA records support the stated combination approval events: adagrasib + cetuximab in the specified prior-treated CRC setting on 21 June 2024 [S6], and sotorasib + panitumumab in its specified prior-treated metastatic CRC setting on 16 January 2025 [S7]. These facts do not validate the UI's unrelated evidence links.
- PMID 42636835 resolves to an ifebemtinib/garsorasib trial record [S8]. Do not label a real citation fabricated merely because it is incorrectly attached elsewhere.
- The detailed support panel explicitly says scores are not truth probabilities. That is good labeling. The calibration sample size is not reported, so empirical calibration remains unestablished.
- The UI exposes failures, skipped sources, budget state, trials, model trajectories, and evidence cards. This is useful transparency, but it must agree with the final claim statuses.

Live recruiting status and last-update dates for the three listed NCT records were not independently verified here: registry pages did not expose the needed data and the API was inaccessible through the available retrieval route. Therefore this report neither confirms nor rejects those statuses. The final answer includes a retrieval date but omits individual registry last-update dates requested in the query.

## 4. Provisional scoring

These deductions judge the delivered answer and displayed evidence behavior, not backend implementation completeness. Detailed source access is incomplete, so this is a preliminary reviewer assessment rather than exhaustive factual certification.

| Dimension | Points | Reason |
|---|---:|---|
| Factual and quantitative correctness | 12/20 | Several checked numbers are correct; wrong disease transfer, subgroup N, therapy grouping, and model-unit inconsistencies remain |
| Citation entailment/provenance | 5/20 | Many real IDs and URLs; central claims have wrong displayed evidence partners and some numerical support checks reject values found in the proper source |
| Evidence interpretation | 6/12 | Cross-trial limitations are discussed, but MAIC/RCT classification and model/clinical distinctions are not reliably maintained |
| Research/clinical scope | 6/12 | Avoids a personalized treatment recommendation and distinguishes combination indications; disease/regimen context errors are material |
| Coverage/counter-evidence | 6/10 | Most requested sections appear, but mechanism comparison is thin, dates/context are missing, and the run itself shows incomplete obligations |
| Freshness/source authority | 4/8 | Run date and some official records appear; one manufacturer release is labeled as an FDA announcement, and regulatory completion/current trial validation remain incomplete |
| Uncertainty/abstention | 5/8 | Useful warnings and nonprobability label; insufficient, unchecked, neutral, contradicted, and completed states still need clearer separation |
| Reproducibility/tool honesty | Unassessed /6 | Screenshots report tools and outputs but do not supply raw payloads, executable model provenance, exact settings, or a complete trace |
| Clarity/usefulness | 2/4 | Helpful sections/tables, but raw `agent_evidence`, `untrusted_data`, and empty `key` fields leak into prose; table overflow and status conflicts impede use |
| **Assessed subtotal** | **46/94** | |
| **Possible total with six unassessed points** | **46–52/100** | Not rescaled; not a statistical interval |

Performance is reported separately because latency is not one of the original answer-rubric dimensions. This run took 14m 13s and ended partially after reaching its token budget. Do not hide that behind a high text-quality score or average it into clinical correctness.

## 5. Fix order

1. **Protect evidence correctness:** disease/regimen/denominator binding; canonical citation links; correct missingness states; raw versus interpreted study design.
2. **Bound work:** one targeted default repair pass, novelty-aware stopping, finalization budget reserve, and no whole-answer rewrite for unchanged evidence.
3. **Improve routing and retrieval order:** respect explicit modes; make debate opt-in or clearly justified; satisfy regulatory obligations before drafting; avoid irrelevant simulation.
4. **Expose true progress:** account for every major span; stream honest state; terminal partial status on budget stop; enforce cancellation and reconnect behavior.
5. **Protect persistence:** store unresolved claims as unresolved with provenance; keep them from becoming verified facts on later retrieval.

Increasing the token cap, switching providers, or removing verification should not be the first fix. The visible behavior already identifies redundant work and unreliable source alignment. Provider throughput, caching, worker capacity, and network delays should be measured after instrumenting the critical path.

## 6. Immediate next comparison

Run the same query once with **Plan-and-execute explicitly selected**, using the same provider/model, tier, source setup, and budget. Keep the current Auto/Debate result as the baseline. Record cache state; a warm-cache second run is not a clean latency comparison. If you disable simulation in a separate run, label it as a separate intervention rather than attributing all improvement to the framework.

Collect the Copy/Export output plus the run manifest and event trace if the application exposes them. Do not assume an export endpoint exists. Useful metadata: model ID; framework requested/resolved; budgets; generation settings; actual token usage; source versions; UTC start/end; provider request IDs with secrets removed; answer/evidence/verification versions. Keep tokens and private biomedical data out of shared traces.

Proposed product policies, to be validated against the actual provider and deployment:

- Acknowledge a started request promptly; target within 2 seconds in the controlled test environment.
- Emit liveness events at most 5 seconds apart, with last real-progress time and current stage.
- When a stage exceeds its configured threshold, show that it is still waiting or retrying; do not manufacture a percent complete or predicted ETA.
- An interactive research budget of roughly 180–300 seconds is a **proposed cap**, not a promise of a fully verified answer. On expiry return a useful partial result or offer continued background work while preserving quality labels.
- Keep longer debate/deep-research runs explicit. Set deadlines using actual request scope and measured latency; do not apply one arbitrary timeout to every task.
- Cancel should promptly acknowledge and stop future optional work, with external in-flight call limitations stated accurately.

## 7. Claude Code prompt

Paste this in Claude Code at the root of the actual Phronesis repository. It supplements the earlier broad audit prompt with the specific observed regression case. It authorizes local fixes and tests, not publishing or deployment.

```text
Audit and fix Phronesis's latency, evidence integrity, and incomplete-run handling using the supplied screenshots and this report as a regression case. Inspect real code first. Do not assume exact implementation paths beyond the README's entrypoints: app/backend/main.py, phronesis/, and web/. Preserve user changes. Do not deploy, push, expose credentials, fabricate traces, or replace the whole architecture without a demonstrated need.

OBSERVED BASELINE
- Same KRAS G12C NSCLC/CRC research query: Auto selected Debate.
- 14m13s, 44 steps, 38 model calls, 30 tool calls, estimated 247.8k/250k tokens.
- Budget stop: next 3262-token prompt did not fit the remaining 2215 tokens.
- UI says 5/7 subquestions answered; regulatory task partial and final compound task open.
- Initial draft 146s; four whole-answer rewrites 74/73/83/77s; final revision 92s.
- Repeated regulatory gap retrieval, including a round with zero new passages.
- Phase chart totals 4m36s but run panel reports 14m7s; approval event is 2.5s.
- Initial simulation is 0.5s; do not blame it for most latency.
- Planner model call failed; fallback evidence was used.
- FDA summary claim is displayed as supported by tool:vivarix; FDA-label claim by tool:synapta; some approval claims by unrelated PMID 42636835.
- PMID 23313110 concerns NSCLC but the final answer applies the finding to CRC.
- Johnson 2024 numerical values match PMID 39369609's public abstract but the UI flags them as absent.
- PMID 42173791 is a MAIC analysis; raw PubMed metadata also contains RCT/phase III tags. Preserve raw tags, but label the analysis as indirect comparison.
- PMID 41660470's n=45 is the whole KRAS-mutant group, not automatically the G12C subgroup.
- A combination is labeled monotherapy; model units differ across chart and prose.
- UI says operation completed/time course while the research run is budget-exhausted.
- 41 memory nodes were written with unresolved claims still present; inspect actual saved content.

1. BASELINE AND CODE MAP
Read AGENTS.md and applicable instructions. Record commit, branch, working-tree status, actual model/provider configuration without values of secrets, dependency versions, and available tests. Locate the router, request decomposition, framework registry, execution wrapper, evidence normalization, gap detector, critic loop, compiler, verifier, confidence logic, stores, event schemas, web stream handler, progress renderer, and export renderer. Build a verified path/symbol map. Separate confirmed bugs, risks, configuration problems, and proposed improvements. Do not invent file:line references.

2. REPRODUCE AND INSTRUMENT BEFORE OPTIMIZING
Create a deterministic fixture replay of the supplied question and relevant sanitized records. Fixtures are tests, never live scientific evidence. Reproduce actual payload mappings and transitions where available; otherwise clearly state the missing evidence. Capture raw original outputs if the user exports them. Keep a separate live-provider/live-source benchmark for timing.

Use monotonic spans with parent/child IDs for route, parse, decompose, approval wait, queue wait, provider connection, first token, generation, each tool attempt, retry/backoff, cache access, normalization, evidence merge, gap detection, targeted repair, critics, verification, final render, persistence, and export. Record provider usage and estimated usage separately. Distinguish critical-path wall time, inclusive spans, exclusive spans, and summed work; parallel sums need not equal wall time. Track time to acknowledgment, first meaningful event, first usable checked section, final/partial result, and maximum no-event interval. Do not log secrets, hidden reasoning, or unnecessary private data. Emit bounded stage summaries rather than chain-of-thought.

3. REPAIR CANONICAL CLAIM/EVIDENCE LINKING FIRST
Give evidence immutable IDs and retain source record, URL, exact passage/structured field, disease, biomarker, intervention/regimen, population, denominator scope, timepoint, units, source date, retrieval date, and provenance. Use stable claim IDs with an answer version. Trace the same mappings through verifier inputs, JSON serialization, persisted manifests, UI cards, citations, and exports.

Write regression cases proving that a regulatory claim cannot be certified by a simulator result or generic target association. A factual claim being true elsewhere does not repair an incorrect attached source. Preserve all evidence links needed by a compound claim or split it into atomic claims. Do not repair UI labels without checking the actual payload association.

Add negative controls for correct numbers in the wrong disease, arm, subgroup, unit, or timepoint, and positive controls for the Johnson values in the correct abstract. Diagnose extraction/chunking/truncation/linking failures rather than treating an unsuccessful lookup as proof a number is false. Retain original source text, normalize units carefully, and use tolerances only with valid conversions. Check corrigenda and record whether corrected content was actually applied.

Separate external publication tags from interpreted analysis design. For MAIC, mark the reported comparison as indirect even when raw metadata includes RCT tags; retain provenance and uncertainty. Preserve monotherapy/combination/ICI distinctions and parent-cohort versus subgroup denominators. Prevent PMID 23313110 from supporting a CRC-specific claim. Verify simulator units and species semantics before including numbers in prose.

4. MAKE THE WORK LOOP BOUNDED AND USEFUL
Represent obligations explicitly: mechanisms by disease, clinical evidence by regimen/design, exact FDA indications, counter-evidence, recruiting trials with update dates, and two hypotheses. Get UTC date deterministically from the server; do not ask the LLM to discover it or create a scientific dependency on a date subquestion. Keep formatting instructions separate from scientific subquestions; the observed final subquestion incorrectly absorbs a large block of global instructions.

Resolve class/biomarker-to-drug candidates and fetch primary regulatory evidence before drafting. Do not require a drug literally named by the user to recognize an FDA-status task. Direct FDA records can satisfy the requirement; do not require openFDA exclusively. Keep manufacturer announcements labeled as manufacturer sources and distinguish approval records from application/review documents and proposed indications. No-result retrieval is not proof of nonapproval.

After approval, execute independent retrieval concurrently within rate/resource limits. Respect real dependencies and user approvals. Do not assume current code is serial until traced. Avoid irrelevant simulation by requiring a justified model, question, perturbation, and applicability; preserve explicit valid simulation requests.

Use one shared orchestrator state for requirement coverage and unresolved evidence gaps. Deduplicate query attempts and passages. Track evidence novelty, usability, and which obligation it resolves. Stop a gap when answered; stop or mark unresolved after repeated equivalent queries or no usable gain. Default to one focused repair pass for an interactive brief; longer review must be an explicit mode with a finite cap. Do not endlessly rephrase an unresolved query.

Keep structured evidence and verified sections stable. Patch only affected claims/sections, then recheck those changes and dependent summaries. Do not regenerate the entire answer on each new passage. Reject speculative critic suggestions unless supported by evidence or a clear methodological requirement. If a proposed revision changes no supported content, end it. Preserve one final consistency check without reentering the repair loop.

5. BUDGET TO FINISH
Separate cumulative run budget, provider context limit, output limit, elapsed deadline, and cost policy. Before every call reserve input plus output plus enough remaining capacity for validation, final response, and durable terminal state. Estimate with the configured tokenizer when available and reconcile provider usage after each call. Allocate/release reservations atomically for concurrent calls. Never spend the finalization reserve on optional critics or broad retrieval. Make retries consume the same run deadline and ledger, not reset them.

When budget is insufficient, produce a valid partial answer from existing evidence, unresolved requirements, accurate verification status, and a resumable checkpoint if supported. Do not mark the whole request complete because a simulation completed. The error path must not require another large LLM prompt. Implement deterministic fallback formatting for a budget stop. Keep outputs and logs free of secrets.

6. RESPECT MODES AND ROUTE BY VALUE
Explicit user framework selection must survive all layers. Auto's hypothesis keyword alone must not force a full debate. Use task scope, uncertainty, evidence need, expected extra quality, and remaining resources. Explain the selected mode and expected resource tradeoff before approval. Treat heuristic routing weights as suitability scores, not calibrated probabilities. Compare normal Plan-and-execute plus one targeted review against Debate on matched questions and budgets. Do not silently degrade if the planner fails; report the fallback mode and its consequences in structured status.

7. STREAM TRUTHFUL PROGRESS AND FIX TERMINAL STATE
Emit an acknowledgment, stage events, bounded liveness heartbeats, and last-real-progress time. A heartbeat is not evidence progress. Show waiting for approval, provider generation, retry/backoff, source retrieval, verification, cancellation, and partial completion distinctly. Stream already checked sections where feasible; provisional drafts must be visibly provisional and must not appear verified. Preserve ordered events and reconnect cursors; deduplicate replayed events; stop emissions and optional work after cancellation/terminal state as appropriate.

Use one versioned source of truth for answer text, evidence, verification counts, confidence summary, and terminal status across UI/export. Explain snapshot changes instead of showing unexplained 8-versus-9 counts. Keep heuristic support separate from probability of correctness; no NLI check, neutral, insufficient, contradicted, and inference failure are distinct. Remove raw agent_evidence/untrusted_data/empty-key serialization from normal prose while retaining an inspectable evidence view. Make wide clinical tables readable without silently hiding comparator or uncertainty columns.

8. PERSIST WITH PROVENANCE
Inspect the 41 saved nodes if available. Preserve source, owner/project scope, answer/evidence version, verification state, and uncertainty. User preferences, hypotheses, retrieved facts, and unsupported claims need distinct types. An unresolved claim may be retained as a research note, but must not become a verified fact in later answers. Test deletion and supersession so corrections propagate.

9. VALIDATE AND REPORT
Add meaningful tests for: repeated equivalent regulatory gaps terminate; zero useful evidence does not trigger a full rewrite; one changed claim repairs only its section; budget reservation remains safe under concurrency; insufficient budget produces a deterministic partial result; deadlines and cancel stop new work; explicit mode is honored; heuristic Auto does not trigger unnecessary full debate; wrong disease/regimen/subgroup/unit source pairs fail; correct numerical extraction passes; MAIC design is preserved; FDA claims use eligible evidence; memory retains unresolved status; UI/export counts match the same version; event order/reconnect/heartbeat works.

Use provider mocks and fake clocks to test liveness deterministically; do not test medical correctness using model consensus. Run actual project checks discovered in package manifests and CI. Candidate README commands include pytest, ruff, environment-reference drift, and web typecheck/test/build; verify exact commands first. Report executed, passed, failed, skipped, and blocked checks separately.

Benchmark with the same model/settings/source fixtures and record cold/warm cache separately. Report repeated runs, p50/p95 only with adequate samples, time to first usable checked section, no-event gaps, wall time, token estimates versus actual usage, tool/model counts, source-grounded correctness, numeric checks, unsupported central claims, and unresolved obligations. Do not claim speedup from mocked provider timing or one favorable run. Product caps such as 180–300 seconds are proposals to test, not guarantees of a completed scientific answer.

Deliver LATENCY_AND_EVIDENCE_AUDIT.md with exact paths/lines, root causes, implemented fixes, before/after evidence, actual checks, remaining blockers, and clear rollback/configuration effects. Include a concise feature/status matrix and machine-readable benchmark data using repository conventions. Continue through local implementation and validation; do not stop at generic recommendations or raise the token cap as the only fix. Never weaken evidence checks, invent sources, hard-code this benchmark answer, or conceal incomplete work to improve latency or scores.
```

## Primary-source checks

Accessed 18 October 2026. These sources support the specific checks above, not an exhaustive current treatment review. No long source passages are reproduced.

- **S1:** [PMID 23313110 — EGFR-TKI response in NSCLC](https://pubmed.ncbi.nlm.nih.gov/23313110/). Confirms the disease context of the misapplied claim.
- **S2:** [PMID 39369609 — real-world sotorasib/docetaxel comparison](https://pubmed.ncbi.nlm.nih.gov/39369609/). Public abstract contains the numerical values flagged by the application; a corrigendum is linked.
- **S3:** [PMID 42173791 — matching-adjusted indirect comparison](https://pubmed.ncbi.nlm.nih.gov/42173791/). Methods describe the indirect comparison; indexing includes RCT tags, illustrating why reported analysis design must be checked beyond tags.
- **S4:** [PMC12876016 / PMID 41660470 — first-line ICI retrospective cohort](https://pmc.ncbi.nlm.nih.gov/articles/PMC12876016/). Distinguishes the full KRAS group from its mutation subgroups and identifies treatment context.
- **S5:** [PMID 39029295 — randomized sotorasib dose comparison](https://pubmed.ncbi.nlm.nih.gov/39029295/). Confirms the checked sample size and endpoint values and the descriptive-study qualification.
- **S6:** [FDA adagrasib + cetuximab approval notice](https://www.fda.gov/drugs/resources-information-approved-drugs/fda-grants-accelerated-approval-adagrasib-cetuximab-kras-g12c-mutated-colorectal-cancer).
- **S7:** [FDA sotorasib + panitumumab approval notice](https://www.fda.gov/drugs/resources-information-approved-drugs/fda-approves-sotorasib-panitumumab-kras-g12c-mutated-colorectal-cancer).
- **S8:** [PMID 42636835 — ifebemtinib + garsorasib trial](https://pubmed.ncbi.nlm.nih.gov/42636835/). Confirms record identity; its full numerical extraction was not independently certified in this review.
