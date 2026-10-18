# Phronesis: Scientific Validation, Multi-Agent Architecture & Benchmark Report

> **Phronesis** — Biomedical Research Assistant  
> **Made by ChronoStrider Engineering (ChronoStrider Enginnerings)**

This document specifies the architecture, paper-to-feature mapping, and empirical validation results for the upgraded **Phronesis Biomedical Research Assistant**, engineered by **ChronoStrider Engineering** (**ChronoStrider Enginnerings**).

---

## 1. Executive Summary & Critical Findings Addressed

Prior to this upgrade, an inspection of the codebase revealed several architectural and scientific limitations:
1. **Orchestrator Bypass & Predefined Content**: `clinical_orchestrator.py` bypassed the underlying specialist pipelines and returned canned findings for Vivarix and Lumicell.
2. **Fixed Consensus Scores**: Consensus scores were hardcoded constant floats (`0.94`, `0.89`, `0.92`).
3. **Mock Reflection**: The reflexion verification stage merely printed console announcements without evaluating claims, identifiers, or numerical outputs.
4. **Keyword-Counting Memory**: Working memory search relied on naive string substring counts (`score += 1.0 + content_lower.count(kw)*0.2`).
5. **Hallucinated Clinical Success**: When LLM synthesis encountered missing evidence or failed, it produced a hardcoded conclusion asserting "positive clinical trial progress".

### Implemented Solutions
- **Authoritative Orchestrator (`phronesis/agents/authoritative_orchestrator.py`)**: Replaced the bypass with a unified multi-agent orchestrator connecting **🧬 Synapta**, **⚙️ Vivarix**, **🔬 Lumicell**, **📚 Lithoscribe**, and **🌐 WebSurfer**.
- **Real Execution Engines**: Integrated COPASI/BASICO for kinetic ODE integration and Scanpy/AnnData for single-cell transcriptomics.
- **PaperQA2 Literature Engine**: Iterative query decomposition, EuropePMC passage retrieval, DOI/PMID tracking, and explicit reporting of insufficient evidence.
- **Deterministic Reflexion Verifier (`phronesis/phronesis/agents/verifier.py`)**: Audits claims against simulator outputs, validates canonical identifiers, strips vendor/framework leaks, and enforces bounded self-correction.
- **Mem0 SQLite Vault with BM25 (`phronesis/utils/memory/clinical_memory.py`)**: Implements thread-isolated, typed memory nodes (`USER_INSTRUCTION`, `WORKING_HYPOTHESIS`, `RETRIEVED_EVIDENCE`, `COMPUTED_RESULT`, `FAILURE_LESSON`), contradiction handling, and Okapi BM25 probabilistic retrieval.
- **ToolUniverse Standardized Registry (`phronesis/tools/registry.py`)**: Standardizes tool inputs, outputs, canonical entity IDs (`ensembl_id`, `uniprot_id`, `biomodel_id`, `pmid`), timeouts, and structured error states (`TIMEOUT`, `VALIDATION_ERROR`, `SIMULATION_FAILED`).
- **Live Observed Readiness**: `/api/health` queries live upstream services (OpenTargets, EuropePMC, UniProt) and returns measured latency in milliseconds.

---

## 2. Paper-to-Feature Mapping

| Reference Paper / Framework | Core Principles Adapted | Implementation in Phronesis |
| :--- | :--- | :--- |
| **ReAct** (Yao et al., 2022) & **Biomni** (2025) | Unified multi-specialist coordination, dynamic reasoning-acting loops, routing strictly to relevant domains. | `AuthoritativeOrchestrator` determines active specialists dynamically based on query intent; eliminates hardcoded canned outputs. |
| **ToolUniverse** (2025) | Standardized biomedical tool registry, validated schemas, canonical entity IDs, timeout enforcement. | `BiomedicalToolRegistry` (`phronesis/tools/registry.py`) with Pydantic schemas, canonical IDs (`ensembl_id`, `uniprot_id`, `biomodel_id`), and typed error states. |
| **PaperQA2** (2024) & **OpenScholar** (2024) | Query decomposition, passage-level retrieval and reranking, DOI/PMID tracking, contradiction tracking, explicit limitations. | `LiteratureSynthesisEngine` (`phronesis/lithoscribe/synthesis_engine.py`) breaks clinical questions into mechanistic subqueries, extracts ranked EuropePMC passages, tracks DOIs, and flags missing evidence. |
| **Reflexion** (Shinn et al., 2023) | Deterministic audit of claims, numerical checks against tool execution artifacts, vendor leak prevention, bounded self-correction. | `ClinicalVerifier` (`phronesis/phronesis/agents/verifier.py`) detects unsupported clinical assertions, checks numerical consistency, enforces naming rules, and returns defect feedback. |
| **COPASI / BASICO** | Reproducible systems biology: kinetic ODE integration, parameter scans, steady-state computation, execution artifacts. | `VivarixSimulationEngine` (`phronesis/vivarix/engine.py`) runs deterministic LSODA ODE integration, caches SBML models locally, generates concentration trajectories and base64 plots. |
| **CellAgent** (2024) | Analytical single-cell transcriptomics: QC filtering, normalization, log1p, PCA, graph, Leiden clustering, Wilcoxon DE markers. | `LumicellAnalysisEngine` (`phronesis/lumicell/engine.py`) executes Scanpy/AnnData pipelines, preserves donor metadata, ranks cluster markers, and attaches observational disclaimers. |
| **Mem0** (2025) | Isolated persistent memory, typed semantic nodes, verification tiers, contradiction handling, BM25 retrieval. | `ClinicalMemoryEngine` (`phronesis/utils/memory/clinical_memory.py`) stores memories in SQLite with Okapi BM25 scoring, scopes retrieval by thread, and demotes conflicting provisional claims to `REFUTED`. |
| **LLMCompiler** (2023) | Dependency-aware execution DAG, parallel independent tool retrieval, bounded concurrency. | `AuthoritativeOrchestrator` runs Stage 1 (Synapta, Lithoscribe, WebSurfer) in parallel via `asyncio.gather`, followed by Stage 2 systems biology (Vivarix, Lumicell). |
| **BioDiscoveryAgent** (2024) | Evidence-backed hypothesis prioritization and actionable laboratory validation protocol formulation. | `BioDiscoveryEngine` (`phronesis/agents/discovery.py`) scores candidate targets using composite metrics (association, literature evidence, perturbation response) and formulates experiments. |
| **BFCL**, **STaRK**, **BixBench** (inspiration only) | Public benchmarks whose task categories inspired local regression tests. The official datasets and harnesses are NOT run and no benchmark scores are claimed. | `tests/test_scientific_validation.py` runs local integration and regression tests covering tool selection, entity resolution, ODE simulation, single-cell analysis, verifier audits, and memory isolation. |

---

## 3. Measured Benchmark Validation Results

All 25 scientific validation and regression tests in `tests/test_scientific_validation.py` executed cleanly against Python 3.12:

```text
============================= test session starts =============================
platform win32 -- Python 3.12.13, pytest-8.4.2, pluggy-1.6.0
collected 25 items

tests/test_scientific_validation.py::test_bfcl_tool_registry_validation_and_rejection PASSED [  4%]
tests/test_scientific_validation.py::test_stark_target_disease_association_retrieval PASSED [  8%]
tests/test_scientific_validation.py::test_stark_ambiguous_entity_handling PASSED [ 12%]
tests/test_scientific_validation.py::test_bixbench_vivarix_ode_simulation PASSED [ 16%]
tests/test_scientific_validation.py::test_bixbench_vivarix_steady_state PASSED [ 20%]
tests/test_scientific_validation.py::test_bixbench_vivarix_parameter_scan PASSED [ 24%]
tests/test_scientific_validation.py::test_bixbench_lumicell_requires_real_dataset_without_fabrication PASSED [ 28%]
tests/test_scientific_validation.py::test_paperqa2_literature_synthesis PASSED [ 32%]
tests/test_scientific_validation.py::test_reflexion_verifier_catches_unsupported_conclusion_and_vendor_leak PASSED [ 36%]
tests/test_scientific_validation.py::test_reflexion_verifier_passes_grounded_response PASSED [ 40%]
tests/test_scientific_validation.py::test_mem0_thread_isolation_and_bm25_retrieval PASSED [ 44%]
tests/test_scientific_validation.py::test_mem0_contradiction_detection PASSED [ 48%]
tests/test_scientific_validation.py::test_biodiscovery_hypothesis_prioritization PASSED [ 52%]
tests/test_scientific_validation.py::test_authoritative_orchestrator_end_to_end PASSED [ 56%]
tests/test_scientific_validation.py::test_cancellation_resilience PASSED [ 60%]
tests/test_scientific_validation.py::test_regression_unsupported_clinical_claim_fails_verification PASSED [ 64%]
tests/test_scientific_validation.py::test_regression_numerical_mismatch_14_92_vs_7_46_fails PASSED [ 68%]
tests/test_scientific_validation.py::test_regression_invented_doi_and_pmid_fails PASSED [ 72%]
tests/test_scientific_validation.py::test_regression_evidence_free_discovery_disabled PASSED [ 76%]
tests/test_scientific_validation.py::test_regression_unrelated_query_no_kras_default PASSED [ 80%]
tests/test_scientific_validation.py::test_regression_conversational_greeting_no_tools PASSED [ 84%]
tests/test_scientific_validation.py::test_regression_cross_user_memory_isolation PASSED [ 88%]
tests/test_scientific_validation.py::test_regression_provisional_memory_not_in_verified_context PASSED [ 92%]
tests/test_scientific_validation.py::test_regression_all_failed_parameter_scan_fails PASSED [ 96%]
tests/test_scientific_validation.py::test_regression_invalid_parameter_override_fails PASSED [100%]

================== 25 passed, 1 warning in 212.46s (0:03:32) ==================
```

### Detailed Test Coverage Across 25 Scientific Checks

1. **`test_bfcl_tool_registry_validation_and_rejection`**: Validates input schemas, enforces timeouts, rejects malformed payloads (`ValidationError`), and returns typed `ErrorType.TIMEOUT` and `ErrorType.VALIDATION_ERROR`.
2. **`test_stark_target_disease_association_retrieval`**: Confirms that target-disease association scores from OpenTargets GraphQL accurately extract `overall_association_score` and data-type sub-scores (genetic, somatic, pathway), correctly distinguishing association from entity search.
3. **`test_stark_ambiguous_entity_handling`**: Validates resolution of non-canonical gene symbols and graceful degradation when entities cannot be resolved.
4. **`test_bixbench_vivarix_ode_simulation`**: Verifies deterministic LSODA ODE integration using BASICO on BioModel `BIOMD0000000010`. Confirms model provenance, time points, final concentrations, solver settings, and base64 PNG trajectory charts.
5. **`test_bixbench_vivarix_steady_state`**: Confirms real COPASI steady-state calculation, returning final concentrations and non-zero reaction fluxes.
6. **`test_bixbench_vivarix_parameter_scan`**: Evaluates parameter sensitivity across multiple points, confirming non-linear dose-response curve generation.
7. **`test_bixbench_lumicell_requires_real_dataset_without_fabrication`**: Verifies that Lumicell rejects queries without dataset provenance with `ErrorType.DATASET_REQUIRED` and strictly analyzes real AnnData `.h5ad` files without synthetic generation.
8. **`test_paperqa2_literature_synthesis`**: Tests query decomposition into mechanistic subqueries, EuropePMC full-text XML retrieval, passage reranking, and DOI/PMID tracking.
9. **`test_reflexion_verifier_catches_unsupported_conclusion_and_vendor_leak`**: Evaluates `ClinicalVerifier` against an ungrounded response containing hallucinated clinical success and vendor name leaks (`OpenAI`, `NVIDIA`, `LangGraph`). Successfully flags defects and marks `passed = False`.
10. **`test_reflexion_verifier_passes_grounded_response`**: Confirms that evidence-backed responses referencing verified simulator outputs and literature passages pass verification.
11. **`test_mem0_thread_isolation_and_bm25_retrieval`**: Confirms thread isolation across distinct clinical sessions, typed nodes (`USER_INSTRUCTION`, `RETRIEVED_EVIDENCE`), and probabilistic Okapi BM25 ranking.
12. **`test_mem0_contradiction_detection`**: Verifies that adding an empirical verified memory contradicting a prior provisional node demotes the prior node to `REFUTED`.
13. **`test_biodiscovery_hypothesis_prioritization`**: Confirms multi-criteria ranking of candidate interventions with composite priority scores and structured experimental protocols.
14. **`test_authoritative_orchestrator_end_to_end`**: Validates end-to-end execution of the 6-stage DAG, producing real execution artifacts (`vivarix`, `lumicell`, `discovery`), honest dataset states, and verified synthesis.
15. **`test_cancellation_resilience`**: Verifies that canceling an in-flight query terminates background coroutines cleanly without uncaught exceptions or resource leaks.
16. **`test_regression_unsupported_clinical_claim_fails_verification`**: Confirms that ungrounded clinical trial claims (e.g. claiming Phase III trial success without evidence) fail verification with `CLINICAL_CLAIM` defects and score < 0.50.
17. **`test_regression_numerical_mismatch_14_92_vs_7_46_fails`**: Confirms that numerical claims with >5% deviation (such as 14.92 against an observed 7.46) fail verification with a critical `NUMERICAL` defect.
18. **`test_regression_invented_doi_and_pmid_fails`**: Confirms that unobserved DOIs or PMIDs are caught as critical `CITATION` defects and prevent verification.
19. **`test_regression_evidence_free_discovery_disabled`**: Confirms that the BioDiscoveryEngine returns `INSUFFICIENT_EVIDENCE` and empty rankings when tool evidence is missing, eliminating invented 0.87 scores.
20. **`test_regression_unrelated_query_no_kras_default`**: Confirms that general questions (e.g. light-independent reactions of photosynthesis) do not route to KRAS, Vivarix ODEs, or Lumicell.
21. **`test_regression_conversational_greeting_no_tools`**: Confirms that casual conversational greetings return direct assistant dialogue without dispatching biomedical agents.
22. **`test_regression_cross_user_memory_isolation`**: Confirms that memory retrieval is scoped by `user_id` and does not leak private investigation findings across users.
23. **`test_regression_provisional_memory_not_in_verified_context`**: Confirms that provisional tool findings remain in `active_hypotheses` and never enter `verified_evidence`.
24. **`test_regression_all_failed_parameter_scan_fails`**: Confirms that parameter scans where all points fail return `success=False` with `SIMULATION_FAILED`.
25. **`test_regression_invalid_parameter_override_fails`**: Confirms that invalid parameter overrides fail simulation explicitly rather than being silently ignored.

---

## 4. Setup, Run & Archiving Instructions

### Prerequisites
- Python 3.12+ (managed via `uv` or virtual environment)
- Node.js 18+ & npm

### Starting the Services

1. **Activate Virtual Environment & Install Dependencies**:
   ```bash
   uv sync --extra dev --frozen
   ```

2. **Start the FastAPI Backend Server**:
   ```bash
   .\.venv\Scripts\python.exe -m uvicorn app.backend.main:app --host 127.0.0.1 --port 8000
   ```
   Verify readiness:
   ```bash
   curl http://127.0.0.1:8000/api/health
   ```

3. **Start the Next.js Frontend Dev Server**:
   ```bash
   cd web
   npm run dev
   ```
   Open `http://localhost:3000` in your browser.

4. **Run the Scientific Validation Test Suite**:
   ```bash
   .\.venv\Scripts\python.exe -m pytest tests\test_scientific_validation.py -v
   ```

### Codebase Archiving Utility

The codebase packaging utility (`scripts/archive_codebase.py` and `scripts/archive_codebase.ps1`) enforces strict exclusion rules:
- **Excluded Directories**: `.venv`, `node_modules`, `.next`, `__pycache__`, `.git`, `.phronesis_memory`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `dist`, `build`, `tmp`, `temp`, `logs`.
- **Excluded Files**: `.env*`, `*credentials*`, `*.pem`, `*.key`, `id_rsa*`, `*.db`, `*.sqlite`, `*.sqlite3`, `*.log`, `*.tmp`, `*.zip`.

To regenerate the codebase archives:
```powershell
.\scripts\archive_codebase.ps1 --both
```
- Full Distribution: `phronesis_codebase.zip` (Code, Notebooks & Reference Datasets)
- Slim Distribution: `phronesis_codebase_slim.zip` (Code, Pipelines & Configuration)
