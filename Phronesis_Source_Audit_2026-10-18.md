# Phronesis: source verification after the 18 October fixes

## Scope and result

The uploaded `phronesis_codebase_slim(3).zip` was readable. Archive SHA-256: `75320f30c8ce49897f3a8c8ffb0ec9ba725a24c517aaee92a380ed6c088e2e2b`. The archive contains 1,235 entries. This is a focused source review of the reported latency, budgeting, evidence-verification, and regression-test fixes, not an exhaustive audit of every module or a live biomedical benchmark.

The fixes are substantially present, but important guarantees remain incomplete. Controlled probes reproduced hard-budget overruns, final-reserve consumption by optional calls, first-draft accounting bypass, a streamed-call deadline gap, incorrect deduplication across clinical contexts, weak FDA-origin validation, and residual population/subgroup verification failures.

Backend files remain unchanged. A targeted web patch was implemented in the extracted review copy after the request to open important cards by default; details appear below. Next.js regenerated `web/next-env.d.ts` during the build; it was restored to the exact archived bytes before packaging. No live model calls, authenticated biomedical source runs, production deployments, remote repository writes, or pushes were performed.

## Validation performed

| Check | Result | Limit |
|---|---|---|
| Web `npm test` after installing lockfile dependencies | **82 passed, 0 failed after the UI patch** (76 baseline + 6 new policy cases) | Logic/unit tests, not a browser acceptance run |
| Web `npm run typecheck` | **Passed** | Static typing |
| Web `npm run lint` | **0 errors, 208 warnings** | Warnings remain; not a clean zero-warning result |
| Web `npm run build` | **Passed** | Production compilation/static generation, not deployed behavior |
| Focused backend pytest selection | **Blocked during collection** | The review environment did not contain the full legacy scientific dependency stack; missing imports progressed through langgraph, hydra, and langchain_community as limited dependencies were installed. This is not evidence those tests fail in the developer's full environment |
| Isolated source probes | **Executed successfully; defects reproduced below** | Namespace shells bypassed unrelated package initializers; actual budget/LLM/verifier modules or unchanged AST-extracted source functions supplied tested logic; provider replies were controlled doubles |
| Claimed 1,695 backend passes | **Not independently reproduced** | Do not convert the change log into an independently verified test result |
| Matched live latency improvement | **Not measured** | The latest screenshots report a 6m 9s run, but its scope/settings differ from the earlier 14m 13s run |
| UI interaction checks | **Passed** | Actual React components mounted in happy-dom: automatic updates, retained manual choices, message reset, verification defects, and artifact-context integration. CSS was stubbed; this is not visual/browser acceptance testing |

The isolated verifier probe did not load extension registrations or a live NLI model. For the subgroup case, an explicit semantic-judge result was injected to exercise the actual reconciliation logic. The source-origin and deduplication probes exercised the actual helper definitions directly. These are reproducible unit-level findings; their frequency in live traffic is not measured.

## Confirmed findings

### F1 — First draft bypasses run token and model-call accounting

**Priority: high.** `phronesis/agents/orchestration/draft.py:256–269`, called by `phronesis/agents/authoritative_orchestrator.py:1054`.

`write_first_draft()` creates the streaming model and calls `astream_with_reasoning()` without `RunBudget.charge_llm()`, a per-call reservation, a budget-derived output cap, or usage reconciliation. The surrounding `run.timed()` measures duration; it does not charge the model call. Other pipeline model work may also need review; this finding specifically demonstrates the first draft.

Controlled reproduction: start with a 1,000-token budget already at 1,000 and a one-call budget already at one. Execute the source's first-draft function with a fake provider and minimal I/O scaffolding. It calls the provider once more; the budget remains at 1,000 tokens and one call afterward.

The budget module explicitly says the classic pipeline's own stages are not charged. That may explain the design, but the same first-draft path is used inside agentic runs. The displayed ledger therefore cannot currently be treated as complete run usage.

**Repair:** make first-draft generation use the shared admission/reservation/reconciliation path. If a hard budget cannot admit any model generation, produce a deterministic partial result. If intentionally excluding work, rename and document the reported metric rather than calling it total run usage. Account for every retry attempt, reasoning/output usage, and cancellation outcome.

### F2 — Concurrent outputs are not bounded by their reservations

**Priority: high.** `phronesis/agentic/llm.py:229–277`; `phronesis/agentic/budget.py:187–234`.

`AgentLLM.complete()` reserves an expected 2,048 output tokens, but `_invoke_kwargs()` sets the provider's `max_tokens` from the **remaining global budget**, not the current call's admitted output allocation. It may omit the provider cap entirely when that remainder is at least 16,384. A reply can legitimately exceed its reservation while respecting the cap it was actually sent.

Controlled cases used exact 1,000-token input usage per fake reply, so the failure does not depend on tokenizer-estimation error:

| Case | Provider output caps issued | Reconciled usage | Failure |
|---|---|---:|---|
| Three parallel calls; total budget 10,000; output reservation 2,048 each | 6,952; 3,904; 856 | **11,760** | Hard budget exceeded |
| Two optional calls; total budget 10,000; final reserve 3,000 | 6,952; 3,904 | **9,904** | Optional calls consumed nearly all of the protected final reserve |

The existing concurrency test uses tiny replies and therefore does not exercise this failure.

**Repair:** admit each call with its own maximum input/output allocation; bind that exact output allocation to the provider; reconcile actual usage and release unused allocation. Keep optional and final allocations separate. Account for provider-specific reasoning-token semantics. Reconciliation can report unavoidable estimation differences, but an independent global cap is not a per-call reservation.

### F3 — Rewrite output floor exceeds what the budget can afford

**Priority: high.** `phronesis/agents/orchestration/agent_rounds.py:20–28`, used at `83–93`; also used by verifier revision in `phronesis/agents/orchestration/verify.py`.

The helper returns `max(1024, min(configured, left))`. With 1,100 tokens remaining and a 1,000-token estimated prompt, it returns **1,024 output tokens**, although only **100** remain for output. `charge_llm()` clips the final-call reservation to what fits, but the larger `cap` is still passed to the provider.

**Repair:** remove the budget-violating floor. Either bind the actual admitted cap or refuse the call and produce a deterministic partial response. If the output would be too short to be meaningful, do not spend the remaining budget on a predictably truncated full rewrite.

### F4 — Stream liveness does not enforce the run deadline

**Priority: high.** `phronesis/agents/orchestration/context.py:214–246`; `phronesis/utils/llm/llm_factory.py:287–290` and streaming helper; first draft uses this watcher.

`watched()` polls cancellation and emits heartbeats, but does not inspect the run budget's remaining time. The streaming helper has a separate default total timeout of **1,800 seconds**. Those are different limits.

Controlled reproduction: attach a 0.01-second run budget to the watcher and await a 0.05-second controlled coroutine. The watcher returns `finished` after approximately 0.05 seconds; the budget is exhausted afterward. This directly shows the watcher does not enforce that deadline. It is not a measurement of live provider responsiveness.

**Repair:** use an absolute run deadline and bound each model/tool wait by remaining time. Race long calls against both deadline and cancellation. Propagate partial/deadline status and reconcile reservations. A heartbeat must never be represented as substantive scientific progress. Noncooperative external calls require honest cancellation limitations.

### F5 — Query deduplication removes distinct clinical questions

**Priority: high.** `phronesis/agentic/gaps.py:179–191`, applied at `349–355`; threshold `0.6` at line 64.

The source compares prefix-stem sets using Jaccard similarity and has no protected disease, regimen, or temporal fields. These actual helper calls all classify the second query as a duplicate:

| First query versus second | Similarity | Result |
|---|---:|---|
| `KRAS G12C sotorasib FDA approval NSCLC` versus `KRAS G12C sotorasib FDA approval colorectal cancer` | 0.625 | Second skipped |
| `KRAS G12C sotorasib FDA approval colorectal cancer monotherapy` versus the same scope with `combination panitumumab` | 0.700 | Second skipped |
| Same CRC approval query ending in `2024` versus `2025` | 0.778 | Second skipped |

Suppressing equivalent phrasing is useful. Suppressing different disease/regimen/time obligations can make the run faster by leaving required evidence uncollected. A whole gap is also retired after a search yields no usable result (`gaps.py:427–428`); transient failure, irrelevant results, and a definitive source check should not all have identical implications.

**Repair:** compare structured requirement keys first: target/variant, disease, intervention, combination partner, treatment line, approval jurisdiction, date scope, endpoint. Apply lexical similarity only within equivalent contexts. Stop equivalent retries while allowing a bounded alternative source/strategy; preserve unresolved state.

### F6 — FDA eligibility trusts URLs embedded in text

**Priority: high.** `phronesis/phronesis/agents/verifier.py:180`, `205–211`.

`is_regulatory_doc()` accepts a web document when `_FDA_URL_RE` finds an FDA-looking URL anywhere in its flattened text. It does not validate the result's canonical host. The regex boundary after `gov` accepts a following dot.

Direct helper results:

| Test document | Accepted as regulatory? | Expected |
|---|---|---|
| Canonical URL `https://www.fda.gov.example.test/fake` | **Yes** | No |
| Canonical URL `https://example.test/article`, with an FDA link only in its snippet | **Yes** | No, unless the official linked record is independently retrieved and represented as separate evidence |
| Canonical URL `https://www.fda.gov/drugs/notice` | Yes | Eligible origin; factual entailment still needs checking |

No test host was contacted. These reserved example domains are only input strings. This demonstrates an evidence-origin validation weakness, not a production compromise.

`phronesis/agents/prompts.py:64` already has a hostname-based source classifier, which is stronger than the verifier's separate text regex. The two paths should share a typed authority policy.

**Repair:** preserve canonical/final retrieved URLs as fields; parse the actual hostname; require exact `fda.gov` or a subdomain ending in `.fda.gov`; validate redirects and record identity. A URL mentioned by another page is a citation, not proof that the other page is an FDA record. Continue to verify claim entailment after establishing origin.

### F7 — Population checks skip variant/tool evidence

**Priority: high.** `phronesis/phronesis/agents/verifier.py:2423–2435`.

The mismatch guard immediately returns for sources whose keys do not start with `PMID:`, `DOI:`, or `passage:`. Structured variant/CIViC evidence can also carry a specific disease population, so that exemption is too broad.

Controlled source: a variant-tool record explicitly describing NSCLC and a 5% response. Controlled claim: the same numbers and treatment, but attributed to CRC. The core verifier returned the claim as **supported** with `tool:variant_intel` evidence and no `POPULATION_MISMATCH`. It separately raised a generic citation defect because extension-based identifier registration was not loaded in the isolated probe. That citation defect does not repair the incorrectly supported population assignment. A live semantic judge or another extension might catch it; the deterministic population safeguard does not.

The 5% value in this probe is synthetic test input, not a certified extraction from PMID 23313110.

**Repair:** retain typed disease/population context for all clinically relevant records, including variant records, clinical registries, and labels. Validate individual evidence items rather than exempting whole tool families or rejecting a label merely because it mentions unrelated warnings.

### F8 — Matching numbers can override an explicit subgroup warning

**Priority: high.** `phronesis/phronesis/agents/verifier.py:2519–2529`.

When figures match somewhere in the record, the reconciliation logic ignores a judge verdict of `unsupported`. Numeric presence is being used as protection against inconclusive semantic checks, but it does not establish which population or arm owns the number.

Controlled synthetic fixture (not a real scientific record): whole KRAS cohort n=45; G12C subgroup n=16; G12C median PFS 19.9 months. The answer incorrectly assigns n=45 to G12C. An injected judge explicitly returns `unsupported` with the correct denominator explanation.

**Observed:** final claim status **supported**, `cited_source_supported=True`, **zero defects**.

**Repair:** ground numerical relationships, not a bag of numbers. Associate subject/subgroup, treatment arm, endpoint, value, units, timepoint, denominator, and uncertainty. Treat explicit population/arm/denominator warnings as unresolved until checked; distinguish a judge that is uncertain from one that identifies a concrete scope error. Preserve genuine positive controls for correct extraction.

### F9 — The named Johnson regression fixture is not the real abstract

**Priority: medium, evaluation validity.** `tests/test_evidence_integrity.py:160–170`.

The hand-built `JOHNSON` fixture uses real PMID 39369609 while describing 102 and 164 as the two treatment-arm counts, a docetaxel median OS of 11.3, and HR 1.02. The public abstract instead has sotorasib/docetaxel N=102/58 in 2L and N=164/116 in 2L+, with different comparator outcomes and HRs. A corrigendum is linked [S1].

Hand-built fixtures are appropriate for text-parser unit tests, but this fixture does not demonstrate that the actual Johnson extraction problem was fixed. It should not be presented as a real-paper gold control. This is a test-data problem; it is not evidence that these invented values were returned to users in a live run.

**Repair:** split generic middle-dot/parser fixtures from a verified, versioned primary-record regression fixture. Use obviously synthetic IDs for synthetic text. Retain source URL, retrieval date, exact excerpt/hash, correction status, and separate subgroup/treatment-line denominators for the real benchmark.

### F10 — Exact identifier retrieval is missing from the registered literature tool

**Priority: high.** `phronesis/tools/registry.py:463–470`, `615–630`; `phronesis/lithoscribe/synthesis_engine.py:628–655`, `785–786`.

The synthesis engine supports `pinned_ids` and builds an exact Europe PMC query such as `EXT_ID:39369609 AND SRC:MED`. However, the registered `LithoscribeSynthesisInput` schema and executor do not expose or forward `pinned_ids`. An agent using this tool cannot select that supported retrieval path. It can still construct a structured `search_queries` query, so exact lookup is not impossible through every route.

An unchanged-schema probe supplied `query="PMID 39369609"` and `pinned_ids=["39369609"]`: Pydantic silently discarded `pinned_ids`. The actual decomposition method returned the literal free-text query `PMID 39369609`, with no exact-ID normalization. The latest screenshots show repeated failed searches for all three requested PMIDs; this integration gap is consistent with those failures, but the screenshots do not supply raw HTTP requests that prove the complete live cause.

**Repair:** expose and forward validated PMID/PMCID/DOI identifiers, extract explicit identifiers from the user question into typed retrieval obligations, and preserve those records when their population contradicts the user's assertion. Direct lookup should precede topical searching. A failed lookup means the record was not retrieved; it does not prove that the paper does not exist. Log the normalized request, upstream status, correction handling, exclusion reason, and at most one alternative strategy. Add registry-to-engine integration tests, not only engine helper tests.

## Changes that are present and useful

- Reserve settings and optional/final-phase distinctions are implemented, although enforcement has the gaps above.
- Heartbeats and cancellation watching exist for initial synthesis, rewrites, and verifier revision.
- Partial status includes incomplete reasons and unresolved subquestions.
- The rewrite loop stops on exact unchanged output and records a change ratio.
- Regulatory-intent discovery, label indication extraction, source-authority labels, and route-score descriptions are implemented.
- The population/citation/regimen defect categories and indirect-analysis grading have actual code and tests.
- Memory persistence records turn verification information; this review did not validate every persistence and supersession path.
- The duplicate `phronesis/phronesis/agents/verifier.py` path is a real module in this archive, not a typo in the fix log.

Whole-answer rewriting and cancellation waiting inside some structured model calls remain acknowledged limitations. These changes should not be described as completed section-level patching or uniformly immediate cancellation.

## Latest screenshots: what they actually establish

Both newly attached tall screenshots were inspected in full. They are overlapping views of one answer, with the second showing manually expanded cards; they are not independent benchmark runs.

| Visible observation | Interpretation |
|---|---|
| 6m 9s; 45 steps; 8 issues | Still a long response for a targeted audit; no matched speedup claim is justified |
| Auto · Plan & execute, with critic-debate escalation | This was not a clean explicit Plan-and-execute comparison; unmet evidence obligations triggered more model work |
| Budget panel: 29/100 model calls; estimated 190k/250k tokens; roughly 6m 4s/60m | The displayed run has a generous time budget; displayed ledger also has source-accounting limitations F1–F3 |
| 1 of 8 subquestions answered; 23 tool calls, 9 failed; execution plan 6 error / 2 done | Main problem is failure to collect requested evidence, rather than merely a slow renderer |
| Searches for PMIDs 23313110, 39369609 and 42173791 failed, with repeated attempts | Fix direct identifier retrieval and failure classification before adding more debate/rewrite effort |
| First draft ~1m 12s; two rewrites ~1m 8s total; verification ~31s; critic debate ~38s | Expensive synthesis/revision continued despite missing primary records; some work overlaps/nests, so these numbers must not be summed as independent wall time |
| Rewrite change ratios around 1% then 0% | Rewriting did little to resolve the retrieval failure; exact unchanged stopping exists, but the preceding no-benefit generation still costs time |
| All 10 checked claims described as supported, while 2 citation defects remain | Claim support and correct attached citation are distinct checks. The UI must expose citation defects and avoid implying complete scientific verification |
| Some support rows describe tool/search failures | Operational facts should be grounded in trace events, not assigned scientific citations that merely concern the same topic |
| Important cards initially collapsed | Implemented the focused UI change below |

### Provisional assessment on the requested 100-point rubric

This is a reviewer judgment of the visible latest answer, not a whole-codebase score, validated benchmark, or statistical interval. The underlying sources and exact live tool payloads were not independently replayed. Honest abstention earns uncertainty credit; missing requested results primarily reduces coverage. A disclosed retrieval failure is not itself a fabricated scientific fact.

| Criterion | Assessed points | Basis and limits |
|---|---:|---|
| Factual and quantitative correctness | 14/20 | Avoids filling unavailable paper results with invented numbers; requested numerical results are mostly absent, and regulatory rows were not fully re-audited here |
| Citation entailment and provenance | 6/20 | Two citation defects remain; operational status is mixed with scientific evidence; no full source-to-claim replay |
| Evidence interpretation | 6/12 | Important distinctions are stated, but paper-specific design/population questions remain unresolved |
| Research and clinical scope | 8/12 | Disease/regimen structure is clearer; requested clinical contexts were not all established from primary records |
| Coverage and counter-evidence | 2/10 | Only 1 of 8 subquestions shown as answered; additional critic debate did not recover the missing core evidence |
| Freshness and source authority | 4/8 | Official regulatory material is present, but discovery completeness and every approval claim are not independently certified |
| Uncertainty and abstention | 6/8 | Visible partial status and retrieval limits are useful; blanket support language remains too reassuring |
| Reproducibility and tool honesty | Unassessed /6 | No authenticated live rerun or complete request/response trace in this review |
| Clarity and usefulness | 3/4 | Organized answer; excessive closed cards made inspection harder before this UI patch |
| **Assessed subtotal** | **49/94** | |
| **Possible total with six unassessed points** | **49–55/100** | Do not rescale or treat as a confidence interval |

The requested research task does not meet its completion gate. This score does not establish improvement over the earlier 46–52 assessment because the queries and settings differ. Latency is reported separately from scientific correctness.

## Implemented UI patch: important cards open by default

Root cause: approximately nineteen artifact components explicitly pass `defaultOpen={false}`. The shared Card initialized local state once with `useState(defaultOpen)`, so incoming data/default changes could not open an untouched card. `VerificationNotice` also started closed and suppressed its defect list whenever claims were present.

The patch adds a centralized policy using the question, task outcome and artifact state, without extra model calls or network requests. It uses deterministic keyword heuristics for relevance, not an assertion of perfect intent understanding. Explicit exclusion clauses are handled for common prompts such as “No simulation or single-cell analysis needed.”

| Card/content | New automatic behavior |
|---|---|
| Literature evidence | Open when the card is present |
| Drug/regulatory, trials, variants, targets, graph, structure, omics, single-cell, simulation, hypotheses | Open when the question requests that evidence/analysis; unrelated routine artifacts remain compact |
| Executed sandbox analysis and model calibration | Open when present |
| Failed analyses | Open even without a keyword match |
| Counter-evidence | Open for contradicting passages or heuristic-only classification warnings |
| Claim-support scores | Open for low-support claims or overall support below 0.6; these are support scores, not truth probabilities |
| Agent run / execution plan | Open for partial/unresolved work, failed calls/steps, rejected plan nodes, or exhausted budget |
| Plan review | Open |
| Routine dialogue and project memory | Collapsed |
| Verification findings | Open automatically; unsupported/miscited claims and all defects visible; supported claims remain in a separate compact disclosure |
| Manual choice | Preserved through updates during that mounted message; reload starts with the policy again |

Files changed: `web/src/lib/artifactDisclosure.ts`, `web/src/components/agentic/ui.tsx`, `web/src/components/MessageArtifacts.tsx`, `web/src/components/VerificationNotice.tsx`, `web/src/app/page.tsx`, `web/tests/artifactDisclosure.test.mts`. Backend verification semantics are unchanged by this UI patch.

Validation after the change: **82 web tests pass**, typecheck and production build pass; lint remains **0 errors / 208 existing warnings**. The separate interaction probe mounted actual React components and checked ten behaviors, including new data auto-expansion, retained manual choices, new-message reset, and real artifact integration. Browser installation was unavailable in this environment; responsive visual/accessibility acceptance remains to be checked in the running app. No claim of a completed live deployment is made.

## Start with this shorter diagnostic query

Select **Plan-and-execute explicitly** and **Standard** effort for a new baseline. Keep the same model/settings/cache policy in subsequent comparisons. The latest screenshots used Auto/Ultra; do not describe a Standard rerun as a like-for-like speed improvement. If a plan-review screen appears for this test, the relevant plan is exact paper retrieval plus one bounded fallback, not unrelated simulations, knowledge-graph expansion, or debate.

```text
Retrieve PMID 39369609 by exact identifier. If your tool cannot
do exact lookup, say so and try one primary-source alternative.
Do not treat a failed keyword search as proof the paper is absent.

Keep the answer under 400 words. Report:
1. Exact title, disease population and study design.
2. Whether a correction exists, whether it was retrieved, and
   whether corrected content was actually applied.
3. Separate second-line and second-line-or-later rows showing
   each treatment-arm sample size, each median overall survival
   with its CI, and the mortality HR with its CI.
4. A source URL and supporting source location for every result.

Mark unavailable fields explicitly. Never substitute another
paper, subgroup, endpoint or treatment arm. Use at most one
alternative lookup if the first fails, then return a partial answer.
No FDA sweep, simulation, omics, hypothesis generation or debate
is needed. Do not rewrite the answer without new evidence.

End with unresolved fields. Include duration, calls and tokens
only if real run telemetry supplies them.
```

This query is a positive retrieval/numerical control. It does not test every disease/regimen boundary. Run the broader regression below after the retrieval integration is repaired.

## Broader live diagnostic query

Use **Plan-and-execute explicitly**, search enabled, a fresh conversation/project, and the same model and effort level used for the preceding regression run. Record cache state. If there is no matched previous run, treat the timing as a new baseline. This query intentionally tests separate retrieval contexts; it is not a complete capability benchmark.

```text
Audit KRAS G12C evidence with strict separation of disease,
regimen, treatment line, and subgroup. State the run's UTC date.
Keep the answer under 900 words.

1. FDA indication matrix
Discover the relevant drug names yourself. Create separate rows for:
- Previously treated advanced NSCLC: targeted monotherapy.
- Previously treated colorectal cancer: targeted monotherapy.
- Previously treated colorectal cancer: targeted combinations,
  naming each partner and exact prior-treatment requirements.

Verify every listed approval using an official FDA record and
report its date and URL. Keep each disease/regimen retrieval
obligation distinct even when the search wording is similar.
Distinguish a verified nonapproval from an unavailable or
incomplete lookup. Do not infer variant selectivity from a
generic KRAS-target annotation. Report candidate-discovery limits.

2. Positive numerical control
Retrieve PMID 39369609 and identify any linked correction.
Provide separate rows for second-line and second-line-or-later:
both treatment-arm sample sizes, both median overall survivals,
their confidence intervals, and the mortality HR with its CI.
Show supporting source locations. Do not treat the two sotorasib
cohorts as opposite treatment arms. State whether corrected
content was actually retrieved and applied.

3. Population and subgroup controls
Check whether PMID 23313110 supports a CRC-specific statement
about outcomes with gefitinib/erlotinib. Identify the paper's
actual disease before deciding support.
For PMID 41660470, determine whether n=45 describes all KRAS-mutant
patients or specifically the G12C subgroup. Report a G12C sample
size only if directly supported; otherwise mark it unavailable.
Do not substitute ICI outcomes for KRAS-inhibitor outcomes.

4. Analysis-design control
For PMID 42173791, classify the reported comparison separately
from its database publication tags and contributing trial designs.
Explain whether the comparison itself is directly randomized.

No simulation, single-cell analysis, or hypothesis debate is needed.
Use sources actually retrieved. Keep supported, contradicted,
insufficient, and not checked distinct. A wrong attached citation
must be disclosed even if another record supports the fact.
Use at most one focused alternative retrieval per unresolved item;
then disclose what remains unresolved rather than repeatedly
rewriting the whole answer.

End with unresolved requirements and tools actually used. Report
duration, calls, tokens, and completion status only when real
telemetry provides them; otherwise say unavailable.
```

This tests visible correctness. A chat query cannot establish atomic budget reservations, hard cancellation, or secure source-origin validation; the corresponding code tests are also required.

## Targeted Claude Code repair prompt

```text
Work in the actual Phronesis repository. Read AGENTS.md and preserve
user changes. Fix the confirmed source-level cases below; do not
deploy, push, replace the architecture, weaken verification, or
hard-code benchmark answers. Inspect real symbols/line numbers
before editing. The supplied source-audit report contains controlled
reproductions; reproduce them independently and retain a failing
regression test for each before fixing it.

1. Unify model-call accounting across first draft, agent JSON calls,
   rewrites, verifier revisions, and retries. The current first draft
   in agents/orchestration/draft.py calls the provider without charging
   the run ledger. Include provider usage and estimates distinctly.
   At hard exhaustion, use deterministic partial finalization; do not
   launch an uncharged model call.

2. Replace expected-output-only reservations with per-call admitted
   allocations bound to the provider. In agentic/llm.py the provider
   output cap currently comes from global remaining tokens, not that
   call's reservation. Preserve the final reserve under parallel work.
   Regression A: 10,000 total tokens, three concurrent calls, 1,000
   exact input tokens each, 2,048 reserved output each; controlled
   replies may legally use the supplied provider cap. Current code
   reaches 11,760. Regression B: 3,000 reserved for final work;
   two optional calls currently reach 9,904. Fix admission, binding,
   reconciliation, failure/timeout cancellation handling, and retries
   without inventing actual usage when it is unknown.

3. Remove the 1,024-token floor in rewrite_output_cap when fewer tokens
   fit. With 1,100 left and a 1,000-token input, current code asks for
   1,024 output despite only 100 being available. Pass the exact
   admitted allocation to the model or skip generation with a useful
   partial result. Cover verifier revisions too. Reserve enough for
   final checks rather than allowing repeated synthesis calls to
   consume everything left in the final pool.

4. Enforce absolute run deadlines in streamed stages as well as
   agent JSON calls and tools. RunContext.watched currently watches
   cancellation/heartbeats but not remaining budget time; the stream
   helper has a separate 1,800-second default cap. Bound waits by the
   remaining run deadline and handle terminal state and budget cleanup.
   Test expiry, cancellation, lost clients, cooperative and delayed
   provider cancellation, and absence of new optional work afterward.

5. Fix gap deduplication using structured clinical context before
   lexical similarity. Preserve disease, target/variant, drug,
   combination partner, treatment line, approval jurisdiction, date,
   endpoint, and question obligation. Current Jaccard threshold 0.6
   drops NSCLC versus CRC queries (0.625), monotherapy versus combination
   (0.7), and 2024 versus 2025 (0.778). Keep equivalent-phrasing stop
   behavior. Distinguish successful empty lookup, timeout, off-topic
   results, and an incomplete candidate discovery. Allow a bounded
   alternative strategy, then mark unresolved. Do not retire distinct
   requirements merely because their queries share most words.

6. Use typed canonical source URLs and hostname validation for FDA
   eligibility. verifier.is_regulatory_doc currently accepts
   https://www.fda.gov.example.test/fake and a third-party page whose
   snippet merely mentions an FDA URL. Reuse a consistent authority
   policy across prompts, citations, verifier, and exports. Validate
   actual canonical/final host and redirect provenance; preserve a
   separately retrieved FDA record as its own evidence. Origin alone
   does not establish claim entailment. Add negative lookalike-domain,
   snippet-link, deceptive userinfo, missing URL, and positive genuine
   subdomain controls without making network requests to test hosts.

7. Extend disease/population checks to typed clinical tool records,
   especially CIViC/variant evidence. _population_mismatch currently
   skips all tool: keys. Do not solve this by rejecting all labels or
   registry records; check the individual fact and its contextual
   fields. Preserve a visible mismatch/insufficient state in all
   renderers and memory. Test equivalent data through both article
   and tool-record paths with and without a semantic judge.

8. Fix numerical relationship grounding and semantic reconciliation.
   The core verifier ignores unsupported semantic verdicts when the
   numbers occur somewhere in the record. Synthetic control: full
   KRAS cohort n=45, G12C n=16, G12C median PFS=19.9 months. Wrong
   answer gives G12C n=45; judge explicitly identifies the denominator
   error. Current output is supported, citation supported, zero
   defects. Ground subgroup/arm/endpoint/value/unit/timepoint together.
   Distinguish an inconclusive judge from an explicit relational
   mismatch. Do not break genuine correct-number positive controls.

9. Repair evaluation fixtures. tests/test_evidence_integrity.py uses
   real PMID 39369609 with synthetic Johnson values. Keep generic
   middle-dot parser tests under synthetic identifiers and names.
   Add an independently verified primary-record fixture for the actual
   2L and 2L+ treatment-arm counts/results, with exact source excerpt,
   date/hash, correction status, and provenance. Never use mocked
   scientific results as live gold truth or expected user output.

10. Fix the exact-identifier integration in tools/registry.py.
    LithoscribeSynthesisInput and its registered executor omit
    pinned_ids, despite the engine supporting exact identifiers.
    A schema probe silently dropped pinned_ids and decomposed
    "PMID 39369609" into the unchanged free-text search. Expose and
    forward validated identifiers; normalize explicit user PMIDs,
    PMCIDs and DOIs into retrieval obligations before topical search.
    Preserve a retrieved paper even when it disproves the question's
    population assumption. Test the registered tool through to the
    client request, including successful exact retrieval, invalid IDs,
    upstream timeout, correction linkage and a bounded alternative.
    Do not label failed lookup as proof of nonexistence. Ground tool
    execution-status claims in trace events, separately from papers.

11. Preserve the included selective-card patch. Important evidence,
    low-support/citation warnings, failed plans and partial runs open
    by default; optional diagnostics stay compact. Manual choices
    survive streaming updates. Verify actual rendering and accessible
    keyboard toggles at desktop/mobile sizes. Do not auto-expand every
    artifact or add an LLM call to decide disclosure state.

12. Retain useful changes: typed defects, honest partial status,
    heartbeat distinction, source-authority chips, indirect-design
    grading, and provenance-aware memory. Implement actual targeted
    section repair where justified; prompt-only minimal edits are
    still whole-answer generations. Keep per-answer/evidence versions
    consistent when text or support changes.

Run relevant focused tests, then required project checks. Report exact
commands and pass/fail/blocked outcomes. Web baseline after the supplied UI patch:
82 tests pass, typecheck/build pass, lint 0 errors with 208 warnings.
Ten additional React DOM interaction checks passed in a separate
review harness; these are not browser visual acceptance tests.
The separate review environment did not reproduce the claimed full
backend suite; establish that baseline in the real project environment.

Deliver a concise code-backed audit, reviewed patch, regression cases,
and matched before/after live telemetry when credentials are available.
Report latency and unsupported-claim outcomes separately. Do not claim
the earlier 14-minute run is fixed until a matched live rerun supports it.
```

## Source check

**S1:** [PMID 39369609: original public abstract and linked corrigendum](https://pubmed.ncbi.nlm.nih.gov/39369609/), checked 18 October 2026. This supports the fixture discrepancy finding, not certification of the inaccessible correction's contents.
