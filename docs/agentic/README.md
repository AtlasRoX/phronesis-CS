# Phronesis Agentic System

> **Phronesis** — Multi-Framework Agent Architecture  
> **Made by ChronoStrider Engineering (ChronoStrider Enginnerings)**

Phronesis can run each question through one of five orchestration frameworks. Choose one per request with
`framework`, or set a default with `PHRONESIS_DEFAULT_FRAMEWORK`. The API default is `auto`, which routes each question to one of the agentic frameworks ([auto-routing.md](auto-routing.md)).

| Framework id | What it does |
|---|---|
| `authoritative-react-compiler` | The classic fixed pipeline: parallel evidence, simulation and single-cell analysis, then synthesis and a Reflexion audit. It runs no agentic phases. |
| `agentic-plan-execute` | Decomposes the question, runs an LLM-planned tool DAG in parallel (LLMCompiler), re-retrieves to fill gaps, searches for counter-evidence, checks claims with NLI and reports calibrated confidence. |
| `multi-agent-supervisor` | A supervisor uses a Magentic-One-style task and progress ledger to delegate to specialist agents (literature, target, simulation, omics, chemistry). |
| `multi-agent-swarm` | Specialist agents hand control to each other until the task is done. |
| `multi-agent-debate` | Gathers evidence with plan-and-execute, then critic agents debate the draft before the final answer (Du et al. 2023). |

- Design, contracts and work packages: [`docs/plans/agentic-system.md`](../plans/agentic-system.md)
- Verified references, agent loop and reasoning: [`references-agent-loop.md`](references-agent-loop.md)
- Verified references, verification, memory, tools and data sources: [`references-verification-tools-data.md`](references-verification-tools-data.md)
- Platform API (run control, jobs, projects, MCP, A2A): [`platform-api.md`](platform-api.md)
- Code sandbox threat model: [`sandbox-security.md`](sandbox-security.md)

## Feature to paper index

| # | Feature | Primary references |
|---|---|---|
| 1 | Plan-and-execute tool DAG | LLMCompiler (Kim et al., ICML 2024, arXiv:2312.04511); ReWOO (arXiv:2305.18323); Plan-and-Solve (ACL 2023, arXiv:2305.04091); ReAct (ICLR 2023, arXiv:2210.03629) |
| 2 | Gap-driven re-retrieval | Self-RAG (ICLR 2024, arXiv:2310.11511); FLARE (EMNLP 2023, arXiv:2305.06983); CRAG (arXiv:2401.15884); IRCoT (ACL 2023, arXiv:2212.10509); PaperQA2 (arXiv:2409.13740) |
| 3 | Question decomposition | Least-to-Most (ICLR 2023, arXiv:2205.10625); Self-Ask (arXiv:2210.03350); Decomposed Prompting (ICLR 2023, arXiv:2210.02406); Adaptive-RAG (NAACL 2024, arXiv:2403.14403) |
| 4 | Supervisor, swarm and debate | Magentic-One (arXiv:2411.04468); AutoGen (COLM 2024, arXiv:2308.08155); MetaGPT (ICLR 2024, arXiv:2308.00352); Multi-agent debate (ICML 2024, arXiv:2305.14325); CRITIC (arXiv:2305.11738); Chain-of-Verification (arXiv:2309.11495) |
| 6 | Hypothesis to experiment loop | AI co-scientist (arXiv:2502.18864); BioDiscoveryAgent (ICLR 2025, arXiv:2405.17631); Robin (arXiv:2505.13400) |
| 7 | Counter-evidence search | PaperQA2 / ContraCrow (arXiv:2409.13740); SciFact (EMNLP 2020, arXiv:2004.14974); SciFact-Open (arXiv:2210.13777) |
| 8 | NLI claim verification | SummaC (TACL 2022, arXiv:2111.09525); FActScore (EMNLP 2023, arXiv:2305.14251); MiniCheck (EMNLP 2024, arXiv:2404.10774); AlignScore (ACL 2023, arXiv:2305.16739); MultiVerS (arXiv:2112.01640) |
| 9 | Calibrated confidence | Guo et al. 2017 temperature scaling (arXiv:1706.04599); Mohri & Hashimoto conformal factuality (ICML 2024, arXiv:2402.10978); Kadavath et al. (arXiv:2207.05221); Xiong et al. (ICLR 2024, arXiv:2306.13063); Farquhar et al. semantic entropy (Nature 2024) |
| 10 | Sandboxed code analysis | CodeAct (ICML 2024, arXiv:2402.01030); Biomni (bioRxiv 2025); BixBench (arXiv:2503.00096); Data Interpreter (arXiv:2402.18679) |
| 11 | SBML model calibration | COPASI (Bioinformatics 2006); basico (JOSS 2023); profile likelihood (Raue et al., Bioinformatics 2009); pyPESTO (2023); Hass et al. benchmarks (2019) |
| 12 | Structure and chemistry | AlphaFold (Nature 2021); AlphaFold DB (NAR 2022, 2024); RCSB PDB (NAR 2023); DiffDock (ICLR 2023); MolMIM (arXiv:2208.09016) |
| 13 | Background jobs | LangGraph persistence docs; living systematic reviews (Elliott et al. 2014, 2017) |
| 15 | Human in the loop | CLAM (arXiv:2212.07769); Clarify When Necessary (arXiv:2311.09469); Aliannejadi et al. (SIGIR 2019); Magentic-UI (arXiv:2507.22358) |
| 16 | Project memory | Zep/Graphiti (arXiv:2501.13956); Mem0 (arXiv:2504.19413); HippoRAG (NeurIPS 2024, arXiv:2405.14831); Generative Agents (UIST 2023); MemGPT (arXiv:2310.08560); A-MEM (arXiv:2502.12110) |
| 17 | MCP server and A2A | MCP specification 2025-11-25 and 2026-07-28; A2A v1.0.0; Hou et al. MCP security landscape (arXiv:2503.23278) |
| 18 | Tool retrieval | ToolLLM (ICLR 2024); Gorilla (NeurIPS 2024); ToolUniverse (arXiv:2509.23426); Re-Invoke (arXiv:2408.01875); Toolshed (arXiv:2410.14594); ToolRet (ACL 2025) |
| 19 | Tool reliability | Circuit breaker (Fowler 2014, after Nygard); API-Bank (EMNLP 2023); ToolEmu (ICLR 2024); ToolSword (ACL 2024) |
| 20 | New data sources | GWAS Catalog (NAR 2023/2025; REST v2); GTEx (Science 2020); STRING (NAR 2023/2025); Reactome (NAR 2024); PubChem (NAR 2025); DepMap (Cell 2017); bioRxiv API |
| (all) | Budget and guardrails | FrugalGPT (arXiv:2305.05176); BATS (arXiv:2511.17006); indirect prompt injection (Greshake et al., AISec 2023); Spotlighting (arXiv:2403.14720) |
