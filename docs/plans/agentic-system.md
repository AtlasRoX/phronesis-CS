# Phronesis agentic system: design, contracts and work packages

Features 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19 and 20 from the agentic feature list, plus
three things they need: a run budget, run control, and prompt-injection guardrails. The verified references
are in `docs/agentic/references-agent-loop.md` and `docs/agentic/references-verification-tools-data.md`.
Each feature below cites the papers it implements.

## 0. Ground rules (verified in code)

- The classic pipeline (`framework="authoritative-react-compiler"`, the default of `execute_inquiry`) runs **no**
  agentic phase. The 619 existing tests must keep passing unchanged.
- The API default is `PHRONESIS_DEFAULT_FRAMEWORK`, or `agentic-plan-execute` if that is unset (`app/backend/main.py:_request_framework`).
- Each file has exactly one owner (section 4). Shared files are frozen: `authoritative_orchestrator.py`,
  `prompts.py`, `verifier.py`, `registry.py` (except the reliability hook), `main.py`, `agentic/{config,budget,llm,control,
  state,runtime,evidence,guardrails}.py`, `tests/agentic_fakes.py`, `tests/fakes.py`. If you need a change there,
  describe it in your final report rather than making it.
- Disk: about 400 MB free on A:, so **install no new packages**. Optional heavy models (NLI cross-encoders, sentence
  embedders) load only from the local HF cache (`local_files_only=True`), are never downloaded at request time, and
  every feature degrades to a deterministic path without them.
- Offline tests: no network and no real model. Use `tests/agentic_fakes.py` (`make_agentic_orchestrator`,
  `ScriptedLLM`) and `tests/fakes.py` (`build_recording_registry`, `extra_tools`). HTTP clients get fake
  transports or monkeypatched functions. Fakes return trimmed real responses, never invented biology.
- **Prompt marker contract:** every agentic phase's system prompt starts with `[phase:<key>]`, so
  `ScriptedLLM` can route by phase. Synthesis calls carry no marker.
- Run tests with `.venv/Scripts/python.exe -m pytest <files> -q -p no:cacheprovider`.

## 1. Runtime (built by the foundation; read these files)

`phronesis/agentic/`
- `config.py`: framework ids `CLASSIC`, `AGENTIC`, `SUPERVISOR`, `SWARM`, `DEBATE`; `is_agentic`; `env_flag`;
  `agent_model`.
- `budget.py`: `RunBudget` covering tool calls, model calls, estimated tokens, seconds, draft rounds, plan nodes and
  replans (FrugalGPT-style cost control, Chen et al. 2023, arXiv:2305.05176; budget-awareness after BATS,
  arXiv:2511.17006). `charge_tool` and `charge_llm` raise `BudgetExhausted`.
- `llm.py`: `AgentLLM.complete(system, user, phase=)` and `.json(system, user, schema=, phase=)`, with budget
  charging, `<think>` stripping and JSON extraction with one repair retry. `.available` is False offline.
- `control.py`: `RunControl` (`request_approval`, `resolve`, `steer`, `drain_steering`, `cancel`, `checkpoint`) and the
  per-user registry `create_control`/`get_control`. Non-interactive runs auto-approve.
- `state.py`: `AgentState` (one per run) and `SubQuestion`.
  - `await state.call_tool(name, phase=, purpose=, **kwargs)` charges the budget, executes, stores the result under
    `agent.<phase>.<n>.<tool>` and labels it.
  - `state.add_result(key, result, phase=, purpose=, render=)` stores a result the phase built itself.
  - Also: `state.step(id, title, text, status, agent)`, `state.evidence_sections` (raw XML the phase adds to the
    evidence block), `state.brief_notes`, `state.scope_notes`, `state.artifacts` (merged into
    `execution_artifacts`), `state.bag` (shared scratch), `state.sub_questions`, `state.draft`, `state.verification`.
- `runtime.py`: `AgentPhase` has `key`, `stage`, `order`, `frameworks`, `needs_llm`, `applies(state)` and
  `async run(state) -> PhaseOutcome | None`. Register phases with `register_phase()` when your module is imported.
  Stages:
  1. `before_evidence`: after parsing and memory recall, before the scope gate. Can stop the turn with
     `stop_response`.
  2. `approve`: after source routing, before Stage 1. May switch sources off through `ctx.decisions[...]`.
  3. `augment`: after the pipeline's Stage 1 and 2 results exist.
  4. `after_draft`: after each draft. Return `PhaseOutcome(resynthesize=True, feedback=[...])` to have the
     orchestrator rewrite the answer. The loop is bounded by `budget.max_draft_rounds`.
  5. `finalize`: after verification and the citation map. `state.verification` and `state.bag["citation_map"]` are set.
- `evidence.py`: renders agentic results in `<agent_evidence>`. Passages render as citable `<document>` elements and web text is spotlighted.
- `guardrails.py`: `screen`, `neutralise`, `spotlight` and `SPOTLIGHT_RULE`, following indirect prompt injection
  (Greshake et al. 2023, arXiv:2302.12173) and Spotlighting (Hines et al. 2024, arXiv:2403.14720). Use these on
  every piece of untrusted text you put in a planner, critic or sandbox prompt.
- `tool_retrieval.py`: `retrieve_tools(registry, query, k, domains, exclude, always)` and `tool_card(entry)`
  (feature 18, below).

Orchestrator integration (already done): `execute_inquiry(..., framework, project_id, agent_options,
run_control, interactive)`.
- The first SSE event of an agentic run is `{"type":"run_control","run_id","framework","interactive"}`.
- `state.bag["entailment_judge"]` (set by the NLI phase) is passed to `verifier.verify_response(entailment_judge=...)`.
- `execution_artifacts["agent_run"] = state.summary()`.
- A cancelled run returns `task_status.task_status == "cancelled"`.

`agent_options` (from `ChatRequest.agent_options`): `require_plan_approval`, `allow_clarification`,
`enable_counter_evidence`, `enable_hypotheses`, `enable_code_analysis`, `budget{max_*}`.

## 2. Features, techniques and citations

### F1 Plan-and-execute with a tool DAG (`agentic/plan_graph.py`, `planner.py`, `executor.py`, `plan_execute.py`)
- **LLMCompiler** (Kim et al., ICML 2024, arXiv:2312.04511) is the core.
  - The planner returns numbered tasks `{id, tool, args, depends_on, purpose}`. Args may reference earlier
    outputs as `"$n1.path.to.field"`.
  - A task-fetching unit dispatches each node as soon as its dependencies resolve; nodes run in parallel through
    `state.call_tool`.
  - A joiner decides `finish` or `replan`, with at most `budget.max_replans` replans.
- **ReWOO** (arXiv:2305.18323): plan once up front, which is the default when no replan is needed.
- **Plan-and-Solve** (ACL 2023, arXiv:2305.04091): the planning trigger in the system prompt.
- **ReAct** (ICLR 2023, arXiv:2210.03629): the fallback single-step mode.
- Validation before anything runs:
  - The tool must be registered.
  - Args are checked against the tool's input schema.
  - Hard policy:
    - Never a simulation tool when `ctx.suppress_simulation` is set or a simulation is forbidden.
    - Respect `ctx.source_allowlist` (papers-only, trials-only).
    - Never re-run an identical call the pipeline already made.
    - Cap the plan at `max_plan_nodes`.
  - Cycles are rejected.
- The planner sees only `retrieve_tools` cards, plus a summary of which pipeline results exist and what they found.
- Artifact `agent_plan` (section 3). Steps: one `agent_plan` step plus one step per node (`agent_node_<id>`).
- Frameworks: AGENTIC, DEBATE.

### F3 Question decomposition (`agentic/decompose.py`, stage before_evidence)
- **Least-to-Most** (Zhou et al., ICLR 2023, arXiv:2205.10625): ordered sub-questions, earlier answers feed later ones.
- **Self-Ask** (Findings EMNLP 2023, arXiv:2210.03350): explicit follow-up questions that can each be routed to search.
- **Decomposed Prompting** (ICLR 2023, arXiv:2210.02406): each sub-question gets a `focus` that routes it to a specialist handler.
- **Adaptive-RAG** (NAACL 2024, arXiv:2403.14403): a complexity check. Simple questions are not decomposed.
- Deterministic fallback when no model is available: split on question marks and on enumerations or conjunctions
  of distinct topics.
- At most 5 sub-questions. Writes `state.sub_questions`. The planner (F1), gaps (F2) and confidence (F9) use them.

### F2 Gap-driven re-retrieval (`agentic/gaps.py`, stage after_draft)
- **Self-RAG** (ICLR 2024, arXiv:2310.11511): IsSup labels {fully, partially, no support}. "No support" is a gap.
- **FLARE** (EMNLP 2023, arXiv:2305.06983): the draft's unsupported or hedged sentences become retrieval queries.
- **CRAG** (arXiv:2401.15884): grade retrieval as correct, ambiguous or incorrect. Incorrect or ambiguous means
  re-query with rewritten terms.
- **IRCoT** (ACL 2023, arXiv:2212.10509): interleave retrieval with the reasoning steps.
- How it works:
  1. Per sub-question, check coverage deterministically: does an evidence document mention its key terms?
  2. Optionally ask the model to label each sub-question answered, partial or missing, and to propose up to 3
     follow-up queries.
  3. Run `lithoscribe_literature_synthesis`, and `websurfer_search` if web search is on, for each gap.
  4. `resynthesize=True` only when new successful evidence arrived.
- Stops when there are no gaps, the budget is used up, or after one gap round per draft round. Each sub-question's
  `status` is updated.

### F4 Multi-agent topologies (`agentic/topologies.py`, plus `agentic/specialists.py`)
Specialists are tool groups built from registry domains:
- Literature: lithoscribe, trials, web
- Target: synapta, disease_targets, primekg
- Simulation: vivarix
- Omics: lumicell, omics sources
- Chemistry: drugs, structure

Topologies:
- **SUPERVISOR** (augment): a supervisor loop modelled on **Magentic-One** (arXiv:2411.04468).
  - A task ledger (facts given, to look up, to derive; the plan).
  - A progress ledger asking: done? looping? progressing? next specialist? instruction?
  - Stall counter above 2: reflect and replan.
  - Each specialist runs a short ReAct loop (at most 3 tool calls) over its own tools only.
- **SWARM** (augment): handoffs. The active specialist either calls one of its tools or hands off to a named
  specialist with a note. A handoff budget applies and the same pair may not hand back and forth.
  **AutoGen** conversable agents (COLM 2024, arXiv:2308.08155).
- **DEBATE** (after_draft): **multi-agent debate**, Du et al. (ICML 2024, arXiv:2305.14325).
  - Two or three critic personas (methodologist, clinical skeptic, mechanistic biologist) critique the draft
    against the evidence.
  - Round two gets the other critics' points: "Using the opinion of other agents as additional advice...".
  - Consensus feedback goes back through `resynthesize=True, feedback=[...]`.
  - **MetaGPT** SOP-style role prompts (ICLR 2024, arXiv:2308.00352).
  - **CRITIC** (ICLR 2024, arXiv:2305.11738) and **Chain-of-Verification** (Findings ACL 2024, arXiv:2309.11495):
    critiques must name a claim and the evidence key behind it.
- The frontend shows all four frameworks as selectable.

### F7 Counter-evidence search (`agentic/counter_evidence.py`, stage augment)
- **PaperQA2 / ContraCrow** (Skarlinski et al. 2024, arXiv:2409.13740):
  - Extract claims from the gathered evidence.
  - Search for contradicting evidence.
  - Rate on an 11-point Likert scale (0 = strongly supports, 5 = lack of evidence, 10 = strongly contradicts).
  - Flag a contradiction at 8 or higher.
- **SciFact / SciFact-Open** (EMNLP 2020, arXiv:2004.14974; Findings EMNLP 2022, arXiv:2210.13777): the SUPPORTS,
  REFUTES or NOINFO labels.
- Queries are built deterministically: "<entity> resistance", "<entity> no effect", "<entity> failed trial",
  "<entity> contradicting", and "<drug> adverse".
- Run lithoscribe and keep passages not already present. Classification uses the NLI judge (F8) when available,
  otherwise the model with the Likert rubric, otherwise a cue-word heuristic labelled `heuristic`.
- Adds an `<evidence_conflicts>` section to the evidence and a brief note: "Discuss these conflicts explicitly".
- Artifact `evidence_conflicts`. Off when `agent_options.enable_counter_evidence` is false, and off under
  papers-only restrictions that exclude lithoscribe.

### F6 Hypothesis to in-silico experiment loop (`agentic/hypothesis.py`, stage augment)
- **AI co-scientist** (Gottweis et al. 2025, arXiv:2502.18864):
  - Generate, then reflect, then rank with an Elo tournament (start at 1200, K=32, pairwise model judge or
    deterministic comparison on effect size and evidence).
  - Evolution: refine the top hypothesis once.
- **BioDiscoveryAgent** (ICLR 2025, arXiv:2405.17631): reflection, then research plan, then a solution batch, with
  an exclusion list of what has already been tested.
- **Robin** (arXiv:2505.13400): rank with a pairwise judge (Bradley-Terry). Elo is acceptable here.
- When a simulation model is in play (`ctx.biomodel_id`, an uploaded SBML model, or the Vivarix result's
  `model_id`):
  - Propose 2 to 4 perturbation hypotheses: knockdown is ×0.1 of a parameter, overexpression ×10, inhibition of a
    rate constant.
  - Run `vivarix_parameter_scan` (2 points: baseline and perturbed) or `vivarix_simulate_time_course` with
    `parameter_overrides`.
  - Compute log2 fold-change of the readout species endpoint.
  - Support means the effect has the predicted direction with |log2FC| ≥ 0.58.
- Without a model, rank the literature- and target-evidence hypotheses only (no fake experiments) and say so.
- Never change `ctx.suppress_simulation`: no simulations when it is set.
- Artifact `hypotheses`.

### F8 Semantic claim verification (`agentic/nli.py`, stage augment, sets `state.bag["entailment_judge"]`)
- **SciFact** label scheme. **SummaC-ZS** (TACL 2022, arXiv:2111.09525): for each claim, take the maximum
  entailment over evidence chunks.
- **FActScore** (EMNLP 2023, arXiv:2305.14251): atomic claims and a supported fraction.
- **MiniCheck** (EMNLP 2024, arXiv:2404.10774): chunk the document and take the maximum score.
- **AlignScore** (ACL 2023, arXiv:2305.16739): the same alignment idea.
- Backends, in order (`PHRONESIS_NLI=off|auto|model|llm`):
  1. A local cross-encoder from the HF cache, `cross-encoder/nli-deberta-v3-base` (labels 0=contradiction,
     1=entailment, 2=neutral; hard-coded), loaded with `local_files_only=True`.
  2. A model judge with `[phase:nli]` and SciFact labels.
  3. None. The judge is then not installed and the verifier keeps its deterministic audit.
- The judge signature is fixed by the verifier: `judge(claim_text, [{"source_key","passage"}]) ->
  {"status": "supported"|"contradicted"|"unsupported", "reason"}` or None. It may only downgrade a verdict.
- The phase also keeps `state.bag["nli_scores"][claim_text] = {"entailment","contradiction","neutral","backend"}`
  for F9.
- Model-judge calls are cached per (claim, passage hash) and capped at 12 per run.

### F9 Calibrated per-claim confidence (`agentic/confidence.py`, stage finalize)
- Per-claim features:
  - NLI entailment probability (F8)
  - Number of independent supporting sources
  - Best evidence grade (`grade_label` from evidence grading)
  - Contradiction (F7, F8)
  - Verifier defects on that claim
  - A hedge in the sentence
- Confidence = logistic(w·x + b) with documented default weights, then **temperature scaling**
  (Guo et al., ICML 2017, arXiv:1706.04599). T is fitted by `fit_temperature(scores, labels)` (minimise NLL) and
  stored in `PHRONESIS_CONFIDENCE_T` or a JSON file.
- `ece(scores, labels, bins=10)` measures calibration.
- Optional split-conformal back-off threshold (**Mohri & Hashimoto**, ICML 2024, arXiv:2402.10978):
  `conformal_threshold(cal_scores, cal_labels, alpha)`. Claims below the threshold are marked `low`.
- Stated-confidence elicitation is optional: **Kadavath et al. 2022** (arXiv:2207.05221) and **Xiong et al.**
  (ICLR 2024, arXiv:2306.13063). Used only if the model is available, and blended at a 0.2 weight.
- Semantic entropy (**Farquhar et al.**, Nature 2024, doi:10.1038/s41586-024-07421-0) is documented as future work
  because it needs N samples.
- Claims come from the verifier report's `claims`, else from the answer's sentences.
- Artifact `claim_confidence`.

### F10 Sandboxed code analysis (`agentic/sandbox/{policy,runner}.py`, `agentic/analyst.py`, `extensions/sandbox.py`)
- **CodeAct** (ICML 2024, arXiv:2402.01030): Python code is the action. Write, run, observe, fix, up to 4 iterations.
- **Biomni** (bioRxiv 2025, doi:10.1101/2025.05.30.656746): plan first, then code.
- **BixBench** (arXiv:2503.00096): notebook-style analysis of real datasets, with an "insufficient information"
  answer allowed.
- **Data Interpreter** (arXiv:2402.18679): verify each step.
- Isolation, layered:
  1. An AST policy: an import allowlist (numpy, pandas, scipy, statsmodels if installed, sklearn, matplotlib,
     anndata, scanpy, math, statistics, json, re, collections, itertools). Banned names and attributes: `os`,
     `sys`, `subprocess`, `socket`, `open` outside the work dir, `eval`, `exec`, `__import__`, `importlib`,
     `ctypes`, `shutil`, `pathlib` writes outside the work dir, dunder attribute access.
  2. A separate `python -I` subprocess in a fresh temp work dir, holding only copies of the attached files.
  3. Wall-clock timeout (default 60 s), output size caps, and a memory limit where the OS supports it (resource on
     POSIX; a Windows Job Object via ctypes in the parent is optional).
  4. Environment scrubbed of every secret (API keys, tokens).
  5. Network blocked inside the child by monkeypatching `socket` before user code runs.
- This is documented honestly as defence in depth. Production should use a container (see the docs).
- Enabled only when `PHRONESIS_SANDBOX=on` (default off) and `agent_options.enable_code_analysis`.
- The registry tool `analysis_run_python` (domain `analysis`) and the analyst phase run when a CSV/TSV/h5ad
  dataset is attached and the question asks for analysis.
- Figures are saved as PNG and returned base64 (max 3).
- Artifact `sandbox_analysis`.

### F11 SBML model calibration (`vivarix/calibration.py`, `agentic/calibrate.py`, `extensions/calibration.py`)
- **COPASI** (Bioinformatics 2006, doi:10.1093/bioinformatics/btl485) and **basico** (JOSS 2023,
  doi:10.21105/joss.05553):
  - `load_model`/`load_biomodel` and `run_time_course` for simulation.
  - `basico.task_parameterestimation` (`add_experiment`, `set_fit_parameters`, `run_parameter_estimation`) is an
    optional backend.
- The default backend is `scipy.optimize.least_squares` over log-parameters, with basico time-course simulations
  and bounded iterations.
- Identifiability by **profile likelihood** (Raue et al., Bioinformatics 2009, doi:10.1093/bioinformatics/btp358):
  - Δχ² threshold 3.84 (95%, df=1).
  - A flat profile means structurally non-identifiable; one open side means practically non-identifiable.
  - At most 3 parameters, 7 profile points each.
- **pyPESTO** (doi:10.1093/bioinformatics/btad711) and **Hass et al. 2019** benchmarks
  (doi:10.1093/bioinformatics/btz020) are the references for multistart and test problems.
- Input: an attached CSV with a `time` column plus species columns, a model (BIOMD id or uploaded SBML), and
  parameter names (from the question, else the model's top sensitivity parameters, else the first k parameters
  that match species named in the question).
- Registry tool `vivarix_calibrate_parameters` (LOCAL_COMPUTE, timeout 300 s). Phase runs on calibration intent
  (calibrate, fit, estimate parameters) with data and a model.
- Artifact `model_calibration`.

### F12 Structure and chemistry (`structintel/*`, `extensions/structure.py`, an EvidenceSource)
- **AlphaFold DB** (Varadi et al., NAR 2022/2024, doi:10.1093/nar/gkab1061, doi:10.1093/nar/gkad1011):
  `GET https://alphafold.ebi.ac.uk/api/prediction/{UniProt}`. Report the model id and mean pLDDT with the bands
  from **AlphaFold** (Jumper et al., Nature 2021, doi:10.1038/s41586-021-03819-2).
- **RCSB PDB** (Burley et al., NAR 2023, doi:10.1093/nar/gkac1077): search
  `https://search.rcsb.org/rcsbsearch/v2/query` by UniProt accession, and entries via
  `https://data.rcsb.org/rest/v1/core/entry/{id}` (method, resolution, ligands).
- Optional NVIDIA NIM, only with `NVIDIA_API_KEY`:
  - **DiffDock** (ICLR 2023, arXiv:2210.01776) at `POST https://health.api.nvidia.com/v1/biology/mit/diffdock`
    with fields `protein`, `ligand`, `ligand_file_type`, `num_poses`.
  - **MolMIM** (arXiv:2208.09016) at `POST .../v1/biology/nvidia/molmim/generate` with fields `smi`, `algorithm`,
    `num_molecules`, `property_name`, `min_similarity`.
  - **ESMFold** (Science 2023, doi:10.1126/science.ade2574): the body field is unverified, so it is not wired by
    default.
- Docking and generation run only on explicit request ("dock", "generate analogs") and never by default.
- The UniProt accession comes from the resolved entities (`ctx` primary gene) or the UniProt BioContext tool if
  registered.
- Artifact `structure_intel`. Identifiers: AF model ids and PDB ids go into `allowed_identifiers` under a new id
  family if needed (via `extensions/id_families.py` registration API, if one exists; otherwise
  `allowed_identifiers` returns `{"pdb": ...}` and the source's `citations`).

### F20 New data sources (`sources_extra/*`, `extensions/omics_sources.py`, EvidenceSources)
Each source is one module with a client (httpx, `phronesis/utils/http_limits.py` if useful), an EvidenceSource
with `should_run`, `run`, `evidence_xml`, `citations`, `artifact`, `allowed_identifiers` and `scope_notes`, and
fixtures.
- **GWAS Catalog** (Sollis et al., NAR 2023, doi:10.1093/nar/gkac1010; Cerezo et al., NAR 2025):
  **v2 only**, `https://www.ebi.ac.uk/gwas/rest/api/v2`. v1 returns 410. Runs on genetics or trait intent with a
  gene or disease.
- **GTEx** (Science 2020, doi:10.1126/science.aaz1776): `https://gtexportal.org/api/v2/`, median gene expression
  per tissue. Runs on expression or tissue intent with a gene.
- **STRING** (Szklarczyk et al., NAR 2023/2025, doi:10.1093/nar/gkac1000, doi:10.1093/nar/gkae1113): pinned
  `https://version-12-5.string-db.org/api/json/network` and `/interaction_partners`, species 9606. Runs on
  interaction or network intent with a gene.
- **Reactome** (Milacic et al., NAR 2024, doi:10.1093/nar/gkad1025): `https://reactome.org/ContentService/`,
  pathways for a UniProt or gene. Runs on pathway or mechanism intent.
- **PubChem** (Kim et al., NAR 2025, doi:10.1093/nar/gkae1059): PUG-REST compound properties for drugs. Accept
  both `ConnectivitySMILES` and `CanonicalSMILES`.
- **bioRxiv/medRxiv**: `https://api.biorxiv.org/details/...`, recent preprints for a topic (filter by
  title/abstract term match). Label them "not peer reviewed".
- **DepMap** (Tsherniak et al., Cell 2017, doi:10.1016/j.cell.2017.06.010): the portal API blocks scripted access.
  Implement a local-file connector over a user-provided `CRISPRGeneEffect.csv` (env `PHRONESIS_DEPMAP_DIR`). It is
  skipped with a scope note when no file is configured.
- **DrugBank** is licence-restricted and is not implemented (documented).
- Artifact key: `omics_intel`, shaped as `{<source>: {...}}`, one sub-object per source.

### F19 Reliability (`tools/reliability.py`, hooked into `registry.execute`)
- **Circuit breaker** (Fowler 2014, after Nygard, *Release It!*): closed, then open after N consecutive upstream
  failures (default 5), then half-open after a cool-down (default 60 s), allowing one trial call.
- Retries with exponential backoff and full jitter, only for TIMEOUT and UPSTREAM_API_ERROR, only for idempotent
  REST and GraphQL adapters, never LOCAL_COMPUTE. Default 2 retries.
- Result cache:
  - In memory, plus an optional SQLite store at `PHRONESIS_TOOL_CACHE_DIR` (default under the runs dir), with TTL
    per adapter (REST 6 h).
  - Key = tool name plus the sha256 of canonical JSON of the validated kwargs.
  - Only successful results, never LOCAL_COMPUTE or uploads, and never when `PHRONESIS_TOOL_CACHE=off`.
  - Cache hits set `metadata["cache"]="hit"`.
- Fallback map: when a tool's breaker is open, try its registered fallback (e.g. `websurfer_search` for
  literature). This is optional and listed in `metadata["fallback_from"]`.
- The failure taxonomy from **API-Bank** (EMNLP 2023, arXiv:2304.08244) labels telemetry. Fault-injection tests are
  in the spirit of **ToolEmu** (ICLR 2024, arXiv:2309.15817) and **ToolSword** (ACL 2024, arXiv:2402.10753).
- `GET` stats are exposed through `reliability_snapshot()`, which the platform's health or agent route can serve.
- Must keep `record_tool_call` behaviour: one recorded call per `execute`.

### F18 Tool retrieval (`agentic/tool_retrieval.py`, foundation, owned by WP1 for improvements)
- **ToolLLM** (ICLR 2024, arXiv:2307.16789) API retriever.
- **Gorilla** (NeurIPS 2024, arXiv:2305.15334): retrieval-grounded docs (`tool_card`).
- **ToolUniverse** (arXiv:2509.23426) tool finder.
- **Re-Invoke** (Findings EMNLP 2024, arXiv:2408.01875): synthetic intent expansion.
- **Toolshed** (arXiv:2410.14594).
- **ToolRet** (ACL 2025, arXiv:2503.01763): measure recall@k.

### F13 Background jobs (`agentic/jobs.py`, `app/backend/routers/jobs.py`)
- SQLite job store (`PHRONESIS_JOBS_DIR`): job id, user, status (queued, running, waiting_approval, succeeded,
  failed, cancelled, interrupted), request, result, created/updated.
- Every event is logged in order (seq), so a client can reattach with `GET /api/jobs/{id}/events?after=N` (SSE,
  replays then follows).
- An asyncio worker pool runs jobs (`PHRONESIS_JOB_WORKERS`, default 2), each with its own `RunControl`, which is
  interactive when someone is watching.
- On startup, jobs left `running` are marked `interrupted` and can be resumed with `POST /api/jobs/{id}/resume`. A
  resumed job re-executes; tool calls replay from the F19 cache, so completed upstream work is not repeated.
- Durable-execution reference: LangGraph persistence docs
  (https://docs.langchain.com/oss/python/langgraph/persistence). No peer-reviewed paper was verified.
- Routes:
  - `POST /api/jobs` (body = ChatRequest fields) returns `{job_id, run_id}`.
  - `GET /api/jobs`, `GET /api/jobs/{id}`, `GET /api/jobs/{id}/events?after=`.
  - `POST /api/jobs/{id}/cancel`, `POST /api/jobs/{id}/resume`, `DELETE /api/jobs/{id}`.
- All are user-scoped.

### F15 Human in the loop (`agentic/clarify.py`, `agentic/approval.py`, `app/backend/routers/agent_runs.py`)
- **Clarify** (before_evidence):
  - **CLAM** (arXiv:2212.07769): detect ambiguity, then ask.
  - **Clarify When Necessary** (Findings NAACL 2025, arXiv:2311.09469): ask only when the expected ambiguity is
    high.
  - **Aliannejadi et al.** (SIGIR 2019, arXiv:1907.06554).
  - Deterministic ambiguity signals:
    - The scope is `unclear`.
    - An ambiguous gene family (`ctx.genes` with `ambiguous_with`).
    - Simulation intent with no model and no searchable term.
    - Several attached datasets with no way to tell which one to use.
  - An optional model check, `[phase:clarify]`, returns `{ambiguous, question, options[]}`.
  - Asks at most once per thread turn. Does not ask if the previous turn already asked
    (`state.bag`/memory thread history), or when `allow_clarification` is false.
  - The stop response is the question plus options. Artifact `clarification`.
- **Plan approval** (approve):
  - **Magentic-UI** (arXiv:2507.22358) co-planning and action guards.
  - Runs when `require_plan_approval` is set, or when the planned work includes a simulation, parameter scan,
    calibration, docking or sandbox run (expensive) and the run is interactive.
  - Sends `{"type":"approval_request","run_id","approval_id","kind":"plan","payload":{"sources":[{key,label,run,
    reason}]}}` through `state.send`, then waits on `control.request_approval`.
  - An edited payload switches sources off through `ctx.decisions[key] = SourceDecision(run=False,
    reason="switched off by the researcher")`. A rejected plan switches every optional source off except
    literature.
  - Artifact `plan_approval`.
  - The F1 plan DAG also asks for approval (kind `"dag"`) before running nodes that call expensive tools, using the
    same mechanism.
- **Router** `/api/agent/runs/{run_id}`:
  - `GET` returns `control.to_dict()`.
  - `POST /approve {approval_id, approved, payload?, message?}`.
  - `POST /steer {message}`. Steering is read before synthesis and after each draft; a steer during the draft loop
    triggers one rewrite.
  - `POST /cancel`.
  - All user-scoped.

### F16 Project memory (`utils/memory/project_store.py`, `agentic/project_memory.py`, `app/backend/routers/projects.py`)
- **Zep/Graphiti** (arXiv:2501.13956): a bi-temporal fact graph. `fact(subject, relation, object, valid_from,
  invalid_at, recorded_at, source, confidence)`. A contradicting new fact invalidates the old one instead of
  deleting it.
- **Mem0** (arXiv:2504.19413): extract, then ADD, UPDATE, INVALIDATE or NOOP.
- **HippoRAG** (NeurIPS 2024, arXiv:2405.14831): Personalized PageRank over the fact graph, seeded with the query
  entities, for retrieval.
- **Generative Agents** (UIST 2023, arXiv:2304.03442): recency × importance × relevance scoring.
- **MemGPT** (arXiv:2310.08560): the working-memory and archival split.
- **A-MEM** (NeurIPS 2025, arXiv:2502.12110): linked notes.
- Facts are extracted deterministically from verified tool results:
  - gene–disease association scores
  - drug–target and mechanism (drug_intel)
  - trial (NCT) to condition
  - BioModel to topic
  - variant to classification
  - hypothesis outcomes
  - user preferences from `classify_instructions`
- Facts come only from turns where verification passed, or are tagged `unverified`.
- The recall phase (before_evidence, only with `project_id`) writes `state.project_memory` (a text block) and the
  artifact `project_memory`.
- The consolidate phase runs at finalize.
- Routes (user-scoped):
  - `POST /api/projects {name, description}` and `GET /api/projects`.
  - `GET/PATCH/DELETE /api/projects/{id}`.
  - `GET /api/projects/{id}/memory?query=`.
  - `PATCH /api/projects/{id}/memory/{fact_id}` (invalidate or edit) and `DELETE` the same path.
  - Project ids match `prj_[A-Za-z0-9]{6,40}`.

### F17 MCP server and A2A (`app/backend/routers/mcp.py`, `app/backend/routers/a2a.py`)
- **MCP** (spec 2025-11-25 with the initialize handshake, plus 2026-07-28 `server/discover` and
  `MCP-Protocol-Version` header negotiation): Streamable HTTP, JSON-RPC 2.0 over `POST /mcp`.
  - Methods: `initialize`, `notifications/initialized`, `ping`, `server/discover`, `tools/list`, `tools/call`.
  - Tools:
    - `phronesis_research` (full orchestrator, framework argument)
    - an allowlist of read-only registry tools (`PHRONESIS_MCP_TOOLS`, default: OpenTargets search/associations,
      KEGG, literature, trials, ChEMBL resolve, structure, omics), with `inputSchema` from the pydantic schemas
  - Results are `{content:[{type:"text",text}], structuredContent, isError}`.
  - Same bearer authentication. Errors use JSON-RPC codes -32600, -32601, -32602 and -32603.
  - Security checklist from **Hou et al. 2025** (arXiv:2503.23278): no tool-description injection, a fixed
    allowlist, no write tools, and per-user concurrency limits.
- **A2A v1.0.0** (https://a2a-protocol.org/latest/specification/):
  - `GET /.well-known/agent-card.json` (name, description, url, version, capabilities{streaming:false},
    skills[{id,name,description,tags,examples}], security schemes bearer).
  - `POST /a2a` JSON-RPC with `SendMessage`, `GetTask`, `ListTasks` and `CancelTask`, backed by F13 jobs. The
    older aliases `message/send`, `tasks/get` and `tasks/cancel` are accepted too.

### Guardrails, budget, control (foundation)
- Covered in section 1.

## 3. Artifact contracts (`execution_artifacts` keys; the frontend renders each)

All values must be JSON-safe. Keep lists capped (≤ 50 rows) and never embed more than 3 images.

```jsonc
"agent_run": {"framework","run_id","sub_questions":[{"id","text","focus","depends_on","status","evidence_keys","note"}],
  "budget":{"limits":{...},"used":{"tool_calls","llm_calls","tokens_estimated","seconds"},"exhausted","exhausted_reason","by_phase"},
  "trace":[{"phase","tool","purpose","success","error_type","seconds"}],"decisions":[...],"steering":[...],
  "phases":[{"phase","stage","status","seconds"}],"draft_rounds"}
"agent_plan": {"planner":"llm"|"none","rationale","replans":0,
  "nodes":[{"id":"n1","tool","args":{},"depends_on":[],"purpose","status":"done|error|skipped|pending","result_key","error","seconds"}],
  "rejected":[{"node","reason"}]}
"clarification": {"question","options":["..."],"reason","signals":["..."]}
"plan_approval": {"approval_id","status":"approved|edited|rejected|auto|timeout","sources":[{"key","label","run","reason"}]}
"hypotheses": {"target","model_id","method":"elo-tournament","experiments_run":0,
  "hypotheses":[{"id","statement","mechanism","intervention":{"kind","parameter","factor"},"readout","prediction":"increase|decrease",
     "result":{"baseline","perturbed","log2_fold_change","supports":true|false|null},"elo","rank","evidence_keys":[]}],"note"}
"evidence_conflicts": {"queries":[...],"claims_checked":0,"classifier":"nli|llm|heuristic",
  "conflicts":[{"claim","passage","source_key","pmid","doi","label":"contradicts|supports|neutral","likert":0-10,"score"}]}
"claim_confidence": {"answer_confidence":0-1,"method","calibration":{"temperature","conformal_threshold"},
  "claims":[{"claim_id","text","confidence":0-1,"band":"high|medium|low","features":{...},"nli_label"}]}
"sandbox_analysis": {"status":"ok|error|disabled","iterations","code","stdout","result","error","dataset",
  "figures":[{"name","png_base64"}],"tables":[{"name","columns","rows"}],"policy":{"timeout_s","network":"blocked","isolation"}}
"model_calibration": {"model_id","dataset","backend","status","objective":{"chi2","rmse","n_points"},
  "parameters":[{"name","initial","estimate","ci_low","ci_high","identifiability":"identifiable|practically_non_identifiable|structurally_non_identifiable"}],
  "profiles":{"<param>":[{"value","chi2"}]},"fit":{"<species>":{"time":[],"observed":[],"simulated":[]}}}
"structure_intel": {"uniprot","gene","alphafold":{"model_id","mean_plddt","plddt_band","url","pdb_url","version"},
  "pdb_entries":[{"pdb_id","title","method","resolution","ligands":[],"url"}],"docking":{...}|null,"generated":[...]|null}
"omics_intel": {"gwas":{"status","items":[...]},"gtex":{...},"string":{...},"reactome":{...},"pubchem":{...},"preprints":{...},"depmap":{...}}
"project_memory": {"project_id","recalled":[{"id","subject","relation","object","valid_from","source","confidence"}],"written":0,"invalidated":0}
```

New SSE events: `run_control` (first event), `approval_request` (from approval phases), and `job` events, which
reuse the chat event types.

## 4. Work packages (parallel, disjoint ownership)

| WP | Features | Owns |
|---|---|---|
| 1 planning | F1, F3, F18 | agentic/{plan_graph,planner,executor,plan_execute,decompose,tool_retrieval}.py; tests/test_agentic_planning.py, tests/test_tool_retrieval.py |
| 2 loops | F2, F4, F7 | agentic/{gaps,counter_evidence,topologies,specialists}.py; tests/test_agentic_loops.py |
| 3 verification | F8, F9 | agentic/{nli,confidence,calibration_stats}.py; tests/test_agentic_verification.py |
| 4 science | F6, F11 | agentic/{hypothesis,calibrate}.py, vivarix/calibration.py, extensions/calibration.py; tests/test_hypothesis_calibration.py |
| 5 analyst+structure | F10, F12 | agentic/sandbox/*, agentic/analyst.py, extensions/sandbox.py, structintel/*, extensions/structure.py; tests/test_sandbox.py, tests/test_structure_intel.py |
| 6 data+reliability | F19, F20 | tools/reliability.py, sources_extra/*, extensions/omics_sources.py; tests/test_reliability.py, tests/test_omics_sources.py |
| 7 platform | F13, F15, F16, F17 | agentic/{clarify,approval,jobs,project_memory}.py, utils/memory/project_store.py, app/backend/routers/{agent_runs,jobs,projects,mcp,a2a}.py; tests/test_agent_platform.py |
| 8 frontend | UI for all | web/** |

Cross-package calls go only through the contracts above:
- F7 and F9 read `state.bag["nli_judge_fn"]` and `state.bag["nli_scores"]` (WP3 sets them; consumers must work when
  they are absent).
- F2 reads `state.sub_questions` (WP1).
- F6 uses the registry tools.
- The jobs router imports the orchestrator.
- MCP and A2A import `phronesis.agentic.jobs`. WP7 owns both, so they can agree internally.

Phase order: clarify 10, project_memory recall 20, decompose 30 | approval 10 | plan_execute 10, supervisor 10,
swarm 10, hypothesis 40, analyst 50, calibrate 55, counter_evidence 60, nli 90 | gaps 10, debate 20 | confidence 10,
project_memory consolidate 50.

## 5. Done criteria per package
- Its tests pass, `tests/test_agentic_foundation.py` passes, and the **whole** suite still passes
  (`.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider`). Other packages may be mid-edit, so a failure
  only counts against you if it is in your own files.
- No new dependencies. Every model and network path has an offline fallback.
- Docstrings cite the papers this document lists for the feature.
- The final report lists the files changed, the tests added, the env flags, and any change requested to a frozen file.
