# Audit repair plan (14 October 2026)

Combines `Phronesis_Full_Audit_and_Repair_Report.md` (findings F01–F16, regression cases in its §8) with the
six-area code review of the same date. Each workstream owns a disjoint set of files so they can be repaired in
parallel; the shared contracts in §1 are fixed first and every workstream codes against them.

Completion rule (report §13): an item closes when its execution or evidence contract passes a test, not when
answer wording becomes more cautious. Preserve working auth, caller-owned dataset resolution, no-synthetic-data
guards, precise numeric checks and cancellation/error disclosure.

## 1. Shared contracts

### 1.1 `SimulationPlan` (`phronesis/agents/query_context.py`) — F03, F04, F05, F06, F16

`QueryContext.simulation_plan: Optional[SimulationPlan]`, filled by the planner only when simulation is
requested and not forbidden. Legacy fields (`sim_duration`, `is_simulation_intent`, `suppress_simulation`,
`wants_steady_state`) stay populated for existing consumers.

| Field | Meaning |
|---|---|
| `action` | `time_course` \| `steady_state` \| `parameter_scan` |
| `biomodel_id` | validated `BIOMD\d{10}`/`MODEL\d{10}` or `None` (uploaded SBML or search) |
| `duration_seconds` | parsed request duration in seconds (default 60.0 only when none stated) |
| `duration_explicit` | the user stated a duration |
| `intervals` | output intervals (default 100) |
| `parameter_overrides` | `{name: float}` parsed with bounded `ast.literal_eval`; never silently dropped |
| `scan_parameter`, `scan_start`, `scan_end`, `scan_points` | required for `parameter_scan`; points include both endpoints |
| `target_species` | species the user asked to report |
| `validation_errors` | typed problems found while planning (e.g. `DURATION_OUT_OF_RANGE`, `DURATION_UNIT_UNSUPPORTED`, `SCAN_INCOMPLETE`); a plan with errors is not executed |

`SimulationConstraints` on the context (`simulation_constraints`): `simulation_requested`, `simulation_forbidden`,
`replacement_forbidden`, `reject_if_parameter_missing`, `scope` (`current_turn` \| `investigation`). Only
`simulation_forbidden` suppresses simulation; "do not substitute/invent/replace …" sets `replacement_forbidden`.

### 1.2 Task status (`AuthoritativeOrchestrationResult.task_status`) — F02

```json
{"task_status": "completed|partial|needs_input|failed|cancelled",
 "requested_operation": "parameter_scan", "executed_operation": "time_course",
 "request_match": false, "reason": "...", "grounding_status": "supported|partial|unsupported|contradicted|not_checked"}
```

Emitted in the final result, in the SSE `done` payload (it carries the whole result), in exports and in the run
manifest. The UI shows task status separately from the verification score. A missing dataset or model is
`needs_input`; an executed operation that differs from the requested one is `failed` with `request_match=false`.

### 1.3 Source coverage (`ToolResult.payload["coverage"]`) — F11

Aggregate tools report per-upstream `{"status": "ok|success_empty|error|not_run", "error_type": ...}` and an
aggregate `status` of `ok`, `partial`, `success_empty` or `unavailable`. All upstreams failing gives
`ToolResult.success=False` with `error_type="UPSTREAM_API_ERROR"`.

### 1.4 Claim verification records (`VerificationReport.claims`) — F01

`{"claim_id", "claim_type": "clinical|mechanistic|computational|absence", "claim_text", "evidence": [{"source_key", "passage"}], "status": "supported|contradicted|unsupported|not_checked", "reason"}`.
`verification_score` stays for compatibility; contradicted claims fail verification; unchecked material claims
prevent a "verified" label.

## 2. Workstreams

| WS | Area | Owned files | Findings |
|---|---|---|---|
| A1 | Planning and intent | `agents/query_planner.py`, `agents/query_context.py`, `agents/search_plan.py`, `agents/entity_lexicon.py`, `utils/biocontext/entity_resolver.py`; tests `test_query_planner.py`, `test_search_plan.py`, `test_entity_resolver.py` | F05, F06, F07 (anchors), F08 (entities), F09 (follow-up subject), intent false positives, duration parsing, planner task leak, cancellation swallow |
| A2 | Execution | `agents/authoritative_orchestrator.py`, `agents/discovery.py`, `tools/registry.py`, `tools/web_search.py`, `vivarix/engine.py`; tests `test_scientific_validation.py`, `test_streaming.py`, `test_integration.py`, `test_audit_regressions.py` | F02, F03, F04, F11, F16, §6 runtime integrity, greeting regex, name sanitizer, discovery nulls/species match, revision-worse, scope note, stability/eigenvalues, convergence, units, overrides-before-initials, ambiguous params, scan engine, BIOMD validation, curated fallback, web search budget/SSRF |
| B | Verification | `phronesis/phronesis/agents/verifier.py`, `agents/prompts.py`, `agents/citations.py`; tests `test_citations.py`, `test_prompts.py` | F01, hedge bypass, substring term match, tool-JSON-as-evidence, coverage rendering in `<tool_status>` |
| C | Memory and backend | `utils/memory/clinical_memory.py`, `app/backend/**`; | F09 (scope), connection leaks/WAL, blocking SQLite, unbounded constraints, gene-word false positives, fail-open user scope, BM25 recall, API input limits, model allow-list, chat concurrency, error leakage, health caching, SSE heartbeat, blocking handlers, dead imports, prefix reset |
| D | Single-cell and datasets | `lumicell/**`, `datasets/**`, `data/`; test `test_datasets.py` | F13, F14, normalization branches, `use_raw`, padj reporting, PCA bound, Ensembl var_names, method recording, SCP search retry/TLS |
| E | Web UI | `web/**` | F12, F02 (UI), F10, stream error handling, thread race, URL field, disease-targets card, dev toolbar, accessibility, model picker, thread delete → server reset, health badge, web test/lint scripts |
| F | Clinical modules | `drugintel/**`, `diseaseintel/**`, `trials/**`, `variants/**`, `provenance/**`, `export/**`, `evals/**`, `extensions/**`, `sources/**`; tests for those modules | F15, F02 (export/manifest), ChEMBL salt forms, timeout budgets, CT.gov 429, OT dedup/cache, NDA/BLA family, openFDA gating, variant retries, digests, version probes, export limits |
| G | Literature | `lithoscribe/**` | F07 (retrieval), F08 (study design), PDF RAG dim and scoping, Zotero key/redirect, batch download, PDF checks, S2 retries, Zotero filters/writes, synthesis honesty, JATS parsing, dangerous code, hydra races |
| H | Knowledge graph, legacy tools, Streamlit | `synapta/**`, `vivarix/agents/**`, `vivarix/tools/**`, `app/frontend/**` | PCST edge order, GraphRAG output, subgraph crashes, filter escaping, edge-index cache, legacy steady-state/scan bugs, Streamlit upload dedupe and isolation |
| I | Repo hygiene | `.gitignore`, `.coveragerc`, `pyproject.toml`, `mkdocs.yml`, `sonar-project.properties`, `.github/**`, `docs/**` (except this plan), root `README.md` | gitignore `lib/` and `.env.example`, coverage config, CI runs `tests/` and web, ruff gate, deps split, bandit skips, mypy job, mkdocs nav, sonar, fork leftovers |

## 3. Deferred (need a decision, credentials or network)

- Replacing `data/pbmc3k_processed.h5ad` with a verified original (D removes the truncated file only if a verified
  replacement cannot be produced; never repair the header).
- Git LFS for `phronesis/synapta/tests/files` (334 MB); LICENSE copyright holder; first release tag `v1.49.1`.
- Live repeat of the report §10 prompts (needs the configured model and network).
- Full process isolation for COPASI jobs is implemented as a bounded executor plus lock-wait timeout; a
  subprocess-per-job design is follow-up work if timeouts remain a problem.

## 4. Regression cases (report §8)

Each workstream adds the cases it owns as offline tests in `tests/test_repair_<ws>.py`. Network cases use
`@pytest.mark.network`.
