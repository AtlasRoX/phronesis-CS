# Evaluating Phronesis

> **Phronesis** — Evaluation Suites & Benchmarks  
> **Made by ChronoStrider Engineering (ChronoStrider Enginnerings)**

`phronesis.evals` measures how well Phronesis handles clinical and research questions, at three levels:

| Level | What runs | What it checks | Cost |
|---|---|---|---|
| **plan** | Question parsing and routing (`build_query_context`), as the orchestrator calls it | Scope (research / conversational / unclear), question type, recognised entities, primary target, which sources run or are skipped | Seconds. Offline needs no keys or network; `--live` adds the entity resolver and planner model |
| **retrieval** | The plan, the search plan, and the same Lithoscribe call production makes | Whether the papers an expert would expect are retrieved: hit@k, recall@k, MRR | About 15 s per question; EuropePMC + planner model |
| **answer** | The full orchestrator (or the running API with `--via`), in an isolated thread that is deleted afterwards | Golden facts (fact recall), forbidden claims, abstention, agents run, verifier result and faithfulness, hard gates, budgets; benchmark labels; optional rubric judge | 30–70 s per question on the classic pipeline, 4–20 min on agentic frameworks; full model usage |

## Quick start

```bash
uv run python -m phronesis.evals list                                         # suites and what they cover
uv run python -m phronesis.evals run --level plan                             # offline routing check, ~5 s
uv run python -m phronesis.evals run --suite clinical --level retrieval       # literature retrieval, ~2 min
uv run python -m phronesis.evals run --level answer --judge                   # full answers, ~20 min
uv run python -m phronesis.evals run --suite clinical --level answer --ids clin-sepsis   # one case
```

`phronesis-eval` is the same command once the package is installed. Keys come from `.env`, as for the app.

Each run writes `eval_runs/<time>_<suites>_<levels>/`:

- `report.html`: the summary, then every case with its checks, full answer, and what the run observed. It has a filter box (try `fail`).
- `report.md`: the same summary, for pull requests and chat.
- `summary.json`: machine-readable metrics.
- `results.jsonl`: one line per case and level.
- `meta.json`: model, settings, environment, and a fingerprint of the routing/retrieval code and case data.

## Suites

| Suite | Cases | Purpose |
|---|---|---|
| `clinical` | 20 | Treatment, pharmacology, diagnosis, epidemiology, mechanism, safety, trials, genetics, target, methods, statistics, comparison, biomarker, regulatory. Each case has gold landmark PMIDs (each resolved by exact title on EuropePMC), required facts, and an expert reference answer |
| `behavior` | 25 | Greetings and typos, gibberish, thanks, papers-only and no-simulation requests, simulations, single-cell with and without data, memory follow-up, vague questions, a hallucination trap, prompt injection |
| `golden` | 7 | Live only. Long questions, one or more per orchestration framework (KRAS G12C, BRCA2/SOLO-1, adjuvant PD-1 melanoma, KRAS repurposing, PARP/HRD, EGFR T790M/C797S, MYC in glioblastoma), each with structured golden facts: FDA application numbers, approval dates, hazard ratios with CIs |
| `audit` | 23 | Live only. The former `scripts/audit_evaluation_runner.py` cases (web search off) with assertions: greeting runs no tools, memory follow-up via `setup`, prompt injection must not obey or disclose secrets, simulations, single-cell states |
| `pubmedqa` | 1,000 | PubMedQA PQA-L: yes/no/maybe questions with expert labels and their source PMID. Downloaded on first use (MIT) |
| `litqa2` | 199 | LAB-Bench LitQA2: multiple choice with an "insufficient information" option and source DOIs. Downloaded on first use (CC BY-SA; carries a training-data canary, so it is never stored in the repo) |
| `bioasq` | — | BioASQ task B: yes/no, factoid, list and summary questions with gold documents. Registration is required, so download the JSON and pass `--bioasq-file` |
| `path/to/file.jsonl` | — | Your own cases |

Benchmarks are run open-domain: Phronesis retrieves its own evidence, so a PubMedQA question also measures whether the source abstract was found. Sample them with `--limit 50 --seed 1`. LitQA2 asks about papers published after many models' training data; read its accuracy together with coverage (how often the answer committed to an option).

## Options worth knowing

- `--ids`, `--category`, `--tags`, `--limit`, `--seed`: choose cases. `--ids` takes id prefixes, so `clin-` means every clinical case.
- `--concurrency N`: answer-level cases at once (default 2). Plan and retrieval run up to 4×N.
- `--judge`: score answers 1–5 on correctness, grounding, completeness, calibration and clarity against the reference answer.
  - The judge sees the verified citations, so recent papers are not mistaken for fabrications.
  - `--judge-min 3.5` turns the score into a pass/fail check.
  - `--judge-model` uses a different model from the system under test (recommended when you can).
- `--retries N` (default 1): re-runs a failed live plan or retrieval case. A pass on retry is reported as **flaky**, meaning an upstream service hiccup, not a regression. `--answer-retries` does the same for answers.
- `--resume <run dir>`: continues an interrupted run, retrying only cases that errored.
- `--no-web`: disables web search everywhere, to isolate the peer-reviewed path.
- `--fail-under "answer.pass_rate=0.8,retrieval.hit_rate=0.7"`: the run exits with code 1 below any threshold.
  - Thresholds can name any level metric: `retrieval.mrr`, `answer.verified_rate`, `answer.label_accuracy`, `answer.judge.overall`, `answer.fact_recall_mean`, `answer.faithfulness_mean`, `answer.gate_pass_rate`, and so on.
- `--framework <id>`: runs every case with this orchestration framework (`authoritative-react-compiler`, `agentic-plan-execute`, `multi-agent-supervisor`, `multi-agent-swarm`, `multi-agent-debate`), overriding each case's `framework`.
- `--via http://127.0.0.1:8000`: see [Through the API](#through-the-api-via).
- `--record` / `--replay` (`--replay-lenient`, `--cassette-dir`): record answer cases to cassettes against the live system, or replay them offline. See [Recorded cassettes](#recorded-cassettes-offline-deterministic-answers).
- `--repeat N`: runs each case N times, in sequence. The result keeps the first successful run's observations and replaces `fact_recall`, `faithfulness`, latency, calls and tokens with their means. `metrics.repeat` holds each run's values and the mean and SD per metric. The case passes when at least half the runs pass. It is marked **flaky** when runs disagree on pass/fail, or when the SD of `fact_recall` or `faithfulness` exceeds `--flaky-sd` (default 0.15).

## Metrics and hard gates

Every answer-level result reports these metrics. They are computed from stored observations, so `rescore` recomputes them.

| Metric | Meaning |
|---|---|
| `fact_recall` | Required golden facts found ÷ required facts. `facts_missing` lists the ids of the missing ones; optional misses are listed in `facts_optional_missing` |
| `faithfulness` | The verifier's claim audit: supported ÷ (supported + unsupported + contradicted). Claims that were not checked are excluded. RAGAS/FActScore-style |
| `steps_failed` | Activity steps (thoughts) with status `error` |
| `llm_calls`, `tokens_est` | From the agent budget (`execution_artifacts.agent_run.budget.used`). Agentic frameworks only; `null` on the classic pipeline |
| `tool_calls` | Tool calls in the run manifest |
| `latency_s` | Wall time of the question, setup turns excluded |

`summary.json` adds, per level, `by_framework` (pass rate, fact recall, faithfulness, latency p50/p95 and gate failures for each requested framework) and `latency_p95_s`. The answer section adds `fact_recall_mean`, `faithfulness_mean`, `steps_failed_mean`, `llm_calls_mean`, `tool_calls_mean`, `tokens_est_mean`, `gate_pass_rate` and `gate_failures`.

**Hard gates** are deterministic required checks that run on every answer. They need no model:

- `answer.no_crash`: the run returned a non-empty answer. An exception or an HTTP error from `--via` fails it.
- `answer.citations_in_evidence`: every PMID, NCT, CHEMBL, NDA, BLA or ANDA id written in the answer appears in the run's own evidence or in the question. An id that is in neither is treated as hallucinated. The evidence comprises:
  - the canonical ids and output digests of successful tool calls;
  - the synthesis scope notes;
  - the citation entries and aliases;
  - the verifier's verified identifiers;
  - the evidence artifacts.

  Model-written artifacts never count as evidence: claim confidence, the agent plan, run trace and dialogue, hypotheses and project memory.
- `answer.no_internal_leak`: a research answer must not contain:
  - internal component names (Lithoscribe, Synapta, Vivarix, Lumicell, BioDiscovery, in any case);
  - evidence-field keys (`clinical_trials`, `structure_intel`, `total_reports`, `tool_status`, …);
  - internal step ids (`agent.gaps.9-12`);
  - prompt tags (`[CLINICAL_CLAIM]`).

  Code spans and URLs are ignored. The conversational introduction names the components on purpose, so it is not checked.

A case can switch a gate off with `"skip_gates": ["answer.no_internal_leak"]`.

**Budgets** are optional per-case limits, each a required check: `max_latency_s`, `max_tokens`, `max_llm_calls`, `max_tool_calls` (`0` means no tool may run, as for greetings) and `min_faithfulness`. A budget whose metric the run did not record, such as model calls on the classic pipeline, is reported but not failed.

## Through the API (`--via`)

```bash
uv run python -m phronesis.evals run --suite golden --level answer --via http://127.0.0.1:8000 --ids golden-kras
```

Each answer case, and each of its setup turns, is sent to `POST /api/chat` with the body the web client sends: `message`, `thread_id`, `enable_web_search`, the case's `framework`, `agent_options`, `dataset_ids` and `project_id`, and `model` when `--model` is given. The returned JSON (the orchestrator result's `to_dict()`) is scored exactly like an in-process result, so the API layer is tested as well. Afterwards the thread's memory is deleted with `DELETE /api/memory/thread/{id}`.

Details:

- **Authentication.** A bearer token is read from `PHRONESIS_EVAL_TOKEN`. It is never written to `meta.json`.
- **Errors.** A non-200 status, or a reply without a `response` string, becomes an errored result with a failed `answer.no_crash`.
- **Other levels.** Plan and retrieval still run in-process.
- **Timeouts.** Golden cases set `timeout_s` (1,800 s), because agentic frameworks take minutes.

## Regression control: baselines

```bash
uv run python -m phronesis.evals run --suite golden --level answer --via http://127.0.0.1:8000
uv run python -m phronesis.evals baseline save eval_runs/<run> --name golden          # eval_runs/baselines/golden.json
# ... change the system, run again ...
uv run python -m phronesis.evals baseline check eval_runs/<new run> --name golden --delta 0.03
```

- **Saving.** `baseline save` stores a per-case snapshot of the run. For each scored case it records pass, `fact_recall`, `faithfulness`, latency and each hard gate's outcome. Use `--baseline <file>` to write or read an explicit path, such as a committed baseline.
- **Checking.** `baseline check` pairs the cases that both runs scored. For each metric (`--metrics`, default `fact_recall,faithfulness,pass`) it bootstraps a 95% CI of the mean per-case delta, new minus baseline. The bootstrap uses 2,000 resamples, a fixed seed (`--seed`) and pure Python.
- **When it fails.** The check exits with code 1 when either holds:
  - a metric's CI upper bound is below `-delta`: the drop is both real and larger than the tolerance;
  - a hard gate that held in the baseline (or was not recorded there) now fails on any case.

  It exits with code 2 when the baseline file does not exist.
- **Output.** The comparison is written to `<run>/baseline_check.md` and `baseline_check.json`. It also lists cases that newly fail and cases present in only one of the two runs.

With few live cases a single bad run is noisy. Use `--repeat 3` before saving a baseline or reading a drop.

## Re-scoring without re-running

Edited a case's expectations, or improved a scorer? Re-apply them to a finished run's stored answers, without calling the system again:

```bash
uv run python -m phronesis.evals rescore eval_runs/<run>               # seconds; previous results kept in results.prev.jsonl
uv run python -m phronesis.evals rescore eval_runs/<run> --rejudge     # also re-ask the judge and label grader
```

Retrieval results store every retrieved PMID, DOI and title in rank order, and answers store the full response and citations. Gold papers, title patterns, required facts and thresholds can therefore all change after the fact. Re-run the system only when the system itself changed.

## Comparing runs

```bash
uv run python -m phronesis.evals compare eval_runs/<before> eval_runs/<after>
```

This prints metric deltas per level, every case that went from pass to fail (with the failing checks), and every case that was fixed. Run the same suites and levels before and after a change. `meta.json` records the code fingerprint, so you can confirm which code each run used.

## Writing a case

One JSON object per line. Every block is optional; a case is scored at each level it has a block for.

```json
{"id": "clin-sepsis-criteria", "suite": "clinical", "category": "diagnosis",
 "query": "What are the diagnostic criteria for sepsis?",
 "plan": {"scope": "research", "question_type": "diagnosis", "entities": ["Sepsis"], "skip": ["vivarix"]},
 "retrieval": {"gold_pmids": ["26903338", "26903335", "26903336"],
               "gold_title_patterns": ["Sepsis-3|consensus definitions? for sepsis"], "k": 8},
 "answer": {"must_mention": ["SOFA", "lactate"], "cite_gold": true, "abstain": false},
 "reference": "Sepsis-3 defines sepsis as ... SOFA increase of 2 or more points ..."}
```

Guidance:

- **Gold papers.** List several acceptable PMIDs (the landmark trial, its main follow-up, the guideline), each confirmed on EuropePMC. Add `gold_title_patterns` when any paper of the right kind is a correct result; open retrieval has many right answers. Keep patterns specific enough that an off-topic paper cannot match. Use `"required": false` for papers older than the 12-year citation-ranked window, or when gold is uncertain: the miss is then reported without failing the case.
- **Required facts** (`must_mention`) are regexes; all must match, case-insensitively. Test the facts an expert would insist on, with alternatives (`"IVIG|immunoglobulin"`), not wording.
- **Forbidden claims** (`must_not_mention`): fabricated figures or confirming an injected claim.
- **Abstention.** `"abstain": true` means the answer must say the evidence or entity does not exist (hallucination traps). `false` adds an informational check that the answer does not open with a refusal.
- **Routing.** `plan.run` and `plan.skip` name sources: `synapta`, `lithoscribe`, `websurfer`, `vivarix`, `lumicell`, `clinical_trials`, `chembl`, `openfda`, `variant_intel`. Set `"offline": true` only when the entities and routing hold without the live resolver (the offline lexicon knows common genes and drugs, not every disease).
- **Memory.** `setup` lists earlier turns in the same thread.
- **How the case runs.** `framework` requests an orchestration framework, and `--framework` overrides it. `answer.framework` is a different thing: the framework that must have been *used*, for example `conversational-direct` for a greeting. The case can also set `agent_options` (for example `{"budget": {"max_llm_calls": 30}}`), `dataset_ids`, `project_id` and `timeout_s`. Tag a case `live` when nothing in it can be scored offline; `list` shows a suite as live only when all its cases are.
- **Validation.** Cases are checked on load: every regex must compile and must not contain control characters (a lost backslash turns `\b` into a backspace).

### Golden facts

Structured facts make answer correctness measurable without a judge, in the style of SAFE/FActScore. Each fact has an `id` and exactly one matcher:

```json
"facts": [
  {"id": "sotorasib-nda", "pattern": "NDA\\s*#?\\s*214665"},
  {"id": "sotorasib-approval-date", "date": "2021-05-28"},
  {"id": "codebreak200-pfs-hr", "value": 0.66, "tolerance": 0.01, "ci": [0.51, 0.86], "context": "CodeBreaK|sotorasib"},
  {"id": "keynote716", "any_of": [{"id": "a", "pattern": "KEYNOTE-?716"}, {"id": "b", "pattern": "NCT03553836"}]},
  {"id": "profound-rpfs-hr", "value": 0.34, "tolerance": 0.01, "ci": [0.25, 0.47], "context": "PROfound", "required": false}
]
```

- **`pattern`** is a case-insensitive regex.
- **`date`** (ISO) also matches `December 19, 2014`, `Dec. 19, 2014`, `19 December 2014` and `12/19/2014`.
- **`value`** matches a number however it is written: `0.30`, `0.3`, `0·30`, `1,063`, a Unicode minus. A number glued to a word, as in `G12C` or `rs121434569`, never matches.
  - Use `tolerance` for rounding: 0.663 matches 0.66 ± 0.01.
  - With `ci`, an interval within `window` characters after the number (default 200) must also match. Any of `95% CI 0.23–0.41`, `(0.23 to 0.41)` or `0.23, 0.41` works, with `ci_tolerance` defaulting to `tolerance`.
- **`context`** is a regex that must occur within `window` characters of the match. It ties a hazard ratio to its trial.
- **`any_of`** accepts alternative facts; any one of them counts.

Each fact is a check. An optional fact (`"required": false`) is informational and is left out of `fact_recall`. With `"min_fact_recall": 0.8` in the answer block, a single `answer.fact_recall` check decides instead, and every fact becomes informational.

To add facts:

1. **Confirm against primary sources.** Use the label or Drugs@FDA for application numbers and dates, and the trial publication or registry for hazard ratios.
2. **Check against stored runs.** Run `rescore` on stored runs to see the facts hold on real answers before relying on them.
3. **Mark the rest optional.** A fact the question does not strictly require, such as a secondary trial's HR, should be optional.

## Recorded cassettes: offline, deterministic answers

Live answer cases are slow and vary between runs. A **cassette** records every tool and model call of one answer case, so the case can be replayed offline: no keys, no network, the same result on every run. CI replays the committed cassettes on every commit.

```bash
# 1. Record locally, against the live APIs and model (in-process only; not with --via):
uv run python -m phronesis.evals run --suite golden --level answer --ids golden-kras-g12c-nsclc --record
# 2. Commit the cassette: phronesis/evals/cassettes/golden/golden-kras-g12c-nsclc.json
# 3. Replay (what CI does), in seconds:
uv run python -m phronesis.evals run --suite golden --level answer --replay
uv run python -m phronesis.evals run --suite golden --level answer --replay --repeat 2   # determinism check: never flaky
```

What a cassette holds (`phronesis/evals/cassette.py`):

- **Tool calls.** Each registry call is recorded with its `ToolResult`, keyed by the tool name and the canonical JSON of its arguments. The hook sits below the registry's argument validation and run-manifest recording, and above its cache, circuit breakers, retries and executor. A replayed run therefore still writes its own manifest, canonical ids and evidence, and the citation gate checks them as usual.
- **Model calls.** Each call is keyed by the model and the prompt messages, and records the reply text and the model's reasoning. This covers models obtained through `get_llm`, whether called with `ainvoke`, `astream` or `astream_with_reasoning`: the planner, the agent phases, NLI, the synthesis, rewrites and the Reflexion correction. Replay builds no real client.
- **Side lookups.** Calls made outside the registry: the BioModels search and the run manifest's upstream version probes.
- **Run settings.** The case's query, setup turns and run options, the model, the web-search setting, the latency, and the registered tools.

How replay matches calls:

- **Normalised keys.** Before hashing, volatile parts of prompts and arguments are replaced by placeholders: ISO timestamps, today's date (and the recording's date) in any common format, uuids, `run_…`/`job_…` ids, eval thread ids and Unix timestamps. The `<retrieval_date>` in a prompt does not break a replay.
- **Order.** A call made several times replays its recordings in order.
- **Pinned settings.** `PHRONESIS_NLI=llm` and no tool embeddings, for recording and replay alike. Without the pin, a cross-encoder in one machine's HF cache would send that machine down a different path from CI.
- **Recorded latency.** A replayed result reports the recorded run's latency (`replay_latency_s` holds the replay's own wall time), so its metrics match the recording.

What is stored:

- **Secrets are redacted.** API keys, `Authorization` headers, bearer tokens, `nvapi-`/`sk-` keys, `?api_key=` URL parameters and the values of `*_KEY`/`*_TOKEN`/`*_SECRET` environment variables never reach the file.
- **Prompts are not stored in full.** Each prompt keeps a per-message hash, its length and a short preview. Set `PHRONESIS_CASSETTE_PROMPTS=1` while recording to keep the full normalised prompts; a miss then shows the first differing characters.
- **Size.** Files larger than 2 MB are written gzipped (`.json.gz`).

Failures:

- **Strict by default.** A call the cassette does not hold is a *miss*: it raises `CassetteMiss`, and the case fails `answer.cassette` even when the orchestrator recovers from the exception. The check names the call and, for a model call, which message differs from the nearest recording. `run --replay` exits with code 1 when any case missed.
- **Network.** During replay, outbound network connections are refused and reported, so an unhooked network path shows up as a failure instead of a silent live call.
- **Stale cassettes.** A case whose query, setup or run options changed since recording fails `answer.cassette` without running.
- **No cassette.** Cases without a cassette are skipped.
- **Lenient replay.** `--replay-lenient` answers a missed model call with the nearest recording of the same model and a missed tool call with a failed `ToolResult`. It reports the misses (`cassette_misses`) without failing, which is useful to see how far a change reaches.

When to refresh:

- **Re-record a case** with `--record`, which overwrites its cassette, whenever its replay misses because of an intended change: a prompt, a tool argument, the search plan or the routing. Commit the new file with the change.
- **Re-record yearly.** The literature search window is computed from the current year, so recordings go stale at the turn of the year.
- **Recordings capture the live services as they were.** Use the live weekly golden job and `baseline check` to follow upstream changes; replay answers a different question: did *our code* change what it asks and does?

Limits:

- **Calls inside a tool are not recorded.** Replay never runs the tool, so an extension tool's own model call, or a registry fallback between tools, is not stored.
- **One recording per case.** `--record` cannot be combined with `--repeat`, `--resume` or `--via`.

## Continuous integration

`.github/workflows/evals.yml` has three jobs:

- **On every commit to main/master and on pull requests** that touch the system or the harness, it runs the harness tests, the offline plan level and the cassette replay. None of them needs keys.
  - The plan level gates on `plan.pass_rate=1`.
  - The replay runs every committed cassette (`run --suite <suites with cassettes> --level answer --replay`) and fails on any miss. With no cassettes committed, the step is skipped with a notice.
- **Weekly and on demand**, it runs retrieval and answers live, using the `NVIDIA_API_KEY` secret, and gates on `retrieval.hit_rate` and `answer.pass_rate`. It uploads `eval_runs/` as an artifact, so the HTML report can be downloaded from the run page.
- **Weekly and on demand, golden.** A third job runs the `golden` suite at the answer level and then checks it with `baseline check --baseline phronesis/evals/baselines/golden.json`. When no baseline is committed, the check step is skipped with a notice. To commit one, save it from a trusted run with `baseline save <run> --baseline phronesis/evals/baselines/golden.json`.

The push and pull-request path filters cover:

- the agents and the agentic runtime;
- tools, sources (including `sources_extra`), drug intelligence, Lithoscribe and `phronesis/utils/` (the model factory, HTTP helpers, biocontext);
- the API (`app/`) and the harness.

## Reading results honestly

- **A failing case is information.** First check whether the expectation is right; then fix the system. The `clin-crispr-screen-design` case fails retrieval on purpose: literature search returns applications of CRISPR screens, not design protocols, and that weakness should stay visible.
- **Judge scores are relative.** Compare them between runs with the same judge model; do not read them as absolute quality. The judge model may share blind spots with the system under test.
- **Expect variance.** Live services and model sampling vary. Compare runs on the same suite, and look at flaky counts before reading a one-case change as a regression.
