# Phronesis repair prompt for Claude Code

Attach the source audit and card patch, then paste the text below. The UI change is implemented in the supplied patch; the backend findings are repair work remaining.

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
