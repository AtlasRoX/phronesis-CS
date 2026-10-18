# Zero-Docker Cloud Architecture & Deployment Guide — Phronesis

> **Phronesis** — Biomedical Research Assistant  
> **Made by ChronoStrider Engineering (ChronoStrider Enginnerings)**

This guide describes how to configure, run, and deploy **Phronesis** (engineered by **ChronoStrider Engineering** / **ChronoStrider Enginnerings**) with **zero Docker dependencies**, using a 100% **cloud-native** architecture powered by **BioContext MCP**.

---

## 🌟 Architectural Principles

1. **Zero Docker Dependency**:
   - **No local containers required** (no `docker compose up`, no `etcd`, no `minio`, no `milvus-standalone`).
   - Run directly with `uv` on any workstation, cloud VM, or serverless container.
2. **All-Cloud Foundation**:
   - **Cloud Vector Database**: Fully managed **Zilliz Cloud** (Serverless Milvus) via remote URI and API token.
   - **In-Process Fallback**: **Milvus Lite** (`uri="./milvus_data.db"`) runs natively inside Python for zero-setup local development.
   - **Cloud LLM & Embeddings**: OpenAI (`gpt-4o`, `gpt-4o-mini`, `text-embedding-3-small`), Azure OpenAI (Azure AD credentials), or NVIDIA Cloud NIM endpoints (`build.nvidia.com`).
3. **BioContext MCP Integration**:
   - Standardized biomedical knowledge retrieved via **BioContext Model Context Protocol** (`https://mcp.biocontext.ai/mcp/`).
   - Unified tools for **OpenTargets** (target-disease associations & tractability), **KEGG** (metabolic & disease pathways), **Europe PMC** (scientific literature), **UniProt** (protein sequences & functions), **Ensembl** (genomics), and **PanglaoDB** (single-cell markers).

---

## ⚙️ Environment Configuration

Set the following environment variables:

```bash
# ==========================================
# 1. Cloud LLM Configuration
# ==========================================
export OPENAI_API_KEY="sk-..."                 # Required for OpenAI models
# Or for Azure OpenAI:
# export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"
# export AZURE_OPENAI_DEPLOYMENT="your-deployment"
# export AZURE_CLIENT_ID="..."
# export AZURE_TENANT_ID="..."
# export AZURE_CLIENT_SECRET="..."
# Or for NVIDIA Cloud Endpoints:
# export NVIDIA_API_KEY="nvapi-..."

# ==========================================
# 2. Zero-Docker Cloud Vector Database
# ==========================================
# Option A: Zilliz Cloud (Fully Managed Cloud Milvus)
export MILVUS_URI="https://in03-xxxx.serverless.gcp-us-west1.cloud.zilliz.com"
export MILVUS_TOKEN="db_admin:YourZillizPassword"  # Or your Zilliz Cloud API Key

# Option B: Milvus Lite (Zero Docker, embedded local file)
# export MILVUS_URI="./milvus_data.db"
# (If MILVUS_URI is left empty, the system automatically falls back to Milvus Lite)

# ==========================================
# 3. BioContext MCP Cloud Gateway
# ==========================================
export BIOCONTEXT_MCP_URL="https://mcp.biocontext.ai/mcp/"

# ==========================================
# 4. Optional Agent Keys
# ==========================================
export ZOTERO_API_KEY="..."                    # For Lithoscribe
export ZOTERO_USER_ID="..."                    # For Lithoscribe
```

---

## 🚀 Running Locally with Zero Docker

With dependencies managed via `uv`, you can launch any agent without starting Docker:

```bash
# Launch the main orchestrator agent
uv run streamlit run app/frontend/streamlit_app_phronesis.py

# Or launch specific agents:
uv run streamlit run app/frontend/streamlit_app_synapta.py
uv run streamlit run app/frontend/streamlit_app_vivarix.py
uv run streamlit run app/frontend/streamlit_app_lithoscribe.py
uv run streamlit run app/frontend/streamlit_app_lumicell.py
```

Notice the sidebar will display:
```
☁️ Cloud & BioContext MCP Status
Vector Database: ☁️ Zilliz Cloud (Managed Milvus)
BioContext MCP Gateway: https://mcp.biocontext.ai/mcp/
BioContext Resources: OpenTargets, KEGG, EuropePMC, UniProt, Ensembl, PanglaoDB
Zero-Docker Cloud Native Mode active.
```

---

## ☁️ Cloud Deployment Blueprints

### Blueprint 1: Streamlit Community Cloud (Fastest Zero-Docker Deploy)

1. Fork or push your repository to GitHub.
2. Log in to [share.streamlit.io](https://share.streamlit.io/).
3. Create a new app:
   - **Repository:** `YourOrg/Phronesis`
   - **Branch:** `main`
   - **Main file path:** `app/frontend/streamlit_app_phronesis.py`
4. In **App settings → Secrets**, paste:
   ```toml
   OPENAI_API_KEY = "sk-..."
   MILVUS_URI = "https://in03-xxxx.serverless.gcp-us-west1.cloud.zilliz.com"
   MILVUS_TOKEN = "db_admin:..."
   BIOCONTEXT_MCP_URL = "https://mcp.biocontext.ai/mcp/"
   ```
5. Click **Deploy**. Streamlit Cloud builds and hosts the app natively without requiring Docker.

---

### Blueprint 2: Azure App Service (Enterprise Cloud)

1. Create an Azure App Service plan with Linux Python 3.12 runtime.
2. In Azure Portal, configure **Configuration → Application settings**:
   - `OPENAI_API_KEY`
   - `MILVUS_URI`
   - `MILVUS_TOKEN`
   - `BIOCONTEXT_MCP_URL`
   - `SCM_DO_BUILD_DURING_DEPLOYMENT = 1`
3. Set Startup Command:
   ```bash
   streamlit run app/frontend/streamlit_app_phronesis.py --server.port 8000 --server.address 0.0.0.0
   ```
4. Connect your GitHub repository for continuous deployment.

---

### Blueprint 3: Hugging Face Spaces

1. Create a new Space on [Hugging Face Spaces](https://huggingface.co/spaces) with SDK: **Streamlit**.
2. Add your secrets in **Settings → Repository secrets**:
   - `OPENAI_API_KEY`
   - `MILVUS_URI`
   - `MILVUS_TOKEN`
   - `BIOCONTEXT_MCP_URL`
3. Push your repository code. The space builds automatically using `requirements.txt` or `pyproject.toml`.

---

## 🧬 BioContext MCP Capabilities in Phronesis

| Agent | BioContext Tool | Purpose |
| :--- | :--- | :--- |
| **Synapta** | `biocontext_opentargets` | Enriches extracted subgraphs with clinical evidence, disease targets, and drug tractability. |
| **Synapta** | `biocontext_kegg` | Maps disease and drug nodes to KEGG biological & metabolic pathways. |
| **Vivarix** | `biocontext_uniprot` | Validates model species against UniProtKB protein sequences and functional annotations. |
| **Vivarix** | `biocontext_kegg` | Cross-references reaction kinetics and enzymes with KEGG metabolic networks. |
| **Lithoscribe** | `biocontext_europepmc` | Queries Europe PMC for full-text articles and citations alongside Semantic Scholar. |
| **Lumicell** | `biocontext_panglaodb` | Identifies single-cell cell-type markers and expression profiles. |
| **Lumicell** | `biocontext_ensembl` | Converts and standardizes gene symbols and genomic coordinates. |
| **Phronesis** | `biocontext` | Universal biomedical intelligence gateway across all coordinated workflows. |
