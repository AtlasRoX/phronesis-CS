# Evaluation harness and tech-debt plan (2026-10-17)

Inputs:

- Research on Inspect AI, RAGAS, ALCE, FActScore/SAFE, LAB-Bench, promptfoo, DeepEval and pytest-recording.
- A map of the existing `phronesis/evals` harness.
- Two tech-debt audits: backend; frontend, tests and ops.

The existing harness (`phronesis/evals`, the `phronesis-eval` CLI, the plan/retrieval/answer levels, an LLM judge, compare and gates) is extended, not replaced.

## Phase 1, in parallel and by file ownership

### H. Evaluation harness (`phronesis/evals/**`, `tests/test_evals*.py`, `docs/evals.md`, `.github/workflows/evals.yml`)

1. **Agent modes and run options.** Cases and the CLI pass `framework`, `agent_options`, `dataset_ids` and `project_id` to the orchestrator. Today every case runs the default framework.
2. **Structured golden facts.** `facts: [{id, pattern | value + tolerance, required}]`, with a fact-recall metric (SAFE-style). Numeric matching uses tolerance, for example an HR of 0.30 within ±0.01.
3. **Hard gates.** Each is deterministic and runs offline:
   - every cited PMID/NCT/CHEMBL/NDA appears in the retrieved evidence;
   - no internal names or keys leak into the answer;
   - no crash or schema failure;
   - no hallucinated identifiers.
4. **More metrics:**
   - faithfulness, the verifier's supported-claims ratio (RAGAS/FActScore);
   - the step-failure rate;
   - per-mode breakdown;
   - LLM and tool calls, estimated tokens, latency p50/p95, with budgets.
5. **Golden suite.** `golden.jsonl` holds the verified live questions (KRAS G12C, BRCA2/SOLO-1 in Supervisor, PARP, melanoma adjuvant, KRAS repurposing, EGFR, MYC), each with the facts confirmed in this session.
6. **Regression control:**
   - `phronesis-eval baseline save|check`;
   - paired per-case comparison with a bootstrap 95% CI, failing on a significant drop or a newly failing hard gate;
   - `--repeat N` with mean, SD and a flaky flag.
7. **HTTP transport.** `--via http://host:port` runs answer cases through `/api/chat`, so the API path is evaluated too.
8. **Audit cases.** The free-form HTTP audit cases (`scripts/audit_evaluation_runner.py`) become a harness suite with assertions.
9. **Documentation and CI.** Update docs and the CI path filters. Record/replay cassettes are documented as the next step, not done in this phase.

### R. Backend reliability and security (`phronesis/utils/**`, `tools/registry.py`, `tools/web_search.py`, `agentic/sandbox/**`, `app/backend/**`, `phronesis/phronesis/agents/__init__.py`, typography in the verifier and orchestrator)

- **One shared `fold_typography()`.** The verifier and orchestrator tables disagree on U+2212, U+00AD and U+200A, so flagged claims can survive. Log when a flagged sentence can't be found.
- **Lazy legacy imports.** The nested `phronesis.phronesis.agents` package must not import `main_agent` (hydra/langgraph/Milvus) on every verifier import.
- **Registry:** provenance failures are logged, not swallowed; `execute()` always returns a ToolResult; networked sync tools get a bounded thread pool.
- **biocontext:** remove the sync-on-running-loop deadlock in `utils/biocontext/client.py`.
- **Sandbox:** a path guard on `pandas`/`numpy`/`anndata` file reads.
- **SSRF:** close the DNS-rebinding gap by checking the connected peer IP.
- **API:** rate-limit `/api/chat/title`; return a generic 401 (no JWT error echo); list disabled routers in `/api/health`.
- **Logging:** swallowed exceptions in hot paths get `logger.debug(exc_info=True)`.

### P. Persistence, config and docs (`agentic/jobs.py`, `run_store`, `clinical_memory`, `datasets/store.py`, `tools/reliability.py`, `scripts/`, `README.md`, `mkdocs.yml`, `docs/`)

- **Data root.** One `PHRONESIS_DATA_DIR` for memory, runs, uploads, jobs and caches, defaulting to today's paths for back-compatibility.
- **Retention:** expired tool-cache rows purged; a finished-job TTL; a run-store cap; periodic VACUUM; ad-hoc caches moved to TTLCache.
- **Config reference.** A generated env reference (`scripts/gen_env_reference.py`) producing `docs/configuration.md` and a root `.env.example`, with no secrets.
- **README and nav.** Refresh the README for FastAPI + Next.js, the sources and the harness. Add the missing pages to the mkdocs nav. Declare `httpx` in pyproject.

### F. Frontend (`web/**`)

- **One `apiClient`** for auth, errors and SSE, replacing three copies and nine raw fetches.
- **SSE:** flush the final frame; resume jobs from the last seq; a stall timer.
- **Errors:** `app/error.tsx` plus a boundary around each message.
- **Accessibility:** `aria-live` on the answer, and a focus trap in dialogs.
- **Lint and types:** ESLint (`eslint-config-next`) with a `lint` script; replace the `any`-typed API responses.

### O. Ops and tests (`docker/`, `.github/workflows/*` except evals, `.pre-commit-config.yaml`, `tests/` fakes)

- **Containers.** A backend Dockerfile (FastAPI + uvicorn, healthcheck `/api/health/live`), a web Dockerfile and a compose file. The legacy Streamlit image is not reused.
- **CI:**
  - realistic coverage gates;
  - core lint (ruff on `phronesis`, `app` and `tests`);
  - web lint;
  - `npm audit`;
  - Dependabot for uv and npm.
- **Pre-commit:** large-file and secret-scan hooks.
- **Tests:**
  - consolidate the duplicated fakes into `tests/fakes.py`;
  - rename `test_audit_fixes_live.py`;
  - remove real sleeps;
  - turn hard-dependency `importorskip`s into failures.

## Phase 2, after Phase 1 lands

- **Split the orchestrator.** `_execute_inquiry` is 1,505 lines; split it into stage functions (plan, gather, draft, verify, correct, finalize).
- **One sentence splitter.** Nine copies become `verifier.sentence_spans`.
- **One `SOURCE_REGISTRY`** for source key → public name; there are five maps today.
- **One HTTP helper** for `get_json` with retry, cache and Retry-After.
- **Pydantic `Settings`.** 125 env vars, 73 of them undocumented.

## Needs the owner

- **Initial git commit.** There are no commits, so CI never runs; rename the branch to `main`.
- **Delete `phronesis_codebase.zip` and `_slim.zip`** (427 MB) to free space on A:.
- **Dependency pruning** (cell2sentence, gseapy, …) and moving torch, legacy agents and Streamlit to extras. This changes installs.
- **Legacy Streamlit app**, which has `allow_dangerous_code` pandas agents: keep it, gate it or remove it.

## Status (2026-10-17): Phase 1 done

All five tracks landed:

- **Full suite:** 1335 passed, 6 skipped (it was 1247).
- **Web:** typecheck clean, 64/64 tests, ESLint 0 errors and 189 warnings (existing `any`/React Compiler debt, reported as warnings).

| Track | Result |
|---|---|
| **H harness** | Run options per case (`framework`, `agent_options`, `dataset_ids`, `project_id`). Structured `facts` with numeric, CI and date tolerance (`fact_recall`). Hard gates `no_crash`, `citations_in_evidence` and `no_internal_leak`. Metrics: faithfulness, steps_failed, llm/tool calls, tokens, latency p95, per-framework breakdown, budgets. `golden.jsonl` (7 live cases) and `audit.jsonl` (23 cases). `baseline save/check` with a paired bootstrap CI. `--repeat N` with a flaky flag. `--via` HTTP. Docs and CI updated. Against stored real answers, all required facts matched, and the gates caught real leaks plus unrecorded DGIdb PMIDs. |
| **R reliability/security** | Shared `fold_typography` / `find_folded`. Lazy `main_agent` import (orchestrator import 39–54 s → about 25 s). Registry always returns a ToolResult, logs provenance failures and has a bounded network pool. biocontext deadlock fixed. Sandbox reader path guard (static and runtime). SSRF peer check. Title limiter, generic 401, `disabled_routers` in health, `PHRONESIS_STRICT_ROUTERS`. Debug logging at swallowed exceptions. |
| **P persistence/config/docs** | `PHRONESIS_DATA_DIR` covers memory, runs, uploads, jobs, tool cache, BioModels cache and project memory. Retention: cache purge and VACUUM, a job TTL, a run cap, a scheduled sweep, bounded caches. `scripts/gen_env_reference.py` generates `docs/configuration.md` and `.env.example` (144 variables, secrets blank; `--check` catches drift). README rewritten; mkdocs nav updated; `httpx` declared. |
| **F frontend** | One `apiClient` with a single SSE reader (final-frame flush, CRLF, stall timer, job resume from the last seq). `error.tsx`, `global-error.tsx` and per-message boundaries. `role=log`, aria-live and a focus trap. Typed API responses. ESLint. Token in sessionStorage with "Remember on this device". |
| **O ops/tests** | `docker/backend.Dockerfile`, `web.Dockerfile` and `compose.yml`. Coverage gate 76 % (measured 78 %). Blocking ruff checks for syntax errors and undefined names. Realistic per-module gates. Web lint plus a production `npm audit`. pip-audit gate. Dependabot for uv, npm and actions. Pre-commit large-file and secret hooks. Shared fakes. Shorter sleeps. |

Integration fixes:

- **DGIdb** now records its PMIDs and ChEMBL ids as canonical ids.
- **Internal step keys** (`agent.gaps.9.lithoscribe`) are removed from answers.
- **The BioModels pin and project memory** follow the data root.

**Still with the owner:**

- the initial commit and the `main` branch;
- deleting the 427 MB of zips;
- **`uv.lock` has 348 known vulnerabilities, 333 of them fixable**, so the pip-audit gate fails until the lock is upgraded;
- moving heavy dependencies to extras (the image is several GB);
- making `phronesis/__init__.py` and `phronesis/phronesis/__init__.py` lazy (blocked as out of scope; it would cut startup further);
- the legacy Streamlit `allow_dangerous_code` tools;
- committing a trusted golden baseline (`phronesis/evals/baselines/golden.json`).

**Phase 2 (not started):** split the orchestrator's `_execute_inquiry`; one sentence splitter, one `SOURCE_REGISTRY`, one HTTP helper and pydantic `Settings`; record/replay cassettes for a deterministic offline answer suite.

## Phase 2 (started 2026-10-17)

The five tracks are split by file ownership:

- **A. Orchestrator split** (`phronesis/agents/authoritative_orchestrator.py`, new `phronesis/agents/orchestration/`).
  - `_execute_inquiry` becomes stage functions sharing one run context: plan, gather, draft, agent rounds, verify/correct, finalize.
  - No behaviour change. The full suite and one live golden case act as the guard.
  - The orchestrator's source-name map moves to B's `SOURCE_REGISTRY` once B lands.
- **B. One of each** (sentence-splitter call sites; source-name maps outside the orchestrator).
  - `sentence_spans` moves to `phronesis/utils/text.py` (the verifier re-exports it) and replaces nine local splitters wherever the semantics match.
  - `phronesis/sources/names.py` `SOURCE_REGISTRY` covers key, public name, internal component and agent label, replacing the maps in `approval.py`, `drugintel/sources.py`, `variants/render.py` and `export/report_model.py`.
- **C. One HTTP helper** (new `phronesis/utils/http.py`; the API clients).
  - `get_json` covers retries with backoff, Retry-After, typed errors (timeout/http/transport/bad_body), an optional TTL cache and a rate limiter.
  - It is adopted by `chembl_client`, `openfda_client`, `pubtator`, `sources_extra/common`, `structintel/clients`, `variants/clients` and `datasets/geo`, wherever behaviour stays identical.
- **D. Record/replay** (`phronesis/evals/cassette.py`; `tests/`; CI).
  - Registry tool calls and model calls are recorded per case and replayed, giving a deterministic offline `--replay` answer suite on every commit.
  - Recording is explicit (`--record`). Secrets are filtered.
- **E. Settings** (new `phronesis/config/settings.py`; `scripts/gen_env_reference.py`; app startup).
  - A declarative registry for all environment variables (name, type, default, description, secret).
  - Typed accessors, and startup validation that warns on invalid values.
  - The env docs are generated from the registry, and a test checks that every `getenv` in the code is registered.

### Status (2026-10-17): Phase 2 done

- **A. Orchestrator split.**
  - `_execute_inquiry` is now a 73-line coordinator. The work happens in `phronesis/agents/orchestration/`, which holds `RunContext` and the plan, gather, draft, agent_rounds, verify and finalize modules. `authoritative_orchestrator.py` went from 2,389 to 960 lines.
  - Before and after every extraction, 15 offline scenarios gave byte-identical event and result traces.
  - Patch points on the module still work.
- **B. One of each.**
  - `sentence_spans` now lives in `phronesis/utils/text.py`. Four splitters were replaced. Three were kept on purpose, each with a comment saying why: scoring refusal, clinical-memory `;`, and drugintel, which already used the shared one.
  - `phronesis/sources/names.py` holds `SOURCE_REGISTRY`, with 32 sources. It feeds `approval`, `drugintel/sources`, `variants/render`, `report_model` and the orchestrator's `_INTERNAL_SOURCE_NAMES`, and every string is unchanged.
- **C. One HTTP helper.**
  - `phronesis/utils/http.py` provides `get_json`/`post_json`/`request`, `HttpError(kind)` and `retry_after_seconds`.
  - It is adopted by chembl, openfda, sources_extra/common, pubtator, structintel `_request`, variants (ClinVar/CIViC/cBioPortal) and geo. gnomAD and the budgeted ClinicalTrials.gov search were kept as they were; the latter only switched to the shared Retry-After parser.
- **D. Record/replay.**
  - `phronesis/evals/cassette.py` records tool, model and provenance-probe channels, with secret redaction and normalisation of volatile values. Strict replay refuses network access.
  - New CLI flags: `--record`, `--replay` and `--replay-lenient`.
  - CI now replays committed cassettes on every push and PR.
  - `golden-parp-hrd` is recorded (353 KB) and replays deterministically in about 3 s offline.
- **E. Settings.**
  - `phronesis/config/settings.py` registers 146 variables with type, default, bounds, group and secret flag. Typed accessors never raise.
  - At startup, `validate_environment()` logs warnings, and `PHRONESIS_STRICT_CONFIG=1` turns them into errors. `/api/health` reports `config_problems`.
  - The env docs are generated from the registry. They list file paths only, so `--check` no longer fails when lines shift.
- **Integration.** Full suite: 1402 passed, 6 skipped, plus the env test fixed afterwards. Golden replay: pass.
