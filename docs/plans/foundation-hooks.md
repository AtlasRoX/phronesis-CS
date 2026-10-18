# Foundation hooks for feature extensions

The shared pipeline files (orchestrator, prompts, verifier, memory, registry, `main.py`, `page.tsx`,
`MarkdownRenderer`, `ThinkingProcess`, `chat.ts`) are frozen. A feature plugs in only through the hooks
below, from files it owns. With no extension registered, behavior and output are identical to the
pre-foundation code (the evidence block is byte-identical; `tests/test_foundation_hooks.py` checks this).

Every hook call site is wrapped in `try/except` and logs failures, so one faulty extension cannot break a
turn. All registries are keyed and idempotent: registering an existing key replaces it.

## 1. How a feature registers

Each feature owns one module in `EXTENSION_MODULES` (`phronesis/extensions/__init__.py`):

| Module | Owner |
|---|---|
| `phronesis.extensions.entities` | ENTITIES (3) |
| `phronesis.extensions.grading` | GRADING (5) |
| `phronesis.extensions.citations` | CITATIONS (4) |
| `phronesis.extensions.trials` | TRIALS (1) |
| `phronesis.extensions.drugs` | DRUGS (2) |
| `phronesis.extensions.variants` | VARIANTS (6) |
| `phronesis.extensions.uploads` | UPLOADS (8) |
| `phronesis.extensions.export` | EXPORT (9) |

At import time the module registers its hooks and sources. It may also define
`register_tools(registry) -> None`, which registers its `ToolUniverseEntry` tools.

```python
# phronesis/extensions/trials.py
from phronesis.extensions import hooks
from phronesis.sources import EvidenceSource, register_source

class ClinicalTrialsSource(EvidenceSource):  # defined in this module
    ...

register_source(ClinicalTrialsSource())
hooks.register_prompt_rule("trials", "Clinical figures come only from a <posted_result> ...")

def register_tools(registry) -> None:
    registry.register(ToolUniverseEntry(name="lithoscribe_clinical_trials_search", ...), executor=...)
```

- `load_extensions() -> List[ModuleType]` imports every module once per process. A module that fails to
  import is logged and skipped. It is called lazily by the orchestrator, `get_sources()`,
  `get_synthesis_system_prompt()`, `verify_response()`, memory consolidation and
  `register_default_biomedical_tools()`.
- `register_extension_tools(registry)` calls each loaded module's `register_tools`. It runs at the end of
  `register_default_biomedical_tools`, so the real registry has the feature tools. The offline test
  registry (`tests/fakes.py`) does not, unless a test passes `extra_tools`.
- Import rule: `hooks.py` and `id_families.py` import nothing from `phronesis`. Extension modules should
  import heavy dependencies lazily, inside functions.

## 2. Evidence sources (`phronesis/sources`)

```python
from phronesis.sources import EvidenceSource, CitationRecord, SourceRunState, register_source, get_sources, unregister_source
from phronesis.agents.query_context import QueryContext, SourceDecision
```

| Member | Signature / default | Used by |
|---|---|---|
| `key` | `str`, the tool_results key and step id | everywhere |
| `agent`, `action` | Phronesis agent label; the `steps[]` action | steps, selected_agents |
| `required_tools` | `Tuple[str, ...]` | if any is missing from the registry, the source is skipped with decision `tool not available`, no step and no scope notes |
| `stage`, `order` | `1`; `100` | only stage 1 runs; sorted by `(order, key)` |
| `should_run(ctx)` | `-> SourceDecision` (default `run=False, "not applicable"`) | routing; stored in `ctx.decisions[key]` and in the plan step `extra.sources` |
| `running_title(ctx)` | `-> str` (default `"Running {key}"`) | running step title |
| `async run(ctx, registry, state)` | `-> ToolResult` | runs in Stage 1 in parallel with Synapta/Lithoscribe/WebSurfer |
| `describe(result, ctx)` | `-> (title, detail)` (default `"{key} failed"` with `"{ERR}: {error[:240]}"`, or `"{key} finished"`) | done/error step |
| `evidence_xml(result)` | `-> str`, success only, must escape everything | `<evidence>` after `<web_search>`, before `<biodiscovery_ranking>` |
| `allowed_identifiers(result)` | `-> Dict[family, Set[str]]` | merged into the verifier `context_identifiers` |
| `citations(result)` | `-> List[CitationRecord]` | `build_citation_map(..., source_citations={key: [...]})` |
| `artifact(result)` | `-> Optional[(key, value)]`; dict values for one key are shallow-merged; JSON round-tripped | `execution_artifacts` |
| `fallback_markdown(result)` | `-> str` | deterministic answer when the model is unavailable, before the discovery summary |
| `scope_notes(ctx, result)` | `-> List[str]`, `result` is None when not run | `<tool_status>` notes, for every evaluated source |
| `plan_fact(ctx)` | `-> Optional[str]`, active sources only | plan step text |

`SourceRunState(registry, ctx).once(key, factory)` shares one upstream call between sources in the same
inquiry: the first caller starts `factory()` as a task, and later callers await that same task. For
example, `chembl` and `openfda` both use `state.once("drug_resolve", ...)`.

`CitationRecord` fields: `type, id, title, url, passage, section, authors, journal, year, evidence_level,
retracted, source_agent, source_tool`.

## 3. Hook registries (`phronesis/extensions/hooks.py`)

| Kind | Register | Callback signature | Call site |
|---|---|---|---|
| prompt component | `register_prompt_component(key, line)` | a `- Name: ...` line | appended after the `- BioDiscovery:` bullet of `SYNTHESIS_SYSTEM_PROMPT` |
| prompt rule | `register_prompt_rule(key, text)` | rule text | `<additional_rules>` numbered from 9 in registration order, right after `</grounding_rules>` |
| evidence decorator | `register_evidence_decorator(slot, key, fn)` | see the slots below | `build_evidence_block` |
| step augmenter | `register_step_augmenter(tool_key, key, fn)` | `fn(res, title_ctx) -> (title_override or None, [extra detail parts])` | after each tracked step, built-in or plugin; the parts are joined with ` · ` |
| artifact builder | `register_artifact_builder(key, fn)` | `fn(tool_results, ctx) -> dict` | merged into `execution_artifacts` after the plugin artifacts |
| scope notes | `register_scope_note_provider(key, fn)` | `fn(ctx, tool_results) -> List[str]` | `<tool_status>` |
| serialization filter | `register_serialization_filter(key, fn)` | `fn(tool_key, data_copy) -> dict` | verifier identifier harvest and evidence docs, memory summaries and identifiers |
| evidence doc provider | `register_evidence_doc_provider(key, fn)` | `fn(tool_key, result) -> Optional[List[(evidence_key, doc)]]` | verifier: the first provider that returns a list replaces that tool's default grounding docs (the whole-tool JSON, or its passages) |
| identifier veto | `register_identifier_veto(key, fn)` | `fn(tool_results) -> {family: {id: reason}}`; the family is `pmids`, `dois` or any id family | verifier: a vetoed id gives a CRITICAL `RETRACTED_CITATION`, is never verified, and beats observed and historical ids |
| verification check | `register_verification_check(key, fn)` | `fn(vctx: VerificationContext) -> [{category, severity, description, suggested_correction, claim}]` | verifier, before scoring; the severity is `CRITICAL`, `MAJOR` or `MINOR` |

Evidence decorator slots:

- `literature_document_attrs`: `fn(passage) -> Dict[str, str]`, extra attributes on `<document>`. The values are escaped and `None` values are skipped.
- `literature_tail`: `fn(lit_data) -> str`, XML placed before `</literature>`. The literature block also renders when there are no passages but the tail is non-empty.
- `lumicell_tail`: `fn(lum_data) -> str`, XML placed before `</lumicell>`.
- `vivarix_attrs`: `fn(viv_data) -> Dict[str, str]`, extra attributes on `<vivarix>`.
- `query_entities`: `fn(ctx) -> str`, XML placed first inside `<evidence>`. Only called when `query_context` is passed.

`<document>` also gets `citation=` from `p['citation']`, falling back to `p['citation_label']`.

`VerificationContext` fields: `query, response, sentences, table_rows` (markdown rows without the `|---|`
separators), `tool_results, evidence_documents, evidence_keys` (parallel: `tool:<name>`, `PMID:<id>`,
`DOI:<lower doi>`, `passage:<id>`, or the provider's keys), `observed` (the legacy keys `biomodels, pmids,
dois, ensembl, uniprot` plus every family), `cited` (the same keys, ids extracted from the response),
`historical, context_identifiers, vetoed, query_context, hedge_pattern`.

Test helpers: `hooks.isolated_hooks()` is a context manager that discards registrations made inside it.
Also available: `hooks.unregister(kind, key)`, `unregister_evidence_decorator(slot, key)`,
`unregister_step_augmenter(tool_key, key)`, `hooks.clear(kind=None)` and
`phronesis.sources.unregister_source(key)`.

## 4. Identifier families (`phronesis/extensions/id_families.py`)

`FAMILIES` (name, pattern, normalization, `canonical_ids` keys):

| name | pattern | normalize | canonical keys |
|---|---|---|---|
| nct | `\bNCT\d{8}\b` (case-insensitive) | upper | `nct_ids` |
| chembl | `\bCHEMBL\d{1,9}\b` (case-insensitive) | upper | `chembl_ids`, `chembl_id` |
| hgnc | `\bHGNC:\d{1,6}\b` | none | `hgnc_ids`, `hgnc_id` |
| ontology | `\b(?:EFO\|MONDO\|Orphanet\|HP\|DOID)[_:]\d{4,9}\b` | `:` becomes `_` | `ontology_ids`, `efo_id` |
| rsid | `\brs\d{3,12}\b` | lower | `rsids` |
| clinvar | `\bVCV\d{9}(?:\.\d+)?\b` | drop the version | `clinvar_vcv` |
| civic | `\b(?:EID\|AID)\d{1,6}\b` | none | `civic_ids` |
| geo | `\bGS[EM]\d{1,9}\b` | none | `geo_accessions` |
| dataset | `\bds_[0-9a-f]{12}\b` | none | `dataset_ids` |

An id counts as observed when it appears in any of these:

- the serialized successful tool data (after the serialization filters)
- the passages
- the `canonical_ids` under the family's keys (comma-separated)
- `historical_identifiers[name]` (memory)
- `context_identifiers[name]`
- the user's own question

The check is hedge-independent. A cited id that was never observed is a CRITICAL `IDENTIFIER` defect.
Verified ids are reported as `"{LABEL}:{id}"`, for example `NCT:NCT02296125`.

Helpers: `extract_family_ids(text) -> Dict[name, Set[str]]`, `family_names()`,
`normalize_ids(name, ids)` and `get_family(name)`. To add a family, add it here. That file is frozen, so
add it at Integration.

## 5. Query context and planner (ENTITIES owns these after the foundation, additive changes only)

- `phronesis/agents/query_context.py`: `ResolvedEntity`, `SourceDecision`, `DatasetRef` (with
  `to_public()`, which never includes paths) and `QueryContext`. Its properties are `genes`, `drugs`,
  `diseases`, `variants`, `primary_symbol`, `primary_display`, `primary_ensembl_id`, `dataset_attachment`,
  `model_attachment` and `analysis_dataset_path`. Its methods are `primary_aliases()`, `should_run(name)`,
  `web_query()`, `biomodel_search_term()`, `lumicell_target_gene()`, `plan_facts()`, `to_step_extra()`,
  `to_artifact()` and `observed_identifiers()`. `extras: Dict[str, Any]` is free space for features.
- `phronesis/agents/query_planner.py`: `extract_primary_target`, `detect_simulation_suppression`,
  `extract_dataset_path` (calls `phronesis.datasets.paths.is_allowed_typed_path`), `extract_biomodel_id`,
  `has_single_cell_intent`, `parse_duration_seconds`, `BIOMODEL_TARGET_HINTS`,
  `decide_builtin_sources(ctx)`, and `async build_query_context(*, query, clean_query, registry,
  enable_web_search, attachments, attachment_errors, user_id, thread_id, model_name, api_key,
  llm_factory=None, budget_s=3.5) -> QueryContext`.
- Built-in decisions: `synapta`, `lithoscribe`, `websurfer`, `vivarix`, `lumicell`. The orchestrator
  gates each built-in agent on `ctx.should_run(name)`.

## 6. Orchestrator contract (`execute_inquiry`)

- New kwargs: `dataset_ids: Optional[List[str]] = None`, `persist_memory: bool = True`,
  `recall_memory: bool = True`.
- New result fields: `citations: Dict` (default `{}`), `run_manifest: Optional[Dict]` and
  `query_context: Optional[Dict]` (`ctx.to_artifact()`).
- Order of operations:
  1. `load_extensions()`
  2. step `plan` running
  3. `resolve_attachments(user_id, ids, clean_query)`
  4. `build_query_context(...)`
  5. memory recall
  6. source routing
  7. an `attachments` step, when there are attachments or attachment errors
  8. step `plan` done, with `extra = {thread_id, memory_nodes_active, **ctx.to_step_extra()}`
  9. Stage 1, with the built-ins and active sources in parallel
  10. Stage 2: Vivarix (an uploaded SBML when `ctx.model_attachment` is set) and Lumicell
      (`ctx.analysis_dataset_path`)
  11. artifacts
  12. discovery (`target_aliases=ctx.primary_aliases()`)
  13. scope notes
  14. synthesis with `get_synthesis_system_prompt()`
  15. verification with `context_identifiers` and `query_context=ctx`
  16. `build_citation_map(...)` on the answer before the warning banner
  17. memory, skipped when `persist_memory=False`
  18. `rec.finalize(...)`
- Uploaded model call: `registry.execute("vivarix_simulate_time_course", biomodel_id=att.id, duration,
  intervals=40, sbml_path=att.model_path, model_label=att.display_name,
  model_provenance=att.public_provenance)`. `vivarix_compute_steady_state` is called the same way.
- Lumicell gets `dataset_name=att.display_name` only for attachments.
- Every Vivarix and Lumicell result passes through
  `phronesis.datasets.provenance.postprocess_vivarix_result(res, ctx)` or
  `postprocess_lumicell_result(res, ctx)`.
- `ChatRequest.dataset_ids`: at most 5 ids, each matching `^ds_[0-9a-f]{12}$`, otherwise 422. Both chat
  endpoints forward it.
- Routers mounted from `main.py`:
  - `app/backend/routers/datasets.py`: `router = APIRouter(prefix="/api/datasets", tags=["datasets"])`
  - `app/backend/routers/export.py`: `router = APIRouter(tags=["export"])`
- CORS allows `PATCH` and exposes `Content-Disposition`.

## 7. Feature-owned stubs (fill in the bodies; keep the signatures)

- `phronesis/agents/citations.py` (CITATIONS): `build_citation_map(response, tool_results, verification_report, historical_identifiers=None, query_context=None, source_citations=None) -> dict`. The stub returns `{}`; exceptions are caught and give `{}`. `VerificationReport.claim_support` holds `[{sentence, evidence_key, passage_id}]` and `VerificationReport.vetoed_identifiers` holds strings such as `["PMID:9500320"]`.
- `phronesis/provenance/run_manifest.py` (EXPORT): `RunRecorder(*, query, thread_id, model_name, enable_web_search, user_id, api_key_supplied)` has these methods:
  - `set_routing(ctx)`
  - `set_memory_context(*, n_evidence, n_instructions, memory_summary)`
  - `note_step(step, inputs=None, outputs=None)`; `biomodel_search` records `{search_term, limit}` and `{hits, selected, fallback}`
  - `start_biomodel_resolution(accession)`
  - `start_dataset_hash(ref)`, where ref is a str path or a DatasetRef
  - `set_synthesis(**fields)`, called with `system_prompt_sha256, synthesis_prompt_sha256, scope_notes, llm_ok, fallback_error, response_words`
  - `set_verification(*, first_pass, final, correction_attempted, correction_error)`, where pass dicts are `{passed, score, n_defects}`
  - `async finalize(*, response, duration, framework="authoritative-react-compiler")`

  The module also exposes `activate_recorder(rec) -> Token`, `reset_recorder(token)`,
  `current_recorder()` (a ContextVar) and `record_tool_call(*, tool_name, entry, inputs, raw_inputs,
  result, started_at, elapsed_seconds)`. `BiomedicalToolRegistry.execute` calls `record_tool_call` for
  every call, including validation failures and unknown tools. `inputs` is None when validation failed;
  the function is looked up at call time and must never raise. The orchestrator also wraps every recorder
  call.
- `phronesis/datasets/` (UPLOADS):
  - `attach.py`: `AttachmentResolution(attachments, errors)` and `async resolve_attachments(user_id, dataset_ids, query)`
  - `paths.py`: `is_allowed_typed_path(path) -> bool`
  - `provenance.py`: `postprocess_lumicell_result(res, ctx)` and `postprocess_vivarix_result(res, ctx)`
- Engine signatures, accepted and ignored in the stubs:
  - `VivarixSimulationEngine.simulate_time_course`, `compute_steady_state` and `run_parameter_scan` take `sbml_path=None, model_label=None, model_provenance=None` (UPLOADS); the registry inputs and executors forward them.
  - `LiteratureSynthesisEngine.execute_literature_synthesis(query, max_articles=8, check_integrity=True)` (GRADING); the tool timeout is now 40 s.
  - `BioDiscoveryEngine.prioritize_hypotheses(inquiry, primary_target, tool_results, *, target_aliases=None)` (GRADING).
- Memory: `ClinicalMemoryEngine.collect_identifiers(data, tool_key="")` returns the legacy keys plus every
  family name and applies the serialization filters. `get_verified_identifiers` returns the same keys.
- `phronesis/utils/http_limits.py` (frozen): `get_limiter(name, min_interval_s)` returns
  `MinIntervalLimiter`, an `async with` context manager. Names: `ncbi_eutils` 0.34 s (0.11 s with
  `NCBI_API_KEY`), `gnomad` 2.0, `crossref` 0.2, `clinicaltrials` 0.0, `openfda` 0.0.
  `TTLCache(maxsize=1024, ttl_s=3600)` provides `.get(key)`, `.set(key, value)` and `.clear()`.

## 8. Frontend slots

| File | Props | Owner | Rendered by |
|---|---|---|---|
| `TrialLandscapeArtifact.tsx` | `{ data: ClinicalTrialsArtifact }` | TRIALS | `MessageArtifacts` (`clinical_trials`) |
| `DrugSafetyArtifact.tsx` | `{ data: DrugIntelArtifact }` | DRUGS | `MessageArtifacts` (`drug_intel`) |
| `VariantArtifact.tsx` | `{ data: VariantIntelArtifact }` | VARIANTS | `MessageArtifacts` (`variant_intel`) |
| `EvidenceSources.tsx` | `{ data: LiteratureArtifact }` | GRADING | `MessageArtifacts` (`literature`) |
| `SingleCellArtifact.tsx` | `{ data: any; datasets?: DatasetPublic[] }` | UPLOADS | `MessageArtifacts` (`lumicell`) |
| `QueryPlanChips.tsx` | `{ plan: QueryPlanExtra }` | ENTITIES | `ThinkingProcess`, plan step when `extra.entities` is set |
| `CitationRef.tsx` | `{ citeKey, occurrence, map, href?, children }` | CITATIONS | `MarkdownRenderer` `a` override when `data-cite-key` is present |
| `lib/citations.ts` | `rehypeCitations({ map })` | CITATIONS | `MarkdownRenderer`, after rehype-raw, only when `citations.entries` is non-empty |
| `MessageActionsExtra.tsx` | `{ message, question, backendUrl, authHeaders }` | EXPORT | `msg-actions`, after Copy |
| `ComposerAttachButton.tsx` | `{ attached, onAttach, backendUrl, authHeaders, disabled? }` | UPLOADS | first item of the composer's left pill row |
| `ComposerAttachmentChips.tsx` | `{ attached, onRemove }` | UPLOADS | above the textarea |
| `MessageAttachmentChips.tsx` | `{ attachments }` | UPLOADS | user bubble, under the text |

- `MessageArtifacts` wraps each card in an error boundary that renders nothing if the card throws.
- Card order: trials, drugs, variants, literature, single-cell, all after `SimulationArtifact`.
- Types: each feature owns `web/src/types/<feature>.ts` (`trials`, `drugs`, `queryPlan`, `citations`,
  `literature`, `variants`, `datasets`, `runs`); changes are additive only. `chat.ts` (frozen) defines
  `ExecutionArtifacts` and the `Message` fields `citations`, `run_manifest`, `attachments` and
  `query_context`.
- `page.tsx` state: `attachedDatasets` holds at most 5, deduplicated by id, and stays set after sending.
  It is sent as `dataset_ids` on both `/api/chat/stream` and `/api/chat`, and copied to the user message
  as `attachments`.
- Styles go in your own `*.module.css`, using the existing CSS variables. Nobody edits `globals.css`.

## 9. Tests

`tests/fakes.py`:

- `build_recording_registry(extra_tools=None) -> (registry, calls)`
- `make_offline_orchestrator(monkeypatch, tmp_path, extra_tools=None) -> (orch, calls)`
- `called(calls, tool_name)`

`extra_tools` maps a tool name to `(InputSchema, fake)`. `fake(kwargs)` returns the data dict or a
`ToolResult`, and must be built from trimmed real responses saved under `tests/fixtures/<feature>/`.

`tests/test_audit_regressions.py` re-exports the old names and keeps the `offline_orchestrator` fixture.
