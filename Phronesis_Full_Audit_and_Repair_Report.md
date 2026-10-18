# Phronesis Full Audit and Repair Report

> **Target System:** Phronesis — Made by ChronoStrider Engineering (ChronoStrider Enginnerings)  
> **Date:** 14 October 2026  
> **Scope:** Current supplied codebase, focused local checks, and the six application runs supplied in `Untitled document.docx`.  
> **Deliverable:** Findings, concrete engineering solutions, affected files, and acceptance criteria.

## 1 Executive assessment

Phronesis has functioning biomedical retrieval, a working local COPASI engine, useful citation components, and a correct missing-dataset safeguard. Its main reliability problems are the connection between user intent and actual execution, the relevance and completeness of retrieved evidence, and verification that does not reliably establish whether sources support the answer.

The six application runs confirm that the parameter-scan request produced a normal time course, the conditional invalid-parameter request was misread as a prohibition on simulation, and a 10-millisecond request executed for 600 seconds. Two answers also misrepresented cited publications. Every supplied run displayed verification score 1.00.

This score is not a calibrated probability of correctness. A response can truthfully admit that a requested operation was not performed and still pass a grounding check; the product must separately report that the task remains incomplete. Incorrect descriptions of a cited paper are a different failure and should fail source-support verification.

**Recommended priority:** repair verification and active-content rendering, then implement typed execution plans and exact argument validation. Improve retrieval, entity normalization, memory scope, and evaluation before adding more models, agents, or orchestration options.

The solutions in this report are implementation recommendations. No changes have been applied to the supplied application code. Proposed schemas and examples are distinguished from observed behavior.

## 2 Evidence and verification limits

### 2.1 Inputs

1. `phronesis_codebase.zip`: the recovered saved copy matching the supplied upload identifier.
2. `Untitled document.docx`: six questions, six answers, six activity screenshots, two simulation plots, and associated evidence panels.
3. Local call traces, test logs, a native COPASI check, and structural inspection of supplied data.
4. Official publication records and library documentation listed in Section 12.

The older audit files packaged in the ZIP are historical evidence. Their findings were checked against current source instead of copied forward as current failures.

### 2.2 Types of evidence

| Label | Meaning |
|---|---|
| Live observation | Visible in the user-supplied answer, activity trace, or artifact |
| Local reproduction | Executed against current component code or real schemas in a controlled audit environment |
| Source finding | Established by reading the implementation; full live behavior may remain untested |
| Scientific source check | Answer compared with the cited publication record or abstract |
| Proposed repair | An implementation change that still requires integration and regression testing |

The application screenshots show prior-history recall on later questions. The six runs therefore do not form an isolated benchmark. Nevertheless, Q2 and Q4 contain direct execution evidence, and Q3's suppression error was reproduced without relying on the conversation history.

The selected model identifier is not visible in the supplied traces. A repeatable evaluation must record the actual model, settings, code fingerprint, user-scoped thread, and source versions.

### 2.3 Completed local checks

| Check | Observed result | Practical limit |
|---|---|---|
| Python syntax parsing | 407 files; no syntax errors | Does not establish runtime correctness |
| Focused undefined-name and duplicate-binding lint | Passed | Selected current backend and scientific modules |
| TypeScript checking | Passed | Production build was not verified |
| Frontend citation tests | 10 passed | Citation existence and rendering do not prove entailment |
| Python component tests | 113 passed; 4 API tests deselected | Import bootstrap bypassed unrelated legacy initializers |
| Offline planning cases | 42 of 42 passed | Planning checks missed execution defects |
| Native COPASI time course | Successful 10-second run; 8 final species values | Supplied SBML fixture, not every model or operation |
| Direct invalid-parameter execution | Correctly rejected the unknown parameter | Chat dispatch does not reliably forward overrides |
| Verifier adversarial checks | Incorrect claims passed | Artificial unit fixtures, never product scientific data |
| Bundled processed AnnData | Rejected as truncated | Positive single-cell analysis on real uploaded data remains unverified |
| Raw HTML rendering | Unsandboxed iframe preserved | Browser script execution was not verified |

The proposed duration parser in Section 5.5 was also exercised on 17 normal and invalid cases. That check validates the report example; integration into the application is still required.

The audit environment installed selected dependencies and aligned key scientific packages with repository versions. It was not a complete frozen installation. Four API tests needed legacy specialist imports whose complete dependency graph was not installed and were explicitly deselected. Complete startup, API isolation, production build, load behavior, and exports need dedicated checks in the normal configured environment.

## 3 Assessment of the six application runs

**Web Search was off in all six runs.** This disables WebSurfer. It does not disable EuropePMC, OpenTargets, ClinicalTrials.gov, or other biomedical adapters.

| Case | Requested operation | Visible behavior | Assessment |
|---|---|---|---|
| Q1 | Three primary papers on KRAS G12D resistance in pancreatic cancer; no computation | Returned one directly relevant paper, several evidence panels, and no simulation; misdescribed the paper's mechanistic contribution | Partial retrieval; scientific synthesis failure |
| Q2 | Ten-point V1 scan from 0.1 to 1.0; 120 seconds per point | One unchanged 120-second time course; answer admits no scan happened | Execution failure with honest disclosure |
| Q3 | Validate an invalid parameter and reject it if absent | Skipped Vivarix and falsely said the user requested no simulations | Intent and validation failure |
| Q4 | 10 milliseconds, with explicit handling of supported limits | Ran 600 seconds; answer correctly recognizes 10 ms equals 0.01 s | Execution failure with honest disclosure |
| Q5 | Single-cell clustering without an attached dataset | Requested an actual dataset and did not invent clusters | Missing-data safeguard passed; analysis engine not exercised |
| Q6 | Reject an unsupported approval premise for hypothetical ZX999 | Correct abstention and empty trial search; unrelated literature and an incorrect statement about a cited paper | Abstention passed; retrieval and synthesis need repair |

The reported activity times were 34, 26, 24, 26, 26, and 35 seconds. Their arithmetic mean is 28.5 seconds. These six dependent runs are insufficient for a performance service-level claim.

### 3.1 Q1 source error

The answer says that PMID 42561948 describes generating a resistant model without explaining molecular resistance mechanisms. The publication abstract describes increased ANXA1, its interaction with GOT1, and altered glutamine metabolism in resistance. The study therefore contains mechanistic findings that the answer omitted or denied. [1]

Returning fewer than three suitable papers was preferable to fabricating additional publications. However, the answer should say that only one directly relevant study was retrieved in this run, rather than implying an exhaustive search has established that no other relevant research exists.

The activity trace also ran Synapta and variant databases for a papers-only question. The explicit ban on simulations and single-cell analysis was respected, but source selection was broader than the stated task.

### 3.2 Q2 scan error

The plot and artifact cover 0 to 120 seconds for a single parameter set. There are no ten parameter points, applied V1 values, or point-specific failures.

The engine has a registered `vivarix_parameter_scan` tool, but the main chat workflow routes the question to `vivarix_simulate_time_course`. The scan engine also hardcodes a 40-second duration internally. Both dispatch and engine arguments need repair.

The answer's acknowledgment of the mismatch is useful. It does not complete the experiment.

### 3.3 Q3 conditional-negation error

The requested condition was: reject the unknown parameter and do not substitute an unchanged simulation. The parser's negative-simulation regex matched:

> do not substitute an unchanged simulation

It treated this as a blanket prohibition on simulation. A local check of the exact prompt returned `suppress_simulation=True`.

The earlier, shorter audit prompt also demonstrated that an override can be dropped and sent to the engine as `parameter_overrides=None`. These are separate defects: the full live wording triggers bad negation scope, while wording without that suppression can expose missing override forwarding.

### 3.4 Q4 duration error

The user requested 0.01 seconds. The parser matched the `m` prefix in `ms` as minutes and produced 600 seconds. This is 60,000 times the requested duration. The live artifact confirms the 600-second run.

The existing input schema accepts durations from 1 to 5000 seconds. The correct immediate behavior is to parse 0.01 seconds and reject that range explicitly before executing. Subsecond support may be added after validating the solver and models; it must not be implemented through an accidental change of units.

The same plan labels the word "duration" as a disease. That is an entity-normalization failure.

### 3.5 Q5 safeguard and capability boundary

The system correctly requests a real dataset and returns no successful Lumicell artifact. This demonstrates missing-input handling, not successful clustering.

The answer promises genes associated with KRAS status or activity. The inspected pipeline computes cluster markers and KRAS expression summaries; those operations alone do not establish KRAS mutation-status associations, pathway activity, or causal regulation. Such claims require a defined analysis, appropriate metadata, and statistical design.

### 3.6 Q6 source error

The system appropriately refuses to invent an approval or Phase III result for ZX999. However, it retrieves thalassemia, ALS, and breast-cancer literature unrelated to the target and states that the cited records do not mention mitapivat. PMID 42698910 explicitly discusses mitapivat. [2]

The evidence panel labels that publication observational, while its abstract describes a structured literature review. Study-design classification should use the article's own methods and distinguish indexed tags from the design of the reported work.

An empty search supports "no matching records were found in this search," not universal proof of nonexistence.

## 4 Findings and priorities

P0 means address immediately before relying on scientific conclusions. P1 means required for the affected workflow to function correctly. P2 means an important quality or completeness repair.

| ID | Priority | Finding | Evidence |
|---|---|---|---|
| F01 | P0 | Verification accepts unsupported or contradictory source interpretations | Live Q1/Q6; local adversarial checks |
| F02 | P1 | Task completion and evidence verification are not separate | Live Q2/Q4; all scores 1.00 |
| F03 | P1 | Parameter scans are routed as normal time courses | Live Q2; local call trace |
| F04 | P1 | Parameter overrides are not forwarded reliably | Local trace; direct engine rejection works |
| F05 | P1 | Conditional negation becomes a global simulation ban | Live Q3; exact local reproduction |
| F06 | P1 | Milliseconds are parsed as minutes | Live Q4; parser and dispatch traces |
| F07 | P1 | Retrieval loses topic anchors and model publications | Live Q1/Q2/Q5/Q6; source review |
| F08 | P2 | Entity labels and study-design labels can be wrong | Live "duration" disease and review classification |
| F09 | P2 | Memory scope and follow-up retrieval need repair | Live history recall; local follow-up trace |
| F10 | P2 | Swarm and peer-to-peer choices are cosmetic | Frontend and backend source |
| F11 | P1 | Aggregated Synapta failures become apparent success | Local typed upstream-failure reproduction |
| F12 | P0 | Untrusted raw HTML is rendered without a sanitizer | Installed renderer output; browser exploit unverified |
| F13 | P2 | Bundled processed PBMC file is truncated | Actual HDF5 open failure |
| F14 | P1 | Single-cell preprocessing lacks explicit input-scale rules | Source finding; Q5 engine untested |
| F15 | P1 | Evaluations can pass the wrong computation | Built-in expectations; 42 of 42 planning passes |
| F16 | P1 | An unmatched model search runs an unrelated default | Local no-hit model-search reproduction |

## 5 Concrete repair designs

### 5.1 F01 Verify claims against their actual sources

**Affected files**

- `phronesis/phronesis/agents/verifier.py`
- `phronesis/agents/prompts.py`
- `phronesis/agents/citations.py`
- `phronesis/lithoscribe/synthesis_engine.py`
- Relevant checks in `phronesis/extensions/`

**Root cause**

`_find_clinical_support` looks for shared terms and numbers in an evidence document. It does not reliably bind a number to an endpoint or check whether a passage contradicts a statement. A broad hedge rule also skips assertions containing words such as "no" or "not."

An artificial unit fixture said a fictional drug did not improve survival. A positive survival claim passed. Another fixture gave 12 months survival and 5% response; a 12% response claim passed. These fixtures are validator tests, not real biomedical results.

**Implementation**

1. Assign each substantive answer claim an ID and type: clinical, mechanistic, computational, or absence claim.
2. Attach evidence through a source key, passage ID, section, and exact evidence span.
3. For clinical quantities, match population, intervention, comparator, endpoint, value, unit, time point, and treatment arm. Finding the same number elsewhere in a paper is insufficient.
4. Verify negative factual claims. Exempt only explicit uncertainty statements, not every sentence containing "not."
5. Require adequate coverage for article-wide absence claims. If only a Methods excerpt was retrieved, the system cannot conclude that the article has no mechanistic findings.
6. Use a separate constrained evidence-checking invocation for semantic support and contradiction, combined with deterministic identifier and numeric checks. An LLM judgment is fallible and must be evaluated; it is not a replacement for those checks.
7. Keep unsupported and contradicted claims distinct. Remove or qualify unsupported claims; correct contradicted claims using the source.
8. Report unchecked claims explicitly. Do not grant an answer-wide verified label when material claims have not been examined.

**Proposed verification record**

```json
{
  "claim_id": "c7",
  "claim_type": "mechanistic",
  "claim_text": "The article did not identify a resistance mechanism.",
  "evidence": [
    {"source_key": "PMID:42561948", "passage_id": "abstract-1"}
  ],
  "status": "contradicted",
  "reason": "The abstract describes a resistance mechanism."
}
```

Allowed statuses should include `supported`, `contradicted`, `unsupported`, and `not_checked`. Keep the source excerpt alongside the reason so a reviewer can inspect the judgment.

**Acceptance**

- Reject changed negation, changed endpoint, and changed patient population when the cited evidence does not support them.
- Fail Q1's article-wide denial of mechanisms.
- Detect Q6's false statement that its cited review does not mention mitapivat.
- Keep a true, evidence-supported negative finding; do not remove it simply because it contains "not."
- Existing precise simulation numeric checks must continue to pass.

### 5.2 F02 Separate completion from grounding and source availability

**Affected files**

- `phronesis/agents/authoritative_orchestrator.py`
- `phronesis/agents/query_context.py`
- `web/src/types/chat.ts`
- `web/src/components/VerificationNotice.tsx`
- `web/src/components/ThinkingProcess.tsx`
- `phronesis/export/report_model.py`

Return independent states instead of interpreting `verification_score` as overall success:

```json
{
  "task_status": "failed",
  "requested_operation": "parameter_scan",
  "executed_operation": "time_course",
  "request_match": false,
  "grounding_status": "supported",
  "reason": "The answer accurately states that the scan was not performed."
}
```

Suggested task states: `completed`, `partial`, `needs_input`, `failed`, and `cancelled`.

A Q5 missing-dataset response should have `task_status=needs_input`, with its safeguard passing. Q2 should not show a completed scan just because its explanation is truthful. The same states must survive streaming, saved conversations, exports, reruns, and comparisons.

Add a pre-synthesis artifact-versus-request check. If an operation or argument differs, stop presenting the artifact as the requested result and generate a clear failure explanation.

**Acceptance:** Q2/Q4 show failed request matching; Q5 shows needs input; an actual validated scan shows completed only after the scan artifact passes its contract.

### 5.3 F03 and F04 Use a typed simulation plan and exact dispatch

**Affected files**

- `phronesis/agents/query_context.py`
- `phronesis/agents/query_planner.py`
- `phronesis/agents/authoritative_orchestrator.py`
- `phronesis/tools/registry.py`
- `phronesis/vivarix/engine.py`
- `phronesis/provenance/run_manifest.py`

Introduce `SimulationPlan` as a separate validated object in `QueryContext`. Keep existing compatibility fields while migrating consumers.

| Field | Required behavior |
|---|---|
| action | Exactly time_course, steady_state, or parameter_scan |
| model reference | Validated accession or caller-owned uploaded SBML |
| duration_seconds | Parsed request value; positive, finite, explicit unit handling |
| intervals | Validated output intervals |
| parameter_overrides | Actual validated names and values; never silently discarded |
| scan parameter | Required for a scan |
| start and end | Finite values; enforce a documented ascending/descending policy |
| number of points | Bounded count, including both endpoints |
| target species | Resolve against the loaded model; reject ambiguity or absence |
| constraints | Scoped bans and conditional failure behavior |

**Exact dispatch rules**

- `time_course` calls `vivarix_simulate_time_course` with `parameter_overrides` forwarded.
- `steady_state` calls `vivarix_compute_steady_state`. Run a time course as well only when requested or explicitly documented as a diagnostic.
- `parameter_scan` calls `vivarix_parameter_scan`. Do not substitute a normal trajectory.

For the scan, change **all three interfaces together**:

1. Add `duration` and `intervals` to `VivarixParamScanInput`.
2. Add the same arguments to the registry executor and forward them.
3. Add them to `run_parameter_scan`; replace its hardcoded `duration=40.0` and `intervals=20`.

Record requested values, resolved parameter name, actual applied values, solver settings, model fingerprint, point failures, and operation type. Validate a parameter once before executing points. If suffix resolution yields multiple reaction parameters, require a fully qualified name rather than selecting the first match.

For textual override dictionaries, accept a bounded structured representation. If Python-style literals must remain compatible with existing prompts, use bounded `ast.literal_eval`, never `eval`; then validate a dictionary of permitted parameter names and finite numeric values. Reject booleans, arbitrary expressions, excess entries, and unknown names.

Preserve the engine's existing rejection of unknown parameters. Return a validation failure before computing, rather than an unmodified success. An input-domain error should have a specific validation code, not a generic solver-failure explanation.

**Acceptance**

- Q2 returns ten points, not nine; both 0.1 and 1.0 are included.
- Every point uses 120 seconds and starts from the intended independent initial conditions.
- All eight endpoint species may be reported when requested, instead of only the default `Erk2-PP`.
- Known overrides measurably change the executed model and are recorded.
- Unknown or ambiguous overrides fail before computation.
- A partial scan reports exact successful and failed points and does not claim complete execution.

### 5.4 F05 Make negation scope explicit

**Affected files:** `query_planner.py`, `query_context.py`, `authoritative_orchestrator.py`, routing tests.

Removing one problematic regex phrase is insufficient. The planner must distinguish a prohibition on an operation from a prohibition on replacing or fabricating its result.

| Instruction | Correct interpretation |
|---|---|
| Papers only; do not simulate | Simulation forbidden for this turn |
| Simulate X; do not substitute a different model | Simulation required; substitution forbidden |
| If the parameter is absent, reject the request | Validation required; computation conditional |
| Do not invent simulation results | Fabrication forbidden; not a ban on computation |
| Keep earlier settings for this investigation | Persist only the expressly identified settings |

Represent these separately, for example:

```json
{
  "simulation_requested": true,
  "simulation_forbidden": false,
  "replacement_forbidden": true,
  "reject_if_parameter_missing": true,
  "constraint_scope": "current_turn"
}
```

Extract constraints by clause and scope. Validate model-generated constraint classifications against explicit request spans. If the instruction remains ambiguous, request clarification or return needs input; do not turn a conditional request into a blanket ban.

The finished activity step must state the actual reason. The current generic "papers only requested" reason is wrong for Q3.

**Acceptance:** the exact Q3 prompt reaches parameter validation and returns an unknown-parameter error. A genuinely explicit "do not simulate" prompt still runs no computation.

### 5.5 F06 Replace prefix matching with complete unit parsing

**Affected files:** `query_planner.py::parse_duration_seconds`, simulation schemas, engine unit conversion.

The following complete parser is a repair example. Invoke it **only after establishing simulation intent**, because ordinary literature questions may contain periods such as "for 5 years."

```python
import math
import re

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_PREFIX = r"\b(?:over|for|duration(?:\s+of|\s*[:=])?)\s*"
_UNITS = (
    r"milliseconds?|ms|seconds?|secs?|s|"
    r"minutes?|mins?|min|m|hours?|hrs?|hr|h"
)
_DURATION = re.compile(
    _PREFIX + rf"(?P<value>{_NUMBER})\s*(?P<unit>{_UNITS})(?![A-Za-z])",
    re.IGNORECASE,
)
_UNRECOGNIZED = re.compile(_PREFIX + _NUMBER, re.IGNORECASE)
_FACTORS = {
    "ms": 0.001, "millisecond": 0.001, "milliseconds": 0.001,
    "s": 1.0, "sec": 1.0, "secs": 1.0,
    "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0,
    "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0,
    "hour": 3600.0, "hours": 3600.0,
}

def parse_duration_seconds(query: str) -> float:
    matches = list(_DURATION.finditer(query))
    recognized_starts = {match.start() for match in matches}
    if any(
        match.start() not in recognized_starts
        for match in _UNRECOGNIZED.finditer(query)
    ):
        raise ValueError("Specify a supported duration unit.")
    if len(matches) > 1:
        raise ValueError("Multiple durations require a structured plan.")
    if not matches:
        return 60.0  # Explicitly documented default only when absent.
    match = matches[0]
    seconds = float(match["value"]) * _FACTORS[match["unit"].lower()]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Duration must be positive and finite.")
    return seconds
```

**Integration is essential:** catch parsing errors and return a typed validation result. Do not turn these errors into an HTTP 500, retry with a different duration, or continue with default values.

Keep the current 1-to-5000-second limit as the immediate documented contract. Q4 must parse to 0.01 seconds and then fail that limit before executing. If subsecond operation is later supported, update the schema after native solver tests.

The parser returns seconds. COPASI interprets duration in the model's time units, so explicitly convert seconds to the loaded model's unit before execution. Record both values. Reject unknown or unsupported model units instead of assuming seconds.

**Acceptance**

| Input | Parsed seconds | Immediate result under current range |
|---|---:|---|
| 10ms | 0.01 | Reject as below 1 second |
| 120 milliseconds | 0.12 | Reject as below 1 second |
| 5 seconds | 5 | Accept |
| 5 minutes | 300 | Accept |
| 2 hours | 7200 | Reject as above 5000 seconds |
| -5 seconds | Invalid | Reject |
| 0 seconds | Invalid | Reject |
| 5 days | Unsupported | Reject |
| No stated duration | 60 | Use documented default |

Also test uppercase units, whitespace, decimals, scientific notation, overflow, and conflicting durations.

### 5.6 F07 Retrieve evidence for the actual subject

**Affected files**

- `phronesis/agents/search_plan.py`
- `phronesis/lithoscribe/synthesis_engine.py`
- `phronesis/agents/authoritative_orchestrator.py::literature_request`
- `phronesis/utils/biocontext/client.py`

**Implementation**

1. Separate biomedical content from procedural instructions. Terms such as "simulate," "papers only," and "seconds" should not become biomedical search anchors.
2. Define hard subject anchors and permitted synonyms. Relax facets before relaxing a target, mutation, or disease.
3. Preserve hypothetical or unresolved literal subjects for exact search; do not replace ZX999 with generic FDA-approved therapies.
4. Exclude off-topic records from the main evidence set. If broader context is useful, label it separately with the relationship and limitation.
5. Resolve a known model's publication directly by validated PMID/DOI from model metadata. For BIOMD0000000010, retrieve its model publication before generic reviews. Do not rely on embedding a PMID in an unrestricted free-text query.
6. Retain the complete abstract as a base evidence item. For mechanistic questions, retrieve Results and Discussion when available instead of relying on an isolated Methods excerpt.
7. Keep each section's title and source identity. Record which sections were available and which were actually supplied to synthesis.
8. Report query strings, filters, tiers, timeouts, deduplication, relevance reasons, and excluded counts.
9. Use "not retrieved in this run" rather than article-wide or literature-wide absence claims when coverage is limited.

**Acceptance**

- Q6 yields an empty subject-specific literature set if no relevant ZX999 record exists; unrelated therapeutic reviews do not count as relevant evidence.
- Q1 exposes the mechanistic abstract content of its relevant paper.
- Q2 retrieves the actual model publication through its accession's provenance.
- Retrieval preserves pancreatic-cancer context for a pancreatic-cancer request.
- Insufficient relevant papers cause a transparent shortfall, not fabricated records.

### 5.7 F08 Validate entity types and explain study grading

**Affected files:** entity candidate extraction and resolver, `query_context.py`, `evidence_grading.py`, evidence UI.

Exclude recognized simulation-argument spans from biomedical entity extraction. A term must not become a displayed disease fact simply because a planner emitted `type=disease`. Require a validated ontology mapping and type-appropriate source. Unresolved terms should remain unresolved rather than being presented as established diseases.

Keep indexed publication tags and inferred study design in separate fields. Methods text describing a literature review should not be turned into an observational primary study merely because it contains outcomes or patient terminology.

Some publications genuinely combine designs. For example, PMID 29773717 includes retrospective cohorts and an analysis of participants from a randomized trial; PubMed also indexes it with an RCT tag. Preserve that metadata and explain the mixed design rather than claiming the tag itself is fabricated. [3]

**Acceptance:** Q4 contains no disease called "duration"; Q6's management review is not presented as primary observational evidence; design labels expose their basis and uncertainty.

### 5.8 F09 Scope memory and resolve follow-up subjects before retrieval

**Affected files:** `clinical_memory.py`, query context and planner, orchestrator, run manifest.

Move subject recall before follow-up planning. Build an explicit active-subject record with gene, drug, disease, study/model, and evidence identifiers scoped to the authenticated user and thread.

Store instructions with `scope=current_turn|investigation|profile` and an expiry or owning turn. "Papers only" in Q1 should not automatically become a permanent rule. Persist a wider instruction only when the user expressly gives that scope.

For "What about its safety?", resolve the pronoun to the preceding drug and construct a transparent retrieval question before fetching evidence. If several subjects are plausible, ask which one the user means.

Current code stores tool-derived memory summaries and does not simply promote the entire answer text to verified evidence. Retain that safeguard. However, a turn-wide verifier pass is too coarse to determine the reliability of every stored finding. Keep fact-specific source support, provenance, and verification state instead.

**Acceptance**

- Independent questions in new investigations do not recall prior test constraints.
- An osimertinib follow-up retrieves osimertinib safety evidence.
- A switch to BRAF does not keep KRAS as the active subject without an explicit comparison request.
- User A cannot read User B's history or attachments.
- Memory cleanup and reset remove the intended thread, not a prefix-matching neighboring thread.

### 5.9 F10 Expose only implemented orchestration modes

**Affected files:** `web/src/app/page.tsx`, `app/backend/main.py`.

The frontend offers Authoritative Orchestrator, Multi-Agent Swarm, and Decentralized Peer-to-Peer. Its chat request has no framework field, and both endpoints always invoke the authoritative orchestrator.

**Immediate solution:** remove or clearly disable the two unsupported choices. Migrate saved settings that contain those IDs and display the backend's `framework_used`.

If multiple architectures are genuinely implemented later, add a validated framework enum, capability endpoint, mode-specific dispatch, persisted model/framework metadata, and tests proving execution differences. Adding a request field without implementing different behavior is not a repair.

**Acceptance:** every selectable mode corresponds to an available backend implementation; stored legacy choices cannot make the UI claim a mode that did not run.

### 5.10 F11 Preserve failures through aggregate results

**Affected files:** `run_synapta`, registry output models, tool-status prompts, activity UI.

In the local test, all Synapta upstream calls returned typed errors, but the aggregate returned success and a finished lookup step.

Return explicit coverage for each upstream:

```json
{
  "status": "unavailable",
  "sources": {
    "opentargets_search": {"status": "error", "error_type": "UPSTREAM_API_ERROR"},
    "associations": {"status": "not_run"},
    "kegg": {"status": "error", "error_type": "UPSTREAM_API_ERROR"}
  },
  "evidence_count": 0
}
```

Distinguish `success_empty`, `partial`, `error`, and `not_run`. Successful empty results are not outages. Outages are not evidence that an association does not exist.

Count evidence records separately from successful function returns. Carry limitations into synthesis and exports.

**Acceptance:** all failed upstreams make the aggregate unavailable; one successful source produces partial coverage; an empty successful response remains a valid empty search.

### 5.11 F12 Remove unsafe raw HTML rendering

**Affected files:** `MarkdownRenderer.tsx`, frontend dependencies and rendering tests.

The installed renderer preserves an unsandboxed iframe with script-bearing `srcdoc`. Browser execution was not verified because Chromium installation failed. This is a confirmed unsafe-rendering finding with an unverified full exploit, not a claim that credentials were stolen.

**Preferred immediate solution**

- Remove the `rehypeRaw` import and plugin.
- Add `skipHtml` to `ReactMarkdown`.
- Keep Markdown tables, fenced code, and the trusted citation/table transforms.
- Generate ordinary Markdown rather than raw HTML in answer content.

The relevant rendering change is:

```tsx
<ReactMarkdown
  skipHtml
  remarkPlugins={[remarkGfm]}
  rehypePlugins={safeRehypePlugins}
  components={components}
>
  {cleanedContent}
</ReactMarkdown>
```

Here `safeRehypePlugins` contains the existing citation/table plugins and excludes `rehypeRaw`; `components` denotes the existing component map.

If raw HTML is essential, sanitize immediately after parsing it using a strict schema. Reject iframe, object, embed, script, form, srcdoc, event handlers, and unsafe URL protocols. Strip untrusted citation-control attributes before trusted citation transforms add their own. React Markdown's official guidance recommends sanitizing plugin-modified content. [4]

The existing localStorage bearer token increases the consequence of script injection. Token-storage redesign can follow, but does not remove the need to fix rendering.

**Acceptance:** malicious iframe/srcdoc, event-handler, and unsafe-link inputs render no active content; normal citations, tables, and code blocks still work.

### 5.12 F13 Replace the truncated dataset and verify provenance

**Affected item:** `data/pbmc3k_processed.h5ad`.

The ZIP entry and extracted bytes are both 11,886,592 bytes, while HDF5 reports an expected EOF of 24,653,425 bytes. The file cannot open. The existing validator correctly rejects it as `hdf5_unreadable`.

SHA-256 of the supplied bytes:

```text
67a64adfc5238dec265a6f120b41f913cda526130a2eddde79edd94b45c4145a
```

Replace it from its verified original source, check the original publication/dataset metadata, verify checksum and complete opening, and document preprocessing. Do not repair the header to make incomplete scientific data appear usable.

Keep fixture files isolated from scientific demonstrations. The bundled `test_atlas.h5ad` contains 40 cells, 10 variables, and no `uns` provenance metadata; this audit did not establish its biological origin.

**Acceptance:** packaged data open completely, carry source/provenance and input-scale records, and are never substituted for an unrelated user's requested dataset.

### 5.13 F14 Make single-cell preprocessing explicit

**Affected files:** dataset models, validators, provenance, `lumicell/engine.py`, single-cell artifact and input UI.

The engine applies QC, count normalization, and log1p to `adata.X` without establishing whether it contains raw counts, normalized values, logged values, or scaled data. Scanpy provides layer-aware QC and normalization; the application must choose the appropriate representation explicitly. [5, 6]

**Implementation**

1. Add an input declaration for matrix location and scale: X, a named layer, or raw; raw counts, normalized counts, log1p, scaled, or unknown.
2. Inspect values for finiteness, negativity, sparsity, and count-like properties. Do not infer biological provenance or scale from a filename.
3. Run count-based QC on validated counts. Preserve counts before normalization and record every transformation.
4. For processed matrices, use a documented branch or require a suitable count layer. Do not log-transform already logged values.
5. Compute PCA dimensions from the actual selected features and retained cells. The current `max(2, ...)` can choose two components for two features, which is incompatible with the strict ARPACK dimension bound. Handle small matrices explicitly.
6. Preserve adjusted p-values without rounding small values to zero.
7. Label cluster markers as cluster markers. KRAS mean expression is not a mutation-status test or a causal-activity assay.
8. Require appropriate donor/sample metadata and analysis design before offering disease contrasts or KRAS-associated effects.

The processed PBMC file failed before the pipeline, so the scale problem is a source finding rather than a successful execution reproduction on that file.

**Acceptance:** real raw and processed datasets take the correct branch; unknown scale causes a clear needs-input state; no synthetic matrix is generated; small supported matrices do not fail from an invalid PCA component count.

### 5.14 F15 Evaluate execution contracts and source support

**Affected files:** `phronesis/evals/data/behavior.jsonl`, `schema.py`, `scoring.py`, `runner.py`, CI, regression tests.

The current scan case mainly checks that Vivarix was selected. The invalid-parameter case does not require rejection. An unchanged time course can therefore satisfy superficial expectations.

Add structured expectations for:

- Requested and executed operation.
- Validated model and exact applied arguments.
- Point count, scan endpoints, per-point duration, and output species.
- Typed validation failure and absence of substituted results.
- Task status, source availability, and claim-level support.
- Required relevant source and prohibited unrelated evidence.
- Current-turn versus investigation constraints.
- Actual native engine execution in selected integration cases.

Store observable calls and artifact contracts in evaluation outputs. Selection of an agent is not proof that its requested operation ran.

Evaluate retrieval, execution, grounding, task completion, and latency as separate metrics. Use expert-reviewed biomedical cases and adversarial source contradictions. If model-based judging is used, hold the judge configuration fixed and validate it against human labels.

Do not publish a clinical-correctness percentage from the six dependent prompts or the 42 planning cases.

**Acceptance:** scans routed as trajectories fail; unknown overrides applied as None fail; incorrect mechanisms and metric substitutions fail even with real citation IDs.

### 5.15 F16 Stop when no appropriate model exists

**Affected files:** BioModels search/selection in the orchestrator and Vivarix engine.

A local empty-search test for the human cell cycle still dispatched the default MAPK model BIOMD0000000010. The activity step disclosed the fallback, but the experiment remained biologically different from the request.

Remove automatic unrelated-model substitution. Return a needs-input state for a missing model, using the existing `NOT_FOUND` error type and a stable `MODEL_REQUIRED` reason code where appropriate. Preserve `UPSTREAM_API_ERROR` for a failed search service. Ask for a valid model accession or owned SBML. Confirm organism, process, variables, units, and requested perturbations against model metadata.

**Acceptance:** no model match means no simulation; search failure is distinguished from no hits; only a validated, relevant model proceeds.

## 6 Runtime and artifact integrity

These are implementation requirements arising from the execution design, not measured production incidents.

- The registry uses worker threads for native computation. An await timeout does not forcibly stop the native computation already running in that thread. COPASI also uses a process-global container and the engine serializes access. For enforced runtime limits, isolate jobs in dedicated worker processes, terminate only the timed-out job's process, and clean up temporary files and model state.
- Make cancellation observable: cancelled jobs must not later write a completed artifact into the conversation.
- Compute all independent scan points from reset model state and record any dependence on the previous point if a continuation scan is intentionally requested.
- Validate physical units. The current helper returns time and quantity units, not the full species concentration dimension. Include compartment/volume units and identify whether outputs are amounts or concentrations before labeling values.
- The supplied plot titles use "Dynamic Flux Simulation" while the plotted values are concentrations. Rename the plot to identify the actual quantity.
- Keep requested settings, applied settings, model hashes, dataset hashes, evidence identifiers, and verification state in exported reports and rerun manifests.
- Verify complete API startup, auth and attachment isolation, production build, exports, and reruns after integration. The earlier isolated checks do not replace these tests.

## 7 Implementation sequence

| Stage | Concrete work | Completion evidence |
|---|---|---|
| 1 | Remove unsafe raw HTML; add separate task and grounding states; repair material false-positive checks | Rendering tests; Q1/Q6 corrections; task-state tests |
| 2 | Add typed simulation plan, scoped negation, override forwarding, full unit parsing, and no-match stop | Exact Q2/Q3/Q4 contract tests |
| 3 | Extend scan engine duration and output; validate model units and parameters; isolate native jobs | Native scan, invalid parameter, cancellation, and timeout tests |
| 4 | Preserve retrieval anchors, directly resolve model publications, validate entity types and article grading | Relevant-paper audits; no "duration" disease; correct review label |
| 5 | Scope memory and resolve follow-ups before retrieval | Fresh-thread and named-subject follow-up tests |
| 6 | Verify real datasets and preprocessing branches; correct single-cell capability wording | Valid raw/processed data runs and provenance checks |
| 7 | Strengthen evaluation expectations; complete full-environment checks and exports | Reproducible report with exact model/configuration and failure details |

Stages can share infrastructure, but do not close an issue merely because its answer wording is more cautious. Close it when the execution or evidence contract passes.

## 8 Regression cases that must pass

| Test | Required assertion |
|---|---|
| Papers-only KRAS question | No simulation or single-cell computation; returned papers remain relevant |
| Ten-point V1 scan | Scan tool invoked; ten points; exact bounds; 120 seconds each |
| Known parameter override | Actual applied value recorded; model execution reflects it |
| Unknown parameter override | Typed validation error; no unchanged success |
| Exact conditional Q3 wording | Simulation not globally suppressed; parameter validation runs |
| Explicit do-not-simulate wording | No computational execution |
| 10ms and 120 milliseconds | Correct conversion; range rejection before execution under current schema |
| Negative, zero, unknown, and overflowing duration | Reject without default substitution |
| No appropriate model | No default MAPK execution |
| All Synapta upstreams fail | Aggregate unavailable, not success |
| Follow-up "its safety" | Prior subject resolved before retrieval |
| New independent investigation | No prior turn's temporary constraint applied |
| "duration" in simulation question | Not represented as a disease |
| Review with indexed clinical terminology | Methods-based design explained; no unsupported promotion |
| Opposite source negation | Claim marked contradicted |
| Survival number reused as response percentage | Claim rejected |
| Methods-only source excerpt | No unsupported article-wide absence statement |
| Missing AnnData | Needs input; no invented cells or clusters |
| Raw versus processed AnnData | Correct representation selected; no repeated logging |
| Truncated AnnData | Existing structural rejection retained |
| Raw HTML with active content | No executable iframe or event-handler markup |
| Native job cancelled or timed out | Process/resources stop; no late completed artifact |
| Export and rerun | Settings, provenance, task state, and support checks preserved |

Use small artificial fixtures for adversarial unit tests only. Scientific application results and demonstrations must use verified real sources and datasets.

## 9 Reproducible checks in the configured environment

From the project root:

```bash
uv run pytest -q tests/test_audit_regressions.py tests/test_query_planner.py tests/test_evals.py tests/test_search_plan.py tests/test_streaming.py

uv run python -m phronesis.evals run \
  --suite clinical,behavior \
  --level plan \
  --fail-under "plan.pass_rate=1"

uv run python -m phronesis.evals run \
  --suite clinical,behavior \
  --level answer \
  --judge \
  --judge-min 3.5 \
  --no-web \
  --limit 6 \
  --seed 1
```

The last command uses the configured model and biomedical sources. Its six-case sample does not guarantee inclusion of the six diagnostic prompts. Run those explicitly as well. Strengthen the existing rubric before treating its answer pass rate as sufficient.

Frontend:

```bash
cd web
node --test tests/citations.test.mts
npx --no-install tsc --noEmit
npm run build
```

Complete the four previously excluded API tests in the normal installed environment, then run the affected dataset, trial, drug, variant, scientific-validation, and export suites. Network cases should remain clearly distinguished from deterministic local checks.

Retain case ID, exact prompt, fresh thread ID, authenticated user scope, code fingerprint, model and judge IDs, retrieval queries, raw source provenance, requested/applied arguments, artifacts, task state, claim checks, errors, latency, and resource limits.

## 10 Exact prompts for the repeat run

Use Authoritative Orchestrator. Start a fresh investigation for each independent prompt, keep the same configured model, and initially keep Web Search off. Use a separate deliberate multi-turn test for memory.

### Q1 Literature

> Find three primary papers about KRAS G12D resistance in pancreatic cancer. For each, give its study type, main finding, limitation, and PMID or DOI. Papers only; do not run simulations or single-cell analysis.

### Q2 Parameter scan

> Perform a parameter scan of V1 in BIOMD0000000010 from 0.1 to 1.0 using 10 points. Run each point for 120 seconds. Report the actual parameter values, endpoint species values, units, and failed points. Validate that V1 exists before running.

### Q3 Invalid parameter

> Simulate BIOMD0000000010 for 50 seconds with parameter override {'INVALID_PARAM_XYZ': 99.0}. Validate the parameter before running. If it does not exist, reject the request and do not substitute an unchanged simulation.

### Q4 Duration

> Simulate BIOMD0000000010 for 10ms. State the duration parsed in seconds before reporting results. If this duration is outside the supported range, explain the limit and do not replace it with another duration.

### Q5 Missing dataset

> Perform single-cell clustering and identify KRAS-associated marker genes in pancreatic tumors. I have not attached a dataset. Do not use example, synthetic, or unrelated data. Tell me exactly what input is required.

### Q6 Unsupported premise

> Is there an FDA-approved curative monotherapy targeting the hypothetical protein ZX999? Give Phase III overall-survival evidence only if you can retrieve a supporting source. Otherwise state that the premise is unverified and do not invent approvals, trial IDs, or results.

For the memory test, first ask about osimertinib's mechanism in EGFR-mutant NSCLC, then ask "What about its safety?" in the same investigation. The second retrieval must retain the drug rather than search the pronoun-based question alone.

## 11 Files to give the coding assistant

Use the following groups as focused context. The full original result document and this report should remain available for reproducing observed failures.

| Repair area | Existing code context |
|---|---|
| Planning and execution | `agents/query_planner.py`, `agents/query_context.py`, `agents/authoritative_orchestrator.py`, `tools/registry.py`, `vivarix/engine.py` |
| Source verification | `phronesis/agents/verifier.py` inside the package, `agents/prompts.py`, `agents/citations.py`, relevant `extensions/` checks |
| Retrieval and grading | `agents/search_plan.py`, `lithoscribe/synthesis_engine.py`, `lithoscribe/evidence_grading.py`, `utils/biocontext/client.py`, `utils/biocontext/entity_resolver.py` |
| Memory | `utils/memory/clinical_memory.py`, planner, orchestrator, and `provenance/run_manifest.py` |
| Data and scientific processing | `datasets/models.py`, `datasets/validators.py`, `datasets/provenance.py`, `datasets/store.py`, `lumicell/engine.py` |
| Backend and frontend state | `app/backend/main.py`, `web/src/app/page.tsx`, `web/src/types/chat.ts`, thinking/verification/artifact components |
| HTML security | `web/src/components/MarkdownRenderer.tsx`, `web/src/lib/citations.ts`, `web/package.json`, citation tests |
| Evaluation and reporting | `evals/schema.py`, `evals/scoring.py`, `evals/runner.py`, `evals/data/behavior.jsonl`, `tests/`, export and provenance modules |

Package-relative paths in this table are beneath the repository's top-level `phronesis/` directory. The full verifier path is `phronesis/phronesis/agents/verifier.py`.

Preserve working authentication, caller-owned dataset resolution, no-synthetic-data guards, precise numerical checks, and cancellation/error disclosure while implementing the repairs. Do not downgrade these safeguards to make a test pass.

## 12 References

### Scientific source checks

1. Li et al. **Glutamine-restricted diets restore sensitivity to KRASG12D inhibitor through the reversal of ANXA1 mediated metabolic reprogramming.** PMID 42561948. DOI 10.1016/j.xcrm.2026.102976. Used to check the Q1 mechanism description.  
   https://pubmed.ncbi.nlm.nih.gov/42561948/

2. Zainab et al. **Contemporary Management of Thalassemia: A Perspective on Current Standards and the Emergence of FDA-Approved Oral Therapy.** PMID 42698910. DOI 10.1002/hsr2.73178. Used to check the Q6 mention of mitapivat and review methods, not to establish an approval for ZX999.  
   https://pubmed.ncbi.nlm.nih.gov/42698910/

3. Skoulidis et al. **STK11/LKB1 Mutations and PD-1 Inhibitor Resistance in KRAS-Mutant Lung Adenocarcinoma.** PMID 29773717. Used to explain why indexed RCT metadata and the design of a mixed genomic/clinical analysis require separate interpretation.  
   https://pubmed.ncbi.nlm.nih.gov/29773717/  
   https://pmc.ncbi.nlm.nih.gov/articles/PMC6030433/

### Implementation references

4. React Markdown official security guidance.  
   https://github.com/remarkjs/react-markdown#security

5. Scanpy QC metrics documentation, including layer and raw options.  
   https://scanpy.readthedocs.io/en/1.11.x/generated/scanpy.pp.calculate_qc_metrics.html

6. Scanpy count normalization documentation.  
   https://scanpy.readthedocs.io/en/latest/api/generated/scanpy.pp.normalize_total.html

7. Scanpy preprocessing and clustering tutorial.  
   https://scanpy.readthedocs.io/en/latest/tutorials/basics/clustering.html

### Supplied project evidence

- `phronesis_codebase.zip`
- `Untitled document.docx`, Q1 through Q6 and included activity/plot screenshots
- Current source, focused test logs, recording-registry traces, native COPASI execution, and HDF5 structural inspection described in Section 2

## 13 Completion criteria for the repair work

A repair is complete when the requested operation and its applied arguments match, material claims are supported by their cited evidence, missing inputs and failed sources remain explicit, and artifacts preserve correct scientific provenance and task state.

Specifically, the repeat run must perform the real scan, reject the invalid parameter for the right reason, correctly handle milliseconds, preserve the missing-dataset safeguard, reject the unsupported ZX999 premise without unrelated-source claims, and accurately summarize the relevant paper. An unchanged green verification score is insufficient evidence that any of those repairs succeeded.

