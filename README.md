# Phronesis

> **Phronesis** — Biomedical Research Assistant for Clinical and Translational R&D  
> **Made by ChronoStrider Engineering (ChronoStrider Enginnerings)**

[![Tests](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_phronesis.yml/badge.svg)](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_phronesis.yml)
[![Web](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_web.yml/badge.svg)](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_web.yml)
[![Evals](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/evals.yml/badge.svg)](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/evals.yml)
[![Docs](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/mkdocs_deploy.yml/badge.svg)](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/mkdocs_deploy.yml)

Phronesis is an advanced research assistant for clinical and translational R&D, created and engineered by **ChronoStrider Engineering** (**ChronoStrider Enginnerings**). It answers questions about targets,
drugs, trials, variants and pathways by planning tool calls across public biomedical sources, then writing
an answer whose claims are checked against the evidence it retrieved and cited inline (PMIDs, NCT ids, ChEMBL
ids, FDA application numbers, DrugBank ids).

The stack:

- **Backend:** FastAPI (`app/backend/main.py`), with the orchestrator, tools and verifier in `phronesis/`.
- **Web app:** Next.js (`web/`).
- **Specialist agents** (`phronesis/`):
  - Vivarix: systems-biology models through COPASI/BioModels.
  - Synapta: knowledge graphs, including the full PrimeKG.
  - Lithoscribe: literature.
  - Lumicell: single-cell data.
- The legacy Streamlit apps in `app/frontend/` still run, but are no longer the main UI.

## Agent modes

Choose a mode per request with `framework`, or set a default with `PHRONESIS_DEFAULT_FRAMEWORK`.
`GET /api/frameworks` lists the modes.

| Mode | `framework` | What it does |
| --- | --- | --- |
| Plan-and-execute (API default) | `agentic-plan-execute` | Decomposes the question and runs an LLM-planned tool DAG in parallel. Then: gap-driven re-retrieval, a counter-evidence search, NLI claim checks and calibrated per-claim confidence. |
| Classic pipeline | `authoritative-react-compiler` | The original fixed six-stage pipeline. |
| Supervisor | `multi-agent-supervisor` | A supervisor routes the question to specialist agents (literature, target, simulation, omics, chemistry). |
| Swarm | `multi-agent-swarm` | The specialists hand control to each other until the task is done. |
| Debate | `multi-agent-debate` | Gathers evidence with plan-and-execute, then critic agents debate the draft before the final answer. |

All modes share:

- reliability policies on every tool call: retries, a result cache and circuit breakers;
- human-in-the-loop controls: clarifying questions, plan approval, steering and cancelling a run;
- background jobs you can reattach to;
- project memory;
- an opt-in code-analysis sandbox (`PHRONESIS_SANDBOX=on`);
- an MCP server (`POST /mcp`) and an A2A endpoint (`/.well-known/agent-card.json`).

[docs/agentic/README.md](docs/agentic/README.md) indexes the features against their papers and documents the
API.

## Data sources

| Area | Sources |
| --- | --- |
| Targets and genetics | Open Targets, ClinVar, CIViC, cBioPortal, gnomAD, GWAS Catalog, GTEx |
| Literature | Europe PMC, PubTator3, LitVar2, bioRxiv |
| Trials and regulatory | ClinicalTrials.gov; openFDA (FAERS adverse events, drug labels, Drugs@FDA) |
| Chemistry and pharmacology | ChEMBL, DGIdb, Pharos, BindingDB, PubChem |
| Pathways and networks | KEGG, Reactome, STRING, SynLethDB (local file), PrimeKG (local build) |
| Structure | AlphaFold DB, PDB |
| Optional | DepMap gene effect (local file, `PHRONESIS_DEPMAP_DIR`); Tavily web search (`TAVILY_API_KEY`) |

The local-file sources are skipped, with a note in the answer, until they are configured:

- SynLethDB: `PHRONESIS_SYNLETHDB_DIR`, downloaded with `scripts/download_synlethdb.py`.
- DepMap: `PHRONESIS_DEPMAP_DIR`.
- PrimeKG: built with `uv run phronesis-primekg build` (about 520 MB downloaded, a 63 MB store). The store
  is kept in `PHRONESIS_PRIMEKG_DIR` (default `~/.cache/phronesis/primekg`); see
  [docs/synapta/primekg_full_graph.md](docs/synapta/primekg_full_graph.md).

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Node.js 22+.

```sh
git clone https://github.com/AtlasRoX/phronesis-CS && cd phronesis-CS
uv sync --extra dev --frozen
cp .env.example .env        # then uncomment and fill in what you need (at least one LLM key)
```

Authentication: every data-bearing endpoint needs a bearer token. Set `PHRONESIS_API_TOKENS="token:user,..."`
or `PHRONESIS_JWT_SECRET`. If neither is set the API answers `503`. For single-user local development,
`PHRONESIS_AUTH_MODE=disabled` runs every request as one local user.

```sh
# Terminal 1: API on http://127.0.0.1:8000
uv run uvicorn app.backend.main:app --host 127.0.0.1 --port 8000

# Terminal 2: web app on http://localhost:3000
cd web && npm ci && npm run dev
```

The web app reads `NEXT_PUBLIC_API_URL` (default `http://127.0.0.1:8000`) from `web/.env.local`. To check it,
run `npm run typecheck`, `npm test` and `npm run build`.

## Configuration

Every environment variable is listed in [docs/configuration.md](docs/configuration.md), with its default and
where it is read. [`.env.example`](.env.example) is a commented template.

Both files are generated by `python scripts/gen_env_reference.py`. Re-run it after adding a variable;
`--check` reports drift.

State is kept in one data root when `PHRONESIS_DATA_DIR` is set. It holds:

- `memory/`
- `runs/`
- `uploads/`
- `jobs/`
- `tool_cache/`
- `biomodels_cache/`

A specific variable such as `PHRONESIS_RUNS_DIR` overrides the data root for its own store. With nothing
set, each store keeps its historical location in the workspace.

Retention:

- Expired tool-cache rows are purged.
- Finished jobs are deleted after `PHRONESIS_JOB_TTL_DAYS` (default 30).
- Only the newest `PHRONESIS_MAX_RUNS` run manifests are kept (default 500).

## Evaluation

`phronesis-eval` (`python -m phronesis.evals`) scores routing, retrieval against gold papers, and full
answers. Suites cover curated clinical questions and public benchmarks.

```sh
uv run python -m phronesis.evals run --level plan                  # offline routing check, seconds
uv run python -m phronesis.evals run --level answer --judge        # full answers with a rubric judge
uv run python -m phronesis.evals compare eval_runs/<a> eval_runs/<b>
```

[docs/evals.md](docs/evals.md) covers suites, golden facts, gates, baselines and CI.

## Development

```sh
uv run pytest -q tests/                  # backend tests
uv run ruff check phronesis app tests    # lint
uv run mkdocs serve                      # documentation site
```

- Setup, testing and workflow guides: [docs/developer/](docs/developer/README.md).
- Contribution guidelines: [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md).
- Design plans: [docs/plans/](docs/plans/).

## Acknowledgements & Credits

- **Engineering & Architecture:** **ChronoStrider Engineering** (**ChronoStrider Enginnerings**).

