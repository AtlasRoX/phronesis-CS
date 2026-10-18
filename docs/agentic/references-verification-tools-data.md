# Phronesis agentic features: verified literature and API references (Verification, Memory, Tools, Data sources)

How I checked: titles, first authors, years and venues come from arXiv abstract pages, Crossref DOI metadata (api.crossref.org) or the venue's proceedings pages. I called the REST endpoints with curl on 2026-10-16 and the result of each call is noted. Anything I could not confirm is labelled UNVERIFIED or left out. The "technique we borrow" lines are design notes based on the papers and docs.

---

## 8. Semantic claim verification with NLI

- **Fact or Fiction: Verifying Scientific Claims.** Wadden, D. et al., 2020, EMNLP 2020. arXiv:2004.14974; DOI 10.18653/v1/2020.emnlp-main.609. https://arxiv.org/abs/2004.14974
  - Borrow: the SciFact task setup. For each (claim, abstract) pair, first pick rationale sentences, then predict a label in {SUPPORTS, REFUTES, NOINFO}. We use the same three labels for each claim and keep the rationale sentence IDs as evidence pointers. The dataset is at github.com/allenai/scifact.
  - Verified via: https://arxiv.org/abs/2004.14974
- **MultiVerS: Improving scientific claim verification with weak supervision and full-document context.** Wadden, D. et al., submitted 2021, published 2022, Findings of NAACL 2022. arXiv:2112.01640. https://arxiv.org/abs/2112.01640
  - Borrow: one Longformer model sees the whole abstract and predicts the label and the rationale together (multitask). We pass full EuropePMC abstracts and do not split them into sentences first. It can be weakly supervised on abstracts that have no rationale annotations.
  - Verified via: https://arxiv.org/abs/2112.01640
- **FActScore: Fine-grained Atomic Evaluation of Factual Precision in Long Form Text Generation.** Min, S. et al., 2023, EMNLP 2023 (main). arXiv:2305.14251. https://arxiv.org/abs/2305.14251
  - Borrow: split each answer into atomic facts with an LLM. Check each fact against retrieved passages. Score = the fraction of atomic facts that are supported. We use this as the per-answer "verified fraction" metric.
  - Verified via: https://arxiv.org/abs/2305.14251
- **AlignScore: Evaluating Factual Consistency with a Unified Alignment Function.** Zha, Y. et al., 2023, ACL 2023. arXiv:2305.16739. https://arxiv.org/abs/2305.16739
  - Borrow: a RoBERTa "alignment" function trained on many tasks. Split the context into chunks and the claim into sentences. For each claim sentence take the best-matching chunk, then average across sentences.
  - CPU checkpoints: HF `yzha/AlignScore`, files `AlignScore-base.ckpt` (RoBERTa-base, about 125M) and `AlignScore-large.ckpt` (RoBERTa-large, about 355M). MIT licence.
  - Verified via: https://arxiv.org/abs/2305.16739 and https://huggingface.co/yzha/AlignScore
- **MiniCheck: Efficient Fact-Checking of LLMs on Grounding Documents.** Tang, L. et al., 2024, EMNLP 2024. arXiv:2404.10774. https://arxiv.org/abs/2404.10774
  - Borrow: a small checker that labels each (document, sentence) pair as supported (1) or unsupported (0) and also returns a probability. Long documents are split into chunks and the maximum chunk score is taken. The model card gives no recommended threshold, so we must calibrate one ourselves (see feature 9).
  - CPU checkpoints (HF): `lytang/MiniCheck-RoBERTa-Large` (0.4B), `lytang/MiniCheck-DeBERTa-v3-Large` (0.4B), `lytang/MiniCheck-Flan-T5-Large` (base google/flan-t5-large, under 1B, MIT). The larger option is `bespokelabs/Bespoke-MiniCheck-7B` (7B, GPU).
  - Verified via: https://arxiv.org/abs/2404.10774 and https://huggingface.co/lytang/MiniCheck-Flan-T5-Large
- **SummaC: Re-Visiting NLI-based Models for Inconsistency Detection in Summarization.** Laban, P. et al., submitted 2021, published 2022, TACL. arXiv:2111.09525. https://arxiv.org/abs/2111.09525
  - Borrow: run NLI at sentence level. Build a matrix of entailment scores (source sentences × claim sentences). SummaC-ZS takes the maximum over source sentences for each claim sentence, then averages over claim sentences. SummaC-Conv learns a 1-D convolution over a histogram of the scores. This is the zero-shot baseline we can run with any NLI cross-encoder.
  - Verified via: https://arxiv.org/abs/2111.09525
- **Generic NLI cross-encoder (not a paper, model card only):** HF `cross-encoder/nli-deberta-v3-base`. Trained on SNLI and MultiNLI, Apache-2.0. Output label order is **0 = contradiction, 1 = entailment, 2 = neutral**. Hard-code this mapping in the code, because other NLI models order their labels differently. The model card does not mention xsmall, small or large siblings, so check those IDs before using them.
  - Verified via: https://huggingface.co/cross-encoder/nli-deberta-v3-base

## 9. Calibrated per-claim confidence

- **Language Models (Mostly) Know What They Know.** Kadavath, S. et al., 2022, arXiv preprint (Anthropic). arXiv:2207.05221. https://arxiv.org/abs/2207.05221
  - Borrow: self-evaluation with P(True). The model proposes an answer, then is asked whether the answer is True or False, and we read the probability of "True" as the confidence. The paper also trains a P(IK) ("I know") head.
  - Verified via: https://arxiv.org/abs/2207.05221
- **Can LLMs Express Their Uncertainty? An Empirical Evaluation of Confidence Elicitation in LLMs.** Xiong, M. et al., 2023, ICLR 2024. arXiv:2306.13063. https://arxiv.org/abs/2306.13063
  - Borrow: a three-part recipe of prompting for a stated confidence, sampling several answers, and combining them by consistency. Report ECE and AUROC. We use the stated confidence combined with sample agreement where we can only reach the LLM through an API (NIM) and have no logits.
  - Verified via: https://arxiv.org/abs/2306.13063
- **Detecting hallucinations in large language models using semantic entropy.** Farquhar, S. et al., 2024, *Nature* 630(8017):625–630. DOI 10.1038/s41586-024-07421-0. https://doi.org/10.1038/s41586-024-07421-0
  - Borrow: sample N answers and group them into meaning clusters using bidirectional entailment (A entails B and B entails A, judged by an NLI model or LLM). Compute the entropy over the clusters. High entropy flags a likely confabulation. We can reuse the feature-8 NLI cross-encoder for the clustering.
  - Verified via: https://api.crossref.org/works/10.1038/s41586-024-07421-0
- **On Calibration of Modern Neural Networks.** Guo, C. et al., 2017, ICML 2017. arXiv:1706.04599. https://arxiv.org/abs/1706.04599
  - Borrow: temperature scaling, which fits one scalar T on a held-out set by minimising NLL and then uses softmax(z/T). Measure with ECE (equal-width bins) and reliability diagrams. We apply it to the logits from the NLI/MiniCheck verifier.
  - Verified via: https://arxiv.org/abs/1706.04599
- **Conformal Language Modeling.** Quach, V. et al., 2023, ICLR 2024. arXiv:2306.10193. https://arxiv.org/abs/2306.10193
  - Borrow: sample candidates, reject low-quality ones, and stop once a calibrated rule is met, so that the returned set contains an acceptable answer with probability at least 1−ε. It can also pick out individual components (sentences) that are confidently correct.
  - Verified via: https://arxiv.org/abs/2306.10193
- **Language Models with Conformal Factuality Guarantees.** Mohri, C. & Hashimoto, T., 2024, ICML 2024, PMLR 235:36029–36047. arXiv:2402.10978. https://proceedings.mlr.press/v235/mohri24a.html
  - Borrow: split the output into sub-claims and score each one. Compute a split-conformal threshold on a calibration set, then drop sub-claims below it ("back-off"). This gives a guarantee that the remaining text is correct with probability at least 1−α (80–90% in the paper). This matches our per-claim pipeline from feature 8.
  - Verified via: https://arxiv.org/abs/2402.10978 and https://proceedings.mlr.press/v235/mohri24a.html

## 11. SBML model calibration, parameter estimation and identifiability

- **COPASI—a COmplex PAthway SImulator.** Hoops, S. et al., 2006, *Bioinformatics* 22(24):3067–3074. DOI 10.1093/bioinformatics/btl485. https://doi.org/10.1093/bioinformatics/btl485
  - Borrow: COPASI's parameter-estimation task, with its choice of local and global optimisers (e.g. Levenberg–Marquardt, particle swarm, evolutionary programming), and its sensitivity tasks. We drive these through basico.
  - Verified via: https://api.crossref.org/works/10.1093/bioinformatics/btl485
- **BASICO: A simplified Python interface to COPASI.** Bergmann, F. T. et al., 2023, *Journal of Open Source Software* 8(90):5553. DOI 10.21105/joss.05553. https://doi.org/10.21105/joss.05553
  - Borrow (functions confirmed in the basico API docs): `load_biomodel`, `run_time_course`, `get_parameters`. From `basico.task_parameterestimation`: `add_experiment`, `set_fit_parameters`, `run_parameter_estimation`, `get_fit_statistic`, `get_parameters_solution`. From `basico.task_sensitivities`: `run_sensitivities`. I found no built-in profile-likelihood helper in basico. Use pyPESTO for profiles, or write a loop that fixes one parameter at a time and re-runs `run_parameter_estimation`.
  - Verified via: https://api.crossref.org/works/10.21105/joss.05553 and https://basico.readthedocs.io/en/latest/API/basico.html
- **Structural and practical identifiability analysis of partially observed dynamical models by exploiting the profile likelihood.** Raue, A. et al., 2009, *Bioinformatics* 25(15):1923–1929. DOI 10.1093/bioinformatics/btp358. https://doi.org/10.1093/bioinformatics/btp358
  - Borrow: the profile likelihood PL(θ_i) = min over θ_{j≠i} of χ²(θ). The confidence interval is where PL rises less than the χ²(α, df) quantile above the optimum (3.84 for a pointwise 95% interval with df = 1). A profile that stays flat means the parameter is structurally non-identifiable. A profile that is open on one side means it is practically non-identifiable.
  - Verified via: https://api.crossref.org/works/10.1093/bioinformatics/btp358
- **pyPESTO: a modular and scalable tool for parameter estimation for dynamic models.** Schälte, Y. et al., 2023, *Bioinformatics* 39(11):btad711. DOI 10.1093/bioinformatics/btad711. https://doi.org/10.1093/bioinformatics/btad711
  - Borrow: multi-start optimisation, the `pypesto.profile` module for profiles, sampling for posterior uncertainty, and the PEtab problem format. Use it if we export the basico problem to PEtab.
  - Verified via: https://api.crossref.org/works/10.1093/bioinformatics/btad711
- **Benchmark problems for dynamic modeling of intracellular processes.** Hass, H. et al. (with Kreutz, C.), 2019, *Bioinformatics* 35(17):3073–3082. DOI 10.1093/bioinformatics/btz020. https://doi.org/10.1093/bioinformatics/btz020
  - Borrow: a set of curated ODE models with real data, which we use as regression tests for the calibration agent (does our pipeline recover published fits and identifiability results?).
  - Verified via: https://api.crossref.org/works/10.1093/bioinformatics/btz020

## 12. Structure and chemistry agents

- **Highly accurate protein structure prediction with AlphaFold.** Jumper, J. et al., 2021, *Nature* 596(7873):583–589. DOI 10.1038/s41586-021-03819-2. https://doi.org/10.1038/s41586-021-03819-2
  - Borrow: per-residue confidence pLDDT (0–100, with bands below 50, 50–70, 70–90 and above 90) and PAE. The agent should report both alongside any structure-based claim.
  - Verified via: https://api.crossref.org/works/10.1038/s41586-021-03819-2
- **AlphaFold Protein Structure Database: massively expanding the structural coverage of protein-sequence space with high-accuracy models.** Varadi, M. et al., 2022 (online Nov 2021), *Nucleic Acids Res.* 50(D1):D439–D444. DOI 10.1093/nar/gkab1061.
  - Verified via: https://api.crossref.org/works/10.1093/nar/gkab1061
- **AlphaFold Protein Structure Database in 2024: providing structure coverage for over 214 million protein sequences.** Varadi, M. et al., 2024, *Nucleic Acids Res.* 52(D1):D368–D375. DOI 10.1093/nar/gkad1011.
  - API: `GET https://alphafold.ebi.ac.uk/api/prediction/{UniProt accession}`. Live check on P04637 returned 200 with a JSON array containing `modelEntityId` "AF-P04637-F1", `toolUsed` and related fields. The response also links to PDB/mmCIF/PAE files.
  - Verified via: https://api.crossref.org/works/10.1093/nar/gkad1011 and a live call to https://alphafold.ebi.ac.uk/api/prediction/P04637
- **Evolutionary-scale prediction of atomic-level protein structure with a language model (ESMFold / ESM-2).** Lin, Z. et al., 2023, *Science* 379(6637):1123–1130. DOI 10.1126/science.ade2574.
  - NIM: `POST https://health.api.nvidia.com/v1/biology/nvidia/esmfold`. An unauthenticated call returned 401 "authorization missing", while a made-up path under the same host returned 404, so the route exists. I could not fetch an official docs page for the request schema, so the body field name is UNVERIFIED (commonly `{"sequence": "..."}`). Check it on build.nvidia.com/meta/esmfold before coding against it.
  - Verified via: https://api.crossref.org/works/10.1126/science.ade2574 and a live endpoint probe
- **DiffDock: Diffusion Steps, Twists, and Turns for Molecular Docking.** Corso, G. et al., 2022 (arXiv), ICLR 2023. arXiv:2210.01776. https://arxiv.org/abs/2210.01776
  - Borrow: a generative docking model that ranks poses with a confidence model. NIM: `POST https://health.api.nvidia.com/v1/biology/mit/diffdock` (OpenAPI path `/molecular-docking/diffdock/generate`). It needs a Bearer `NVIDIA_API_KEY`. Body fields: `protein` (PDB text), `ligand` (text), `ligand_file_type` (mol2, sdf or txt), `num_poses` (default 10, max 100), `time_divisions` (≤20), `steps` (≤18), `save_trajectory`, `skip_gen_conformer`, `is_staged`.
  - Verified via: https://arxiv.org/abs/2210.01776 and https://docs.api.nvidia.com/nim/reference/mit-diffdock-infer (live probe returned 401)
- **Improving Small Molecule Generation using Mutual Information Machine (MolMIM).** Reidenbach, D. et al., 2022 (v2 2023), MLDD workshop at ICLR 2023. arXiv:2208.09016. https://arxiv.org/abs/2208.09016
  - Borrow: generate molecules in latent space, steered by CMA-ES. NIM endpoints (self-hosted, all POST): `/generate`, `/embedding`, `/hidden`, `/decode`, `/sampling`. `/generate` takes `smi` (seed SMILES), `algorithm` ("CMA-ES" or "none"), `num_molecules` (1–100), `property_name` ("QED" or "plogP"), `min_similarity` (0–0.7). Hosted: `POST https://health.api.nvidia.com/v1/biology/nvidia/molmim/generate` (live probe returned 401, so the route exists).
  - Verified via: https://arxiv.org/abs/2208.09016 and https://docs.nvidia.com/nim/bionemo/molmim/latest/endpoints.html
- **RCSB Protein Data Bank (RCSB.org): delivery of experimentally-determined PDB structures alongside one million computed structure models of proteins from artificial intelligence/machine learning.** Burley, S. K. et al., 2023, *Nucleic Acids Res.* 51(D1):D488–D508. DOI 10.1093/nar/gkac1077.
  - API: Data API `GET https://data.rcsb.org/rest/v1/core/entry/{PDB_ID}` (live 200 for 4HHB). Search API `https://search.rcsb.org/rcsbsearch/v2/query?json=...` (live 200 for a full_text "TP53" query).
  - Verified via: https://api.crossref.org/works/10.1093/nar/gkac1077 and live calls

## 13. Durable long-running agent jobs and living literature monitoring

- **LangGraph Persistence (docs, not a paper).** https://docs.langchain.com/oss/python/langgraph/persistence
  - Borrow: checkpointers that save graph state for each `thread_id`, passed as `{"configurable": {"thread_id": ...}}`. Options are `InMemorySaver` (development only, lost on restart), `SqliteSaver` (local file) and `PostgresSaver`/`AsyncPostgresSaver` (production). `thread_id` must be 255 characters or fewer with Postgres. The checkpoints also support human-in-the-loop interrupts and resume. The old `/durable-execution` URL now loads this Persistence page (it has a `#durability-modes` anchor with no visible text), so cite the Persistence URL. Related page: https://docs.langchain.com/oss/python/langgraph/checkpointers
  - Verified via: https://docs.langchain.com/oss/python/langgraph/persistence
- **Living Systematic Reviews: An Emerging Opportunity to Narrow the Evidence-Practice Gap.** Elliott, J. H. et al., 2014, *PLoS Medicine* 11(2):e1001603. DOI 10.1371/journal.pmed.1001603.
  - Borrow: the idea of a review that is updated continuously as new evidence is published. This justifies scheduled EuropePMC and bioRxiv re-queries that update a saved project's claims.
  - Verified via: https://api.crossref.org/works/10.1371/journal.pmed.1001603
- **Living systematic review: 1. Introduction—the why, what, when, and how.** Elliott, J. H. et al., 2017, *J. Clin. Epidemiol.* 91:23–30. DOI 10.1016/j.jclinepi.2017.08.010.
  - Borrow: the operational rules. Search on a fixed, stated schedule (e.g. monthly). Fold new evidence in promptly. Say up front when the review is updated and when it stops being "living".
  - Verified via: https://api.crossref.org/works/10.1016/j.jclinepi.2017.08.010
- I did not verify a peer-reviewed paper on durable execution for LLM agents specifically, so none is listed.

## 16. Project-level and agent memory

- **MemGPT: Towards LLMs as Operating Systems.** Packer, C. et al., 2023, arXiv. arXiv:2310.08560. https://arxiv.org/abs/2310.08560
  - Borrow: memory split into tiers like an operating system. "Main context" is the working memory inside the prompt, and archival and recall storage live outside it. The agent moves information between tiers with function calls (it "pages" memory in and out).
  - Verified via: https://arxiv.org/abs/2310.08560
- **Generative Agents: Interactive Simulacra of Human Behavior.** Park, J. S. et al., 2023, UIST '23 (ACM), pp. 1–22. arXiv:2304.03442; DOI 10.1145/3586183.3606763.
  - Borrow: a memory stream ranked by a weighted sum of recency (exponential decay), importance (an LLM-rated score from 1 to 10) and relevance (embedding cosine). The agent also periodically writes "reflections" that summarise higher-level insights.
  - Verified via: https://arxiv.org/abs/2304.03442 and https://api.crossref.org/works/10.1145/3586183.3606763
- **Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory.** Chhikara, P. et al., 2025, arXiv. arXiv:2504.19413. https://arxiv.org/abs/2504.19413
  - Borrow: an extract-then-update loop. Pull candidate memories out of each exchange, then have an LLM choose ADD, UPDATE, DELETE or NOOP against similar existing memories. There is also a graph variant.
  - Verified via: https://arxiv.org/abs/2504.19413
- **Zep: A Temporal Knowledge Graph Architecture for Agent Memory.** Rasmussen, P. et al., 2025, arXiv. arXiv:2501.13956. https://arxiv.org/abs/2501.13956
  - Borrow: Graphiti's bi-temporal edges. Each fact records when it was true and when it was ingested. When a new fact contradicts an old one, the old fact is marked invalid rather than deleted. This suits project memory where findings get revised over time.
  - Verified via: https://arxiv.org/abs/2501.13956
- **HippoRAG: Neurobiologically Inspired Long-Term Memory for Large Language Models.** Gutiérrez, B. J. et al., 2024, NeurIPS 2024. arXiv:2405.14831. https://arxiv.org/abs/2405.14831
  - Borrow: an LLM extracts an open knowledge graph, and Personalized PageRank is run from the entities in the query to retrieve across multiple hops. This fits well with our PrimeKG graph.
  - Verified via: https://arxiv.org/abs/2405.14831
- **A-MEM: Agentic Memory for LLM Agents.** Xu, W. et al., 2025, NeurIPS 2025. arXiv:2502.12110. https://arxiv.org/abs/2502.12110
  - Borrow: memory notes in the Zettelkasten style, each with keywords, tags and links. The links are created and updated by the LLM, and the notes evolve as new memories arrive.
  - Verified via: https://arxiv.org/abs/2502.12110

## 17. MCP server and agent interoperability

- **Model Context Protocol specification (not a paper).** The current version is **2026-07-28**: https://modelcontextprotocol.io/specification/2026-07-28/
  - Note: this version uses per-request version negotiation through `_meta["io.modelcontextprotocol/protocolVersion"]` and the `MCP-Protocol-Version` header on Streamable HTTP, plus a required `server/discover` RPC. Revisions `2025-11-25` and earlier use the initialize-handshake model and have a backward-compatibility section. If we pin an older SDK, cite https://modelcontextprotocol.io/specification/2025-11-25/ instead.
  - Verified via: https://modelcontextprotocol.io/specification/versioning
- **Agent2Agent (A2A) Protocol specification (not a paper).** Latest released version is **1.0.0** (earlier: 0.3.0, 0.2.6, 0.1.0). The page says A2A has joined the Agentic AI Foundation. https://a2a-protocol.org/latest/specification/
  - Borrow: an Agent Card served at `https://{domain}/.well-known/agent-card.json`. Bindings: JSON-RPC, gRPC and HTTP+JSON/REST. Methods: `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask`, the push-notification config CRUD methods, and `GetExtendedAgentCard`. In v1.0 these names are PascalCase; the older slash names such as `message/send` came from versions before 1.0.
  - Verified via: https://a2a-protocol.org/latest/specification/
- **Model Context Protocol (MCP): Landscape, Security Threats, and Future Research Directions.** Hou, X. et al., 2025, arXiv. arXiv:2503.23278. https://arxiv.org/abs/2503.23278
  - Borrow: the paper's threat taxonomy across the MCP server lifecycle (creation, operation, update), covering risks such as tool poisoning, name collisions, and configuration drift or privilege persistence. We use it as a checklist when exposing Phronesis tools as an MCP server.
  - Verified via: https://arxiv.org/abs/2503.23278

## 18. Tool retrieval and selection from a large tool catalogue

- **ToolLLM: Facilitating Large Language Models to Master 16000+ Real-world APIs.** Qin, Y. et al., 2023 (arXiv), ICLR 2024. arXiv:2307.16789. https://proceedings.iclr.cc/paper_files/paper/2024/hash/28e50ee5b72e90b50e7196fde8ea260e-Abstract-Conference.html
  - Borrow: a neural API retriever that picks the top-k tools for each instruction, DFSDT (depth-first search over a decision tree, which lets the agent back off from failed tool paths), and ToolEval pass/win rates.
  - Verified via: https://arxiv.org/abs/2307.16789 and the ICLR proceedings link above
- **Gorilla: Large Language Model Connected with Massive APIs.** Patil, S. G. et al., 2023 (arXiv), NeurIPS 2024. arXiv:2305.15334. https://proceedings.neurips.cc/paper_files/paper/2024/file/e4c61f578ff07830f5c37378dd3ecb0d-Paper-Conference.pdf
  - Borrow: retriever-aware training, meaning the retrieved API documentation is put in the prompt so the model stays correct when the docs change. Also the AST sub-tree matching check for hallucinated API calls.
  - Verified via: https://arxiv.org/abs/2305.15334 and https://neurips.cc/virtual/2024/poster/93353
- **Democratizing AI scientists using ToolUniverse.** Gao, S. et al. (with Zitnik, M.), 2025, arXiv. arXiv:2509.23426. https://arxiv.org/abs/2509.23426
  - Borrow: a standard tool specification over more than 600 biomedical tools, with "Tool Finder" retrieval, and tools that can be refined or generated from natural-language descriptions. Code is at github.com/mims-harvard/ToolUniverse. It already wraps DepMap and NVIDIA NIM tools, which we can use as a reference implementation.
  - Verified via: https://arxiv.org/abs/2509.23426v1
- **Re-Invoke: Tool Invocation Rewriting for Zero-Shot Tool Retrieval.** Chen, Y. et al., 2024, Findings of EMNLP 2024. arXiv:2408.01875. https://arxiv.org/abs/2408.01875
  - Borrow: at indexing time, an LLM writes synthetic queries for each tool document. At query time, the user's intent is extracted and tools are ranked across intents. No training is needed.
  - Verified via: https://arxiv.org/abs/2408.01875
- **Toolshed: Scale Tool-Equipped Agents with Advanced RAG-Tool Fusion and Tool Knowledge Bases.** Lumer, E. et al., 2024, arXiv. arXiv:2410.14594. https://arxiv.org/abs/2410.14594
  - Borrow: three stages. Before retrieval, enrich each tool document (questions, keywords). During retrieval, rewrite and expand the query. After retrieval, rerank the results.
  - Verified via: https://arxiv.org/abs/2410.14594
- **Retrieval Models Aren't Tool-Savvy: Benchmarking Tool Retrieval for Large Language Models (ToolRet).** Shi, Z. et al., 2025, ACL 2025. arXiv:2503.01763. https://arxiv.org/abs/2503.01763
  - Borrow: an evaluation benchmark and method (code: github.com/mangopy/tool-retrieval-benchmark). Its finding is that generic IR embedders do badly on tool retrieval, so we should measure recall@k on our own catalogue.
  - Verified via: https://arxiv.org/abs/2503.01763

## 19. Tool reliability (retries, caching, circuit breakers)

- **Identifying the Risks of LM Agents with an LM-Emulated Sandbox (ToolEmu).** Ruan, Y. et al., 2023 (arXiv), ICLR 2024 (Spotlight). arXiv:2309.15817. https://arxiv.org/abs/2309.15817
  - Borrow: an LLM emulates tool execution, including adversarial edge cases, so the agent can be tested without calling the real API. An LLM safety evaluator scores the risk. We use this as a test harness for failure handling.
  - Verified via: https://arxiv.org/abs/2309.15817 and https://iclr.cc/virtual/2024/poster/19037
- **ToolSword: Unveiling Safety Issues of Large Language Models in Tool Learning Across Three Stages.** Ye, J. et al., 2024, ACL 2024 (Long Papers). arXiv:2402.10753. https://arxiv.org/abs/2402.10753
  - Borrow: test scenarios grouped by stage. Input stage: malicious queries and jailbreaks. Execution stage: noisy misdirection and risky cues. Output stage: harmful feedback and error conflicts. Noisy or erroneous tool output is the case most relevant to our retry and validation layer.
  - Verified via: https://arxiv.org/abs/2402.10753
- **API-Bank: A Comprehensive Benchmark for Tool-Augmented LLMs.** Li, M. et al., 2023, EMNLP 2023. arXiv:2304.08244. https://arxiv.org/abs/2304.08244
  - Borrow: an error taxonomy for API calls (no call, wrong API name, bad parameters, hallucinated response). Use it to label failures in our tool-call telemetry.
  - Verified via: https://arxiv.org/abs/2304.08244
- **Circuit Breaker (article, not a paper).** Fowler, M., 6 March 2014. https://martinfowler.com/bliki/CircuitBreaker.html
  - Borrow: three states. Closed: calls go through and failures are counted. Open: after a failure threshold, calls fail fast. Half-open: after a timeout, one trial call is allowed; success closes the breaker and failure re-opens it. Fowler credits Michael Nygard's *Release It!* for popularising the pattern. I did not separately verify the book's edition or ISBN.
  - Verified via: https://martinfowler.com/bliki/CircuitBreaker.html

## 20. New data sources (paper and live REST base URL)

- **GWAS Catalog.** Sollis, E. et al., 2023, "The NHGRI-EBI GWAS Catalog: knowledgebase and deposition resource", *Nucleic Acids Res.* 51(D1):D977–D985, DOI 10.1093/nar/gkac1010. Newer paper: Cerezo, M. et al., 2025, "The NHGRI-EBI GWAS Catalog: standards for reusability, sustainability and diversity", *Nucleic Acids Res.* 53(D1):D998–D1005, DOI 10.1093/nar/gkae1070. The Cerezo record is verified from search and abstract mirrors only; I did not check it on Crossref.
  - API: **use v2 only: `https://www.ebi.ac.uk/gwas/rest/api/v2`**. The legacy `https://www.ebi.ac.uk/gwas/rest/api/...` now returns **HTTP 410 Gone**, and the docs say "V1 was retired in August 2026". The summary-statistics API has also been retired and has no replacement. Live check: `GET /gwas/rest/api/v2/studies/GCST000854` returned 200. Reference: https://www.ebi.ac.uk/gwas/rest/api/v2/docs/reference. The docs also list an official GWAS Catalog MCP server.
  - Verified via: https://api.crossref.org/works/10.1093/nar/gkac1010, https://www.ebi.ac.uk/gwas/docs/programmatic-access/rest-api, and live calls
- **DepMap.** Tsherniak, A. et al., 2017, "Defining a Cancer Dependency Map", *Cell* 170(3):564–576.e16, DOI 10.1016/j.cell.2017.06.010. Arafeh, R. et al., 2025 (online Oct 2024), "The present and future of the Cancer Dependency Map", *Nat. Rev. Cancer* 25(1):59–73, DOI 10.1038/s41568-024-00763-x.
  - API: `https://depmap.org/portal/api/` exists, but scripted clients get a bot-verification ("DepMap — Verification") HTML page, including on `/portal/api/download/files`. A DepMap forum reply says all releases are published on Figshare (e.g. https://figshare.com/articles/dataset/DepMap_23Q2_Public/22765112), and Figshare has its own API. The safer plan is to download release CSVs (CRISPRGeneEffect etc.) from Figshare or the portal and cache them locally, rather than query per gene. The Sanger side, Cell Model Passports, has a separate documented JSON:API at `https://api.cellmodelpassports.sanger.ac.uk` (non-commercial use). Per-gene portal routes are UNVERIFIED.
  - Verified via: https://api.crossref.org/works/10.1016/j.cell.2017.06.010, https://api.crossref.org/works/10.1038/s41568-024-00763-x, live probe
- **GTEx.** GTEx Consortium, 2020, "The GTEx Consortium atlas of genetic regulatory effects across human tissues", *Science* 369(6509):1318–1330, DOI 10.1126/science.aaz1776.
  - API: `https://gtexportal.org/api/v2/`. Live check: `GET /api/v2/reference/gene?geneId=TP53` returned 200 with gencodeId ENSG00000141510.16.
  - Verified via: https://api.crossref.org/works/10.1126/science.aaz1776 and live call
- **STRING.** Szklarczyk, D. et al., 2023, "The STRING database in 2023: protein–protein association networks and functional enrichment analyses for any sequenced genome of interest", *Nucleic Acids Res.* 51(D1):D638–D646, DOI 10.1093/nar/gkac1000. Newer paper: Szklarczyk, D. et al., 2025, "The STRING database in 2025: protein networks with directionality of regulation", *Nucleic Acids Res.* 53(D1):D730–D737, DOI 10.1093/nar/gkae1113.
  - API: `https://string-db.org/api/{format}/{method}`. Live: `/api/json/version` returned 12.5 (stable address https://version-12-5.string-db.org). `/api/json/network?identifiers=TP53&species=9606` returned 200 with a combined `score` per edge. Pin the stable versioned address so results are reproducible.
  - Verified via: Crossref (both DOIs) and live calls
- **Reactome.** Milacic, M. et al., 2024, "The Reactome Pathway Knowledgebase 2024", *Nucleic Acids Res.* 52(D1):D672–D678, DOI 10.1093/nar/gkad1025.
  - API: `https://reactome.org/ContentService/`. Live: `/ContentService/data/database/version` returned 97.
  - Verified via: https://api.crossref.org/works/10.1093/nar/gkad1025 and live call
- **DrugBank 6.0.** Knox, C. et al., 2024, "DrugBank 6.0: the DrugBank Knowledgebase for 2024", *Nucleic Acids Res.* 52(D1):D1265–D1275, DOI 10.1093/nar/gkad976.
  - Licence: the full data and the API need a DrugBank account and licence (free academic licence for non-commercial use, paid for commercial use), so we cannot ship or redistribute DrugBank data by default. I did not re-check the licence terms page in this pass, so this is from background knowledge. Make DrugBank an opt-in, bring-your-own-key connector, and use ChEMBL and PubChem as the default sources.
  - Verified via: https://api.crossref.org/works/10.1093/nar/gkad976
- **PubChem.** Kim, S. et al., 2025, "PubChem 2025 update", *Nucleic Acids Res.* 53(D1):D1516–D1525, DOI 10.1093/nar/gkae1059.
  - API (PUG-REST): `https://pubchem.ncbi.nlm.nih.gov/rest/pug/<domain>/<namespace>/<identifiers>/<operation>/<output>`. Live: `/compound/name/aspirin/property/MolecularFormula,CanonicalSMILES/JSON` returned 200 with CID 2244. **Implementation note:** the response key is now `ConnectivitySMILES`, not `CanonicalSMILES`, so parsers must accept both names (or request `SMILES` / `ConnectivitySMILES` explicitly).
  - Verified via: https://api.crossref.org/works/10.1093/nar/gkae1059 and live call
- **bioRxiv / medRxiv API (no database paper verified).** `https://api.biorxiv.org/`
  - Endpoints: `/details/[server]/[interval]/[cursor]/[format]` (30 results per page), `/pubs/...` (100 per page; preprint-to-published links), `/publisher/...`, `/funder/...`, `/sum/...`, `/usage/...`. The interval can be `YYYY-MM-DD/YYYY-MM-DD`, `N` (most recent N items) or `Nd` (last N days). Live: `/details/biorxiv/2024-01-01/2024-01-02/0` returned 200 with `messages[0].total`. Page through results by increasing the cursor. This is the source for the living-review monitor in feature 13.
  - Verified via: https://api.biorxiv.org/ and live call

## Guardrails: prompt injection from retrieved content and the code sandbox

- **Not What You've Signed Up For: Compromising Real-World LLM-Integrated Applications with Indirect Prompt Injection.** Greshake, K. et al., 2023, AISec '23 (16th ACM Workshop on AI and Security), pp. 79–90. arXiv:2302.12173; DOI 10.1145/3605764.3623985.
  - Borrow: the threat model. Any text we retrieve (abstracts, web pages, tool outputs) can carry hidden instructions. Treat all of it as data, never as instructions, especially before passing it to the code sandbox or any tool that has side effects.
  - Verified via: https://arxiv.org/abs/2302.12173 and https://api.crossref.org/works/10.1145/3605764.3623985
- **Defending Against Indirect Prompt Injection Attacks With Spotlighting.** Hines, K. et al., 2024, arXiv (Microsoft). arXiv:2403.14720. https://arxiv.org/abs/2403.14720
  - Borrow: three transforms applied to untrusted text. (1) Delimiting: wrap it in markers. This is the weakest option because an attacker who knows the markers can get around them. (2) **Datamarking**: replace whitespace with a special token such as `ˆ` and tell the model that marked text is data. This cut ASR from about 50% to under 3% for summarisation on GPT-3.5. (3) Encoding: base64-encode the text. This gives the best ASR but hurts task quality on weaker models. Overall the paper reports ASR falling from over 50% to under 2%. Datamarking is cheap and model-agnostic, so it is our default.
  - Verified via: https://arxiv.org/html/2403.14720
- **StruQ: Defending Against Prompt Injection with Structured Queries.** Chen, S. et al., 2024, USENIX Security 2025. arXiv:2402.06363. https://arxiv.org/abs/2402.06363
  - Borrow: keep the prompt and the data in separate channels, using reserved delimiter tokens that are stripped from the data, and fine-tune the model to follow instructions only from the prompt channel. Fine-tuning is out of scope for NIM models, but the front-end idea still applies: separate channels and filter reserved tokens out of retrieved text.
  - Verified via: https://arxiv.org/abs/2402.06363

---

### Items left out or flagged

- MultiVerS checkpoints on HuggingFace: I did not verify a HuggingFace model ID for them, so none is given.
- NLI cross-encoder siblings (`cross-encoder/nli-deberta-v3-xsmall`, `-small`, `-large`): the base model card does not mention them, so check each ID before use.
- ESMFold NIM request body schema: the route was confirmed with a live 401/404 comparison, but I could not load the docs page.
- DepMap per-gene REST routes: the portal API is behind bot verification from scripted clients.
- No peer-reviewed paper on durable execution for agents was verified. The LangGraph docs are the reference.
- DrugBank licence details come from background knowledge and were not re-checked.
