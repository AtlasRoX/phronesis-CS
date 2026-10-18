# Phronesis

> **Phronesis** — Biomedical Research Assistant for Clinical and Translational R&D  
> **Made by ChronoStrider Engineering (ChronoStrider Enginnerings)**

<!--  Project Info -->

![RELEASE](https://img.shields.io/github/v/release/AtlasRoX/phronesis-CS?label=RELEASE)
![Python Version from PEP 621 TOML](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fatlasrox%2FPhronesis%2Frefs%2Fheads%2Fmain%2Fpyproject.toml)

<!--  Deployment Workflows -->

[![Pages Deployment](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/pages/pages-build-deployment/badge.svg)](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/pages/pages-build-deployment)
[![MkDocs Deploy](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/mkdocs_deploy.yml/badge.svg)](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/mkdocs_deploy.yml)
[![Release](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/release.yml/badge.svg)](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/release.yml)
[![Docker Build & Push](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/docker_build.yml/badge.svg)](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/docker_build.yml)

<!--  Tests -->

[![TESTS Phronesis](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_phronesis.yml/badge.svg)](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_phronesis.yml)
[![Vivarix](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_vivarix.yml/badge.svg)](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_vivarix.yml)
[![Synapta](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_synapta.yml/badge.svg)](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_synapta.yml)
[![TESTS Lithoscribe](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_lithoscribe.yml/badge.svg)](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_lithoscribe.yml)
[![Lumicell](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_lumicell.yml/badge.svg)](https://github.com/AtlasRoX/phronesis-CS/actions/workflows/tests_lumicell.yml)

<!--  Docker Pulls -->

![Phronesis Pulls](https://img.shields.io/docker/pulls/vpatientengine/phronesis?label=Phronesis%20Pulls&color=blue&logo=docker&style=flat-square)
![Vivarix Pulls](https://img.shields.io/docker/pulls/vpatientengine/vivarix?label=Vivarix%20Pulls&color=blue&logo=docker&style=flat-square)
![Synapta Pulls](https://img.shields.io/docker/pulls/vpatientengine/synapta?label=Synapta%20Pulls&color=blue&logo=docker&style=flat-square)
![Lithoscribe Pulls](https://img.shields.io/docker/pulls/vpatientengine/lithoscribe?label=Lithoscribe%20Pulls&color=blue&logo=docker&style=flat-square)

## Introduction

Welcome to **Phronesis** — engineered and maintained by **ChronoStrider Engineering** (**ChronoStrider Enginnerings**).

Phronesis brings together AI-driven tools and autonomous agents to help researchers and pharma interact seamlessly with complex biological data.

Our toolkit currently consists of the following agents:

- **Vivarix** _(v1 released; v2 in progress)_: Engage directly with mathematical models in systems biology.
- **Synapta** _(v1 in progress)_: Access and explore complex biological knowledge graphs for insightful data connections.
- **Lithoscribe** _(v1 in progress)_: Get recommendations for articles related to your choice. Download, query, and write/retrieve them to your reference manager (currently supporting Zotero).
- **Lumicell** _(v1 in progress)_: Query and analyze sequencing data with ease.
- **Phronesis** _(v1 in progress)_: Converse with all the agents above (currently supports Vivarix and Synapta)

![Phronesis agents](assets/t2agents_diagram.png)

## News

- Talk2BioModels (now Vivarix) and Talk2KnowledgeGraphs (now Synapta) accepted at the MLGenX workshop during ICLR #2025 in Singapore. [Read More](https://openreview.net/forum?id=av4QhBNeZo)

<div align="center">
<strong>Watch the presentation:</strong><br><br>
<a href="https://www.youtube.com/watch?v=3cU_OxY4HiE">
<img src="https://img.youtube.com/vi/3cU_OxY4HiE/0.jpg" alt="Watch the presentation" width="480">
</a>
</div>

## Getting Started

### Installation

#### Option 1: Docker (stable-release)

_We now have all the agents available on Docker Hub._

Choose your agent below for detailed Docker instructions:

- [Phronesis](https://github.com/AtlasRoX/phronesis-CS/blob/main/phronesis/phronesis/install.md)
- [Synapta](https://github.com/AtlasRoX/phronesis-CS/blob/main/phronesis/synapta/install.md)
- [Vivarix](https://github.com/AtlasRoX/phronesis-CS/blob/main/phronesis/vivarix/install.md)
- [Lithoscribe](https://github.com/AtlasRoX/phronesis-CS/blob/main/phronesis/lithoscribe/install.md)

#### Option 2: git (for developers and contributors)

![Python Version from PEP 621 TOML](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fatlasrox%2FPhronesis%2Frefs%2Fheads%2Fmain%2Fpyproject.toml)

1. **Clone the repository:**

```sh
git clone https://github.com/AtlasRoX/phronesis-CS && cd phronesis-CS
```

2. **Install dependencies:**

We use `uv` for fast and reliable dependency management. Install uv first following the [official installation guide](https://docs.astral.sh/uv/getting-started/installation/).

> **For developers**: See [the developer setup guide](developer/README.md) for detailed setup instructions including system prerequisites.

```sh
uv sync --extra dev --frozen
```

> 💡 **Recommended**: Use `--frozen` flag to ensure exact reproducible builds using the pinned versions from `uv.lock`.

3. **Initialize API Keys**

```env
export OPENAI_API_KEY=....          # Required for all agents
export NVIDIA_API_KEY=....          # Required for all agents
export ZOTERO_API_KEY=....          # Required for Lithoscribe (Zotero)
export ZOTERO_USER_ID=....          # Required for Lithoscribe (Zotero)
export LANGCHAIN_TRACING_V2=true    # Optional for all agents
export LANGCHAIN_API_KEY=...        # Optional for all agents
```

4. **Launch the app:**

> System Dependency: libmagic (for secure uploads)
> For accurate file MIME-type detection used by our secure upload validation, install the libmagic system library. This is recommended across all providers (OpenAI, Azure OpenAI, NVIDIA) because it runs locally in the Streamlit apps.
>
> - Linux (Debian/Ubuntu): `sudo apt-get install libmagic1`
> - macOS (Homebrew): `brew install libmagic`
> - Windows: Use the `python-magic`/`python-magic-bin` package; libmagic is bundled
>   If libmagic is not available, the apps fall back to extension-based detection. For best security, keep libmagic installed.

**Option A: Using UV (recommended)**

```sh
uv run streamlit run app/frontend/streamlit_app_<agent>.py
```

**Option B: Traditional approach**

```sh
# Activate virtual environment
source .venv/bin/activate  # Linux/macOS
# or
.venv\Scripts\activate     # Windows

# Then run the app
streamlit run app/frontend/streamlit_app_<agent>.py
```

_Replace `<agent>` with the agent name you are interested to launch:_

- `phronesis`
- `vivarix`
- `synapta`
- `lithoscribe`
- `lumicell`

If your machine has NVIDIA GPU(s), please install the following this:

- [nvidia-cuda-toolkit](https://developer.nvidia.com/cuda-toolkit)
- [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/1.17.8/install-guide.html) (required for GPU support with Docker; enables containers to access NVIDIA GPUs for accelerated computing). After installing `nvidia-container-toolkit`, please restart Docker to ensure GPU support is enabled.

To use the **Agents**, you need a free **NVIDIA API key**. Create an account and apply for free credits [here](https://build.nvidia.com/explore/discover).

**Lithoscribe** and **Synapta** requires Milvus to be set up as the vector database — install Milvus depending on your setup by following the official instructions for [CPU](https://milvus.io/docs/install_standalone-docker-compose.md) or [GPU](https://milvus.io/docs/install_standalone-docker-compose-gpu.md). You will also need a **Zotero API key**, which you can generate [here](https://www.zotero.org/user/login#applications). _(The Zotero key is only required for Lithoscribe; all other agents do not need it.)_

> By default, `synapta` includes a small subset of the PrimeKG knowledge graph, allowing users to start interacting with it out of the box.
> To switch to a different knowledge graph or use your own, refer to the [deployment guide](https://atlasrox.github.io/Phronesis/synapta/deployment/).
> Additionally on **Windows**, the `pcst_fast 1.0.10` library requires **Microsoft Visual C++ 14.0 or greater**.
> You can download the **Microsoft C++ Build Tools** [here](https://visualstudio.microsoft.com/visual-cpp-build-tools/).

**LangSmith** support is optional. To enable it, create an API key [here](https://docs.smith.langchain.com/administration/how_to_guides/organization_management/create_account_api_key).

_Please note that this will create a new tracing project in your Langsmith
account with the name `PHR-xxxx`, where `X` can be `AA4P` (Main Agent),
`B` (Biomodels), `S` (Scholars), `KG` (KnowledgeGraphs), or `C` (Cells).
If you skip the previous step, it will default to the name `default`.
`xxxx` will be the 4-digit ID created for the session._

#### Option 3: pip (beta-release)

![Python Version from PEP 621 TOML](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fatlasrox%2FPhronesis%2Frefs%2Fheads%2Fmain%2Fpyproject.toml)

```sh
pip install phronesis
```

Check out the tutorials on each agent for detailed instructions.

## Contributing

We welcome your support to make **Phronesis** even better.
All types of contributions are appreciated — whether you're fixing bugs, adding features, improving documentation, or helping with testing, every contribution is valuable.

#### Development Setup

For contributors and developers, we have comprehensive documentation:

- **[Developer Setup Guide](developer/README.md)** - Complete setup instructions with UV, security implementation, and tooling
- **[Testing & Linting Guide](developer/TESTING_LINTING.md)** - How to run tests, coverage, and code quality checks
- **[SonarCloud Integration](developer/SONARCLOUD_SETUP.md)** - Code quality analysis and CI/CD integration
- **[GitHub Workflows](developer/WORKFLOWS.md)** - Understanding our CI/CD pipeline
- **[Streamlit Security](developer/STREAMLIT_SECURITY.md)** - File upload security implementation
- **[Azure Deployment](developer/AZURE_DEPLOYMENT.md)** - Understanding our Azure deployment setup

#### How to contribute

1. Star this repository to show your support.
2. Fork the repository.
3. Create a new branch for your work:

```sh
git checkout -b feat/your-feature-name
```

4. Set up your development environment:

```sh
uv sync --extra dev --frozen  # Install development dependencies
uv run pre-commit install    # Set up code quality hooks
```

5. Make your changes and run quality checks:

```sh
uv run ruff check --fix .  # Lint and fix code
uv run ruff format .  # Format code
uv run pre-commit run --all-files  # Run all checks (linting, formatting, security)

# Run submodule-specific checks (pylint configuration in pyproject.toml)
uv run pylint phronesis/lithoscribe/
uv run coverage run --include="phronesis/lithoscribe/*" -m pytest --cache-clear phronesis/lithoscribe/tests/ && uv run coverage report
```

6. Commit and push your changes:

```sh
git commit -m "feat: add a brief description of your change"
git push origin feat/your-feature-name
```

7. Open a Pull Request.

#### Areas where you can help

- Beta testing for Vivarix and Lithoscribe.
- Development work related to Python, bioinformatics, or knowledge graphs.

#### Contacts for contributions

- **Vivarix**: [@lilijap](https://github.com/lilijap), [@gurdeep330](https://github.com/gurdeep330)
- **Lumicell**: [@tAndreaniSanofi](https://github.com/tAndreaniSanofi), [@gurdeep330](https://github.com/gurdeep330)
- **Synapta**: [@awmulyadi](https://github.com/awmulyadi)
- **Lithoscribe**: [@ansh-info](https://github.com/ansh-info), [@gurdeep330](https://github.com/gurdeep330)

Please refer to our [CONTRIBUTING.md](CONTRIBUTING.md) and [developer documentation](docs/developer/) for detailed contribution guidelines and setup instructions.

## Feedback

If you have questions, bug reports, feature requests, comments, or suggestions, we would love to hear from you.
Please open an `issue` or start a `discussion`
