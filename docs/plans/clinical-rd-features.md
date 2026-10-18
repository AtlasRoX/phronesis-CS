# Phronesis: parallel implementation plan for features 1, 2, 3, 4, 5, 6, 8, 9

## 0. Constraints this plan is built around (verified in code)

- The repo has no commits, so there are no worktrees. Eight agents edit the same tree at the same time, so **every file has exactly one owner**.
- Several hot spots would be edited by almost every feature if left alone:
  - `authoritative_orchestrator.py:execute_inquiry` (Stage 1 append block at L449-518, `_RUNNING_TITLES`, `_describe_tool_result`, artifacts at L617-623, scope notes at L712-725, fallback at L766-777, verification at L783-850)
  - `prompts.py:build_evidence_block` and `SYNTHESIS_SYSTEM_PROMPT` rule 2
  - `verifier.py:verify_response` (the observed-id loop at L233-283 and the identifier checks)
  - `clinical_memory.py:collect_identifiers` / `get_verified_identifiers`
  - `registry.py:register_default_biomedical_tools` / `execute`
  - `main.py`
  - `page.tsx` (L586 buildAssistantMessage, L649 requestBody, L2270-2300 message render and actions, L2363+ composer)
  - `MarkdownRenderer.tsx`, `ThinkingProcess.tsx`, `chat.ts`, `globals.css`

  A **Foundation** step therefore converts every one of these into a hook or slot first, serially, before any feature work starts.
- Facts that constrain the design:
  - tests/test_audit_regressions.py:131 asserts the exact Lumicell kwargs.
  - tests/test_streaming.py asserts "Target: KRAS", "skipped at your request" and the Lithoscribe step title "Found 1 relevant literature passage".
  - `_RUNNING_TITLES[...].format(**title_ctx)` raises KeyError on unknown keys.
  - The offline recording registry registers only today's tools.

## 1. Overview and how the features compose

```
user query + dataset_ids
   │
   ├─ (8) resolve_attachments(user_id, ids, query) ──► DatasetRef[]
   ├─ (3) build_query_context(...) ──► QueryContext (entities, intents, decisions, attachments)
   │
   Stage 1 (parallel, via tracked()):  synapta │ lithoscribe(5 grades/integrity inside) │ websurfer
                                     + EvidenceSource plugins: (1) clinical_trials, (2) chembl + openfda, (6) variant_intel
   Stage 2: vivarix (BIOMD or (8) uploaded SBML) │ lumicell (typed path or (8) uploaded h5ad/csv)
   artifact builders: (5) literature, (8) datasets, (9) sources │ plugin artifacts: clinical_trials, drug_intel, variant_intel
   Stage 3 discovery (aliases from 3, grade-weighted literature from 5)
   Stage 4 synthesis: evidence block = built-ins + plugin evidence_xml + decorators (5 grades, 8 provenance, 3 query_entities)
                      system prompt = base + registered component lines + rules
   Stage 5 verifier: legacy checks + generic ID families (NCT/CHEMBL/HGNC/ontology/rs/VCV/CIViC/GEO/ds_)
                     + context ids (3 resolver, 8 attachments, plugin allowed_identifiers)
                     + vetoes (5 retracted) + registered checks (1 trial figures, 2 IC50/FAERS, 5 EoC/design, 6 frequencies, 8 consent)
                     + claim_support (for 4)
   (4) build_citation_map(final answer, tool_results, report, history, ctx, plugin citation records)
   (9) RunRecorder: every registry.execute call plus stage notes ──► run_manifest (+ RunStore)
   Stage 6 memory (identifier keys for all families)
   result: response, citations, run_manifest, query_context, execution_artifacts{...}
```

How the features connect:
- QueryContext (3) feeds the trials, drugs and variants plugins. Each plugin falls back to its own parser when ctx lists are empty, so features 1, 2 and 6 never wait on feature 3.
- Citation entries (4) carry the evidence_level and retracted flags that grading (5) adds to Lithoscribe passages.
- Plugins emit generic `CitationRecord`s, so NCT, CHEMBL, VCV and EID chips get titles and links.
- Export (9) renders the citations list and the verification report and records every tool call.
- Uploads (8) feed Lumicell (h5ad/csv) and Vivarix (SBML) through `DatasetRef` on the QueryContext.

## 2. Foundation (one agent, serial, first)

The foundation creates the hook modules, stubs, slots and dependencies listed in `foundation_spec`:
- `phronesis/agents/query_context.py` and a stub `query_planner.py` that reproduces today's parsing exactly
- `phronesis/sources/` (the EvidenceSource protocol plus a registry)
- `phronesis/extensions/` (hooks, the ID-family table, the extension loader and one stub module per feature)
- `phronesis/utils/http_limits.py`
- stubs for citations, provenance and datasets
- empty routers
- the orchestrator, verifier, prompts, memory, registry, main, discovery, Vivarix-signature and Lithoscribe-signature hook edits
- frontend types, slot components, MessageArtifacts, MarkdownRenderer citations prop, ThinkingProcess plan chips, page.tsx wiring
- `tests/fakes.py`
- dependencies (python-multipart, python-docx, fpdf2, markdown-it-py)

After the foundation lands, the shared files are frozen until the Integration step.

## 3. Work packages (parallel, disjoint ownership)

| Key | Feature | Owns (summary) |
|---|---|---|
| trials | 1 ClinicalTrials.gov v2 | phronesis/trials/*, extensions/trials.py, TrialLandscapeArtifact, types/trials.ts, tests/test_clinical_trials.py |
| drugs | 2 ChEMBL + openFDA | phronesis/drugintel/*, extensions/drugs.py, DrugSafetyArtifact, types/drugs.ts, tests/test_drug_intel.py |
| entities | 3 normalization + planner | query_planner.py, query_context.py (additive), entity_lexicon.py, entity_resolver.py, extensions/entities.py, QueryPlanChips, types/queryPlan.ts, tests |
| citations | 4 clickable citations | agents/citations.py, extensions/citations.py, lib/citations.ts, CitationRef/CitationPopover, types/citations.ts, localStorageService.ts, tests |
| grading | 5 grading + retraction | lithoscribe/evidence_grading.py, integrity.py, synthesis_engine.py, biocontext/client.py, agents/discovery.py, extensions/grading.py, EvidenceSources, types/literature.ts, tests |
| variants | 6 ClinVar/CIViC/gnomAD/cBioPortal | phronesis/variants/*, extensions/variants.py, VariantArtifact, types/variants.ts, tests |
| uploads | 8 dataset/model upload | phronesis/datasets/*, vivarix/engine.py, routers/datasets.py, extensions/uploads.py, composer/library/single-cell components, datasetService.ts, types/datasets.ts, tests |
| export | 9 export + manifest + rerun | phronesis/provenance/*, phronesis/export/*, routers/export.py, extensions/export.py, MessageActionsExtra/ExportMenu/RerunDiffPanel, exportService.ts, types/runs.ts, tests |

Rules for every feature agent:
- No edits outside your owned files. If you hit a needed shared change, write it down in your final report under "INTEGRATION REQUESTS".
- Styles go only in your own `*.module.css` and reuse the existing CSS variables. Nobody edits globals.css.
- Never install packages; the foundation already did.
- Never start servers on 8000 or 3000. Feature agents should not start servers at all; use TestClient.
- Run your own pytest file, then the full suite. `npx tsc --noEmit` errors in other agents' in-progress files are not yours.
- Fixtures are trimmed real responses captured live into tests/fixtures/<feature>/. Run Python that reads downloaded files with `-I`.

## 4. Integration (one agent, after all features)

See `integration_spec`. In short:
- Cross-feature reconciliation: the planner uses the variant parser and drug names, citations ingest grading fields, the export bundle includes citations.
- The remaining shared wiring: the `/api/agents` text, the casual intro, the wrong PMID 10712599, and the composed-prompt vendor test.
- Merging INTEGRATION REQUESTS, the full pytest run, node --test, tsc, and a live smoke test on port 8011 (frontend on 3011 if used).

## 5. Risks, scope cuts, order

**Order**: Foundation, then 8 features in parallel, then Integration.

**Biggest risks**:
1. The hook surface is wrong or incomplete. Mitigation: hooks are generic and keyed, and features record anything missing as INTEGRATION REQUESTS instead of editing shared files.
2. Existing tests drift. Mitigation: the stub planner reproduces today's parsing verbatim; plugins are skipped when their registry tools are not registered (the offline registry); `build_evidence_block` output is byte-identical with no plugins.
3. Verifier strictness causes more revision passes. Accepted: exact-string checks are intentional.
4. Upstream rate limits: NCBI 3 rps is shared by ClinVar, dbSNP and GEO through one limiter; gnomAD returns 429 on the 3rd call; Crossref allows 5-10 rps.
5. localStorage quota. Artifacts are compact and capped, and the citations budget is ≤24 KB.
6. Upload security: the auth-before-parse pattern, HDF5 external links, SBML DTD/comp references, and typed-path reads.

**MVP scope cuts (deliberate)**:
- Feature 1: arm attribution of figures is not enforced; there is no LLM trial-query planning.
- Feature 2: at most 2 drugs per turn; no set_id check; no strict FDA-approval check (left to the prompt).
- Feature 3: Synapta runs only for the primary gene; the LLM planner only runs in auto mode for ambiguous queries; the memory entity vault is not normalized.
- Feature 4: no lazy `/api/citations/resolve` endpoint (no Crossref/CT.gov on open); no "Sources" footer.
- Feature 5: no inline grade badges in MarkdownRenderer; heuristic grades never trigger EVIDENCE_GRADE defects.
- Feature 6: SNV-only gnomAD; no fusions, exon events or indel normalization; at most 4 cBioPortal studies; never pooled.
- Feature 8: no sidebar Datasets entry (library opens from the attach menu); no drag-and-drop; no download endpoint; the PHI check is advisory; no retention policy beyond quotas; .zarr upload is not supported.
- Feature 9: DOCX links are rendered as text (url); answer raw HTML is dropped; full re-runs cannot replay the original memory; a client-supplied manifest rerun is limited to allow-listed tools.
