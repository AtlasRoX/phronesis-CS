# Phronesis agentic features: verified literature (agent loop and reasoning)

Every paper below was checked against its arXiv abstract or HTML page, an official proceedings page (PMLR, ICLR, ACL Anthology), or the bioRxiv PDF. Specific numbers (thresholds, prompts, counts) were read from the paper's full text, not just the abstract. Details marked "(confirm in PDF)" come from the paper body and were not re-read in this session. Check those before hard-coding them.

---

## 1. Dynamic plan-and-execute (DAG of tool calls, parallel execution, replanning)

- **An LLM Compiler for Parallel Function Calling**: Sehoon Kim et al., 2023 (v1 Dec 2023), ICML 2024. arXiv:2312.04511. https://arxiv.org/abs/2312.04511
  - Technique to borrow: this is the core design. A Planner LLM outputs numbered tasks forming a DAG. A dependent task refers to an earlier result with a placeholder (`$1`, `$2`). A Task Fetching Unit, which needs no LLM, sends out each task as soon as its dependencies resolve (greedy) and fills in the placeholders. An Executor runs ready tasks concurrently. For dynamic replanning, the Executor's results go back to the Planner, which emits a new task set, and this repeats until the final answer. Reported speedups over ReAct are 1.8x on HotpotQA, 3.74x on Movie Recommendation and 2.15x on ParallelQA (GPT). This maps directly onto parallel calls to EuropePMC, OpenTargets, ChEMBL and ClinicalTrials.gov.
  - Verified via: https://arxiv.org/abs/2312.04511 and https://arxiv.org/html/2312.04511

- **ReWOO: Decoupling Reasoning from Observations for Efficient Augmented Language Models**: Binfeng Xu et al., 2023, arXiv preprint (no venue listed). arXiv:2305.18323. https://arxiv.org/abs/2305.18323
  - Technique to borrow: plan the whole tool chain up front, without waiting for observations. Planner, Worker and Solver are separate modules, with evidence variables `#E1`, `#E2` (confirm in PDF). Reported results are about 5x token efficiency and +4% accuracy on HotpotQA. It also holds up when tools fail, and planning can be offloaded to a smaller model. Use it as the low-cost path when the query is predictable.
  - Verified via: https://arxiv.org/abs/2305.18323

- **Plan-and-Solve Prompting: Improving Zero-Shot Chain-of-Thought Reasoning by Large Language Models**: Lei Wang et al., 2023, ACL 2023. arXiv:2305.04091. https://arxiv.org/abs/2305.04091
  - Technique to borrow: the zero-shot planning trigger for the planner system prompt. PS: "Let's first understand the problem and devise a plan to solve the problem. Then, let's carry out the plan and solve the problem step by step." PS+ adds "extract relevant variables and their corresponding numerals" and "calculate intermediate variables (pay attention to correct numerical calculation and commonsense)".
  - Verified via: https://arxiv.org/html/2305.04091

- **ReAct: Synergizing Reasoning and Acting in Language Models**: Shunyu Yao et al., 2022, ICLR 2023. arXiv:2210.03629. https://arxiv.org/abs/2210.03629
  - Technique to borrow: an interleaved Thought, Action, Observation loop. Use it as the fallback executor for steps whose next action depends on the last observation, and as the baseline that LLMCompiler and ReWOO are measured against. It cuts hallucination and error propagation compared with chain-of-thought (CoT) on HotpotQA and FEVER.
  - Verified via: https://arxiv.org/abs/2210.03629

---

## 2. Gap-driven iterative re-retrieval (find unanswered sub-questions after a draft, retrieve again within a budget)

- **Active Retrieval Augmented Generation (FLARE)**: Zhengbao Jiang et al., 2023, EMNLP 2023. arXiv:2305.06983. https://arxiv.org/abs/2305.06983
  - Technique to borrow: forward-looking active retrieval. The model generates a tentative next sentence. If that sentence contains any low-confidence tokens (token probability below a threshold theta), the sentence is used as the query, or the low-confidence spans are masked or turned into questions (confirm in PDF). The system then retrieves and regenerates the sentence. Apply this per draft sentence to spot "gaps".
  - Verified via: https://arxiv.org/abs/2305.06983

- **Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection**: Akari Asai et al., 2023, ICLR 2024 (oral). arXiv:2310.11511. https://arxiv.org/abs/2310.11511
  - Technique to borrow: the reflection-token rubric, used as an LLM-judge schema without fine-tuning.
    - `Retrieve` = {yes, no, continue}
    - `IsRel` = {relevant, irrelevant}
    - `IsSup` = {fully, partially, no support}
    - `IsUse` = {1..5}
    - Segment score = p(segment) + sum of w_G * s_G over the critique tokens. Default weights are IsRel 1.0, IsSup 1.0 and IsUse 0.5.
    - A hard filter drops segments rated "no support".
    - A "partially/no support" verdict on a claim means a gap, which triggers another retrieval.
  - Verified via: https://arxiv.org/html/2310.11511 and https://proceedings.iclr.cc/paper_files/paper/2024/hash/25f7be9694d7b32d5cc670927b8091e1-Abstract-Conference.html

- **Corrective Retrieval Augmented Generation (CRAG)**: Shi-Qi Yan et al., 2024, arXiv preprint (no venue listed). arXiv:2401.15884. https://arxiv.org/abs/2401.15884
  - Technique to borrow: a three-way action rule driven by a retrieval evaluator. The evaluator is a fine-tuned T5-large that scores each document from -1 to 1, compared against an upper and a lower threshold.
    - **Correct:** any document scores above the upper threshold. Refine the retrieved documents.
    - **Incorrect:** every document scores below the lower threshold. Discard them and fall back to web search.
    - **Ambiguous:** otherwise. Combine both sources.
    - Example thresholds: PopQA (0.59, -0.99); PubHealth (0.5, -0.91).
    - Decompose-then-recompose: split documents into strips, filter strips scoring below -0.5, keep the top 5, and concatenate them in order.
  - In Phronesis, swap web search for a reformulated EuropePMC query.
  - Verified via: https://arxiv.org/html/2401.15884

- **Language agents achieve superhuman synthesis of scientific knowledge (PaperQA2)**: Michael D. Skarlinski et al., 2024, arXiv preprint. arXiv:2409.13740. https://arxiv.org/abs/2409.13740
  - Technique to borrow: an agentic literature loop with four tools.
    - **Paper Search:** a keyword query returning 12 papers by default, then chunked and embedded.
    - **Gather Evidence:** dense top-k of 30 chunks, followed by RCS (reranking and contextual summarization). An LLM writes a short query-specific summary of each chunk and scores its relevance, and chunks are reranked by that score.
    - **Citation Traversal:** one hop to citing and cited papers, starting from papers whose summary scored 8 or higher, capped at 12 papers.
    - **Generate Answer:** uses the top 15 summaries by default. The authors found 5 gave the best accuracy.
  - The agent keeps calling Search, Gather and Traverse until it judges the evidence sufficient. This is the template for a budgeted re-retrieval loop over EuropePMC.
  - Verified via: https://arxiv.org/html/2409.13740

- Also verified, as a supporting reference: **Interleaving Retrieval with Chain-of-Thought Reasoning for Knowledge-Intensive Multi-Step Questions (IRCoT)**: Harsh Trivedi et al., 2022, ACL 2023. arXiv:2212.10509. https://arxiv.org/abs/2212.10509
  - Technique to borrow: each new CoT sentence becomes the next retrieval query, and the retrieved passages feed the next reasoning step. This gives a simple per-step re-retrieval baseline.
  - Verified via: https://arxiv.org/abs/2212.10509

---

## 3. Question decomposition

- **Least-to-Most Prompting Enables Complex Reasoning in Large Language Models**: Denny Zhou et al., 2022, ICLR 2023. arXiv:2205.10625. https://arxiv.org/abs/2205.10625
  - Technique to borrow: two stages. First decompose the problem into an ordered list of simpler subproblems. Then solve them in order, adding each answer to the context for the next one. Use this for sequential biomedical chains, for example target, then pathway, then compound, then trial.
  - Verified via: https://arxiv.org/abs/2205.10625

- **Measuring and Narrowing the Compositionality Gap in Language Models (Self-Ask)**: Ofir Press et al., 2022, Findings of EMNLP 2023. arXiv:2210.03350. https://arxiv.org/abs/2210.03350
  - Technique to borrow: the model explicitly asks and answers follow-up questions before giving the final answer. The scaffold is "Are follow up questions needed here: / Follow up: / Intermediate answer: / So the final answer is:" (confirm in PDF). Because the follow-ups are explicit, each "Follow up:" can go to a search tool, here EuropePMC or OpenTargets.
  - Verified via: https://arxiv.org/abs/2210.03350

- **Decomposed Prompting: A Modular Approach for Solving Complex Tasks**: Tushar Khot et al., 2022, ICLR 2023. arXiv:2210.02406. https://arxiv.org/abs/2210.02406
  - Technique to borrow: a decomposer prompt sends sub-tasks to a library of sub-task handlers. Each handler can be another prompt, a further decomposition, a trained model or a symbolic function. Long inputs can be reduced recursively. This matches routing sub-questions to typed Phronesis tools (ChEMBL lookup, COPASI simulation, Scanpy analysis).
  - Verified via: https://arxiv.org/abs/2210.02406

---

## 4. Multi-agent topologies (supervisor, swarm/handoffs, debate)

- **Improving Factuality and Reasoning in Language Models through Multiagent Debate**: Yilun Du et al., 2023, ICML 2024 (PMLR 235:11733-11763). arXiv:2305.14325. https://arxiv.org/abs/2305.14325
  - Technique to borrow: the debate topology. By default, 3 agents debate for 2 rounds. In round 2 and later each agent sees: "These are the solutions to the problem from other agents: ... Using the opinion of other agents as additional advice, can you give an updated response ...". Rounds continue until the agents converge on a common answer. The method works with black-box models and uses the same prompts across tasks.
  - Verified via: https://arxiv.org/html/2305.14325 and https://proceedings.mlr.press/v235/du24e.html

- **Magentic-One: A Generalist Multi-Agent System for Solving Complex Tasks**: Adam Fourney et al., 2024, arXiv preprint. arXiv:2411.04468. https://arxiv.org/abs/2411.04468
  - Technique to borrow: the supervisor pattern with two ledgers.
    - **Outer loop, Task Ledger:** given or verified facts, facts to look up, facts to derive, educated guesses, and a plan.
    - **Inner loop, Progress Ledger:** answers five questions each step: (1) is the request satisfied, (2) are we looping, (3) are we making progress, (4) which agent speaks next, (5) what instruction to give it.
    - **Stall counter:** goes up on a loop or no progress. Above a threshold of 2, the orchestrator reflects, updates the Task Ledger and replans, and all agents reset their context.
  - Verified via: https://arxiv.org/html/2411.04468

- **AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation**: Qingyun Wu et al., 2023, COLM 2024 (per Microsoft Research and AG2 listings; arXiv shows no journal-ref). arXiv:2308.08155. https://arxiv.org/abs/2308.08155
  - Technique to borrow: "conversable" agents that combine an LLM, tools and human input, with interaction patterns defined in code and natural language (group chat with a speaker selector, two-agent chats). This is the reference design for swarm and handoff-style topologies and human-proxy agents.
  - Verified via: https://arxiv.org/abs/2308.08155 and https://www.microsoft.com/en-us/research/publication/autogen-enabling-next-gen-llm-applications-via-multi-agent-conversation-framework/

- **MetaGPT: Meta Programming for A Multi-Agent Collaborative Framework**: Sirui Hong et al., 2023, ICLR 2024 (oral). arXiv:2308.00352. https://arxiv.org/abs/2308.00352
  - Technique to borrow: Standardized Operating Procedures (SOPs) written into role prompts, in an assembly-line split where each role produces a structured intermediate artifact that the next role checks. The paper says this reduces cascading hallucination. Use it for fixed roles such as Literature Analyst, Target Assessor and Modeler, with schema-validated handoffs.
  - Verified via: https://arxiv.org/abs/2308.00352 and https://proceedings.iclr.cc/paper_files/paper/2024/hash/6507b115562bb0a305f1958ccc87355a-Abstract-Conference.html

- Also verified, optional: **More Agents Is All You Need**: Junyou Li et al., 2024, TMLR. arXiv:2402.05120. https://arxiv.org/abs/2402.05120
  - Technique to borrow: sampling and voting ("Agent Forest"). Sample N agent answers and take a majority vote. Gains grow with N and with task difficulty, and the method works alongside debate or CoT.
  - Verified via: https://arxiv.org/abs/2402.05120

---

## 5. Budget and stopping control (token, cost and latency budgets; adaptive compute)

- **Budget-Aware Tool-Use Enables Effective Agent Scaling (BATS)**: Tengxiao Liu et al., 2025, arXiv preprint. arXiv:2511.17006. https://arxiv.org/abs/2511.17006
  - Technique to borrow:
    - **Budget Tracker:** a `<budget>` block appended after every tool response, for example `Query Budget Used/Remaining`, `URL Budget Used/Remaining`, plus "Make the best use of the available resources". It defines tiers: HIGH at 70% or more remaining, MEDIUM 30-70%, LOW 10-30%, CRITICAL below 10%.
    - **Self-verification step:** labels each constraint satisfied, contradicted or unverifiable, then decides SUCCESS, CONTINUE (dig deeper) or PIVOT (on a contradiction or when budget is insufficient), and replaces the raw trajectory with a summary.
    - **Unified cost:** C = c_token + sum over tools of c_i * P_i.
  - Verified via: https://arxiv.org/html/2511.17006v1

- **FrugalGPT: How to Use Large Language Models While Reducing Cost and Improving Performance**: Lingjiao Chen et al., 2023, arXiv preprint (no venue found). arXiv:2305.05176. https://arxiv.org/abs/2305.05176
  - Technique to borrow: an LLM cascade. Query an ordered list of models. A learned scorer g(q, a) in [0, 1] (DistilBERT regression in the paper) accepts the answer at stage i if g > tau_i, otherwise the next model is called. The list and thresholds are optimized to maximize expected reward subject to expected cost staying within budget b. Example thresholds: GPT-J 0.96, J1-L 0.37, with GPT-4 as the final fallback. Reported up to 98% cost reduction at GPT-4 accuracy.
  - Verified via: https://arxiv.org/html/2305.05176

- **Token-Budget-Aware LLM Reasoning (TALE)**: Tingxu Han et al., 2024, Findings of ACL 2025. arXiv:2412.18547. DOI 10.18653/v1/2025.findings-acl.1274. https://arxiv.org/abs/2412.18547
  - Technique to borrow: estimate a per-query token budget from the problem's complexity and state it in the prompt, for example "use less than N tokens". This shortens reasoning with a modest accuracy cost. Use it to set per-node reasoning budgets inside the plan.
  - Verified via: https://arxiv.org/abs/2412.18547 and https://aclanthology.org/2025.findings-acl.1274/

- **Adaptive-RAG: Learning to Adapt Retrieval-Augmented Large Language Models through Question Complexity**: Soyeong Jeong et al., 2024, NAACL 2024. arXiv:2403.14403. https://arxiv.org/abs/2403.14403
  - Technique to borrow: a small classifier predicts query complexity and routes the query to one of three strategies: no retrieval, single-step RAG, or iterative multi-step RAG. Training labels are collected automatically. Use this as the entry gate that picks between the quick-answer path, the plan-and-execute path and the deep-research path.
  - Verified via: https://arxiv.org/abs/2403.14403

---

## 6. Hypothesis-to-experiment loop

- **Towards an AI co-scientist** (v1 title; v2 retitled "Accelerating scientific discovery with Co-Scientist"): Juraj Gottweis et al., 2025. arXiv:2502.18864. The arXiv comments note Nature (2026), DOI 10.1038/s41586-026-10644-y. https://arxiv.org/abs/2502.18864
  - Technique to borrow:
    - A Supervisor runs an asynchronous queue over seven agents: Generation, Reflection (peer review, optionally with web search), Ranking, Proximity (similarity graph for dedup and pairing), Evolution, Meta-review, and the Supervisor itself.
    - Ranking is an Elo tournament. New hypotheses start at Elo 1200. Top-ranked pairs get multi-turn simulated debates and lower-ranked pairs get single-turn comparisons.
    - Meta-review feedback is appended to the other agents' prompts. This gives test-time improvement without fine-tuning.
  - Verified via: https://arxiv.org/abs/2502.18864 and https://arxiv.org/html/2502.18864v1

- **BioDiscoveryAgent: An AI Agent for Designing Genetic Perturbation Experiments**: Yusuf Roohani et al., 2024, ICLR 2025. arXiv:2405.17631. https://arxiv.org/abs/2405.17631
  - Technique to borrow: a closed-loop experiment design.
    - Each round the prompt holds the research problem, the measurement definition, results from previous rounds and an exclusion list ("Do not include any genes from this prompt (since they're already tested)").
    - The agent returns a Reflection, a Research Plan (at most 5 sentences) and a Solution (a batch of 128 genes, or 32 for small screens). Invalid or repeated genes trigger re-prompting.
    - A critic LLM can replace genes in the batch.
    - Tools: PubMed search (summarizes the top 5 papers) and Reactome enrichment over hit genes.
    - Evaluated over 5 rounds.
  - Verified via: https://arxiv.org/html/2405.17631 and https://mlanthology.org/iclr/2025/roohani2025iclr-biodiscoveryagent

- **Robin: A multi-agent system for automating scientific discovery**: Ali Essam Ghareeb et al., 2025, arXiv preprint (FutureHouse). arXiv:2505.13400. https://arxiv.org/abs/2505.13400
  - Technique to borrow: a lab-in-the-loop pipeline.
    - Crow, a PaperQA2-based concise literature agent, proposes 10 candidate disease mechanisms and writes assay reports, which are ranked.
    - Robin proposes 30 candidates, and Falcon, the deep-report agent, writes a report on each.
    - Ranking uses an LLM-judge pairwise tournament: full round robin up to 25 items, otherwise 300 random pairs, fitted with a Bradley-Terry-Luce model.
    - Finch, the Jupyter data-analysis agent, runs up to 10 independent analysis trajectories, and a meta-analysis forms a consensus.
    - The resulting insights seed the next hypothesis cycle.
  - Verified via: https://arxiv.org/html/2505.13400

- **Large Language Models for Automated Open-domain Scientific Hypotheses Discovery**: Zonglin Yang et al., 2023, Findings of ACL 2024. arXiv:2309.02726. https://arxiv.org/abs/2309.02726
  - Technique to borrow: a multi-module hypothesis generator with three feedback mechanisms that critique and refine hypotheses for validity, novelty and helpfulness (confirm in PDF). Use these as the scoring axes in the Reflection step.
  - Verified via: https://arxiv.org/abs/2309.02726

---

## 7. Adversarial counter-evidence and contradiction search

- **PaperQA2 / ContraCrow**, from "Language agents achieve superhuman synthesis of scientific knowledge": Skarlinski et al., 2024. arXiv:2409.13740. https://arxiv.org/abs/2409.13740
  - Technique to borrow: the ContraCrow pipeline.
    - An LLM extracts claims from each section chunk of a paper.
    - A filter LLM scores each claim 0-10 for quality and generalizability, and only claims scoring 8 or higher are kept.
    - Each claim goes to PaperQA2 with a contradiction prompt that searches the literature for opposing evidence. The answer is on an 11-point Likert scale mapped to 0-10, with "lack of evidence" scored 5.
    - A claim is flagged as contradicted at a score of 8 or higher.
    - Results: 2.34 ± 1.99 contradictions per biology paper, 70% validated by experts.
  - Verified via: https://arxiv.org/html/2409.13740

- **CRITIC: Large Language Models Can Self-Correct with Tool-Interactive Critiquing**: Zhibin Gou et al., 2023, ICLR 2024. arXiv:2305.11738. https://arxiv.org/abs/2305.11738
  - Technique to borrow: a verify-then-correct loop. Produce an initial output, call external tools (search, code interpreter) to check specific claims, write a critique, revise, and repeat until the critique passes or an iteration cap is reached. The paper's point is that critique grounded in external tools is what makes self-correction work.
  - Verified via: https://arxiv.org/abs/2305.11738

- **Chain-of-Verification Reduces Hallucination in Large Language Models (CoVe)**: Shehzaad Dhuliawala et al., 2023, Findings of ACL 2024. arXiv:2309.11495. DOI 10.18653/v1/2024.findings-acl.212. https://arxiv.org/abs/2309.11495
  - Technique to borrow: (1) write a draft, (2) plan verification questions, (3) answer each one independently. In the factored variant the verifier cannot see the draft, which avoids copying its errors. (4) Write the final revised answer. Use step (3) with "find evidence against X" queries as the counter-evidence pass.
  - Verified via: https://arxiv.org/abs/2309.11495 and https://aclanthology.org/2024.findings-acl.212/

- **SciFact-Open: Towards open-domain scientific claim verification**: David Wadden et al., 2022, Findings of EMNLP 2022. arXiv:2210.13777. https://arxiv.org/abs/2210.13777
  - Technique to borrow: an evaluation set and label scheme for verifying claims against about 500K abstracts, with SUPPORT/REFUTE labels and rationales. Systems built on small corpora lose 15 or more F1 at this scale. Use it to benchmark the contradiction search and to model "supports only a special case" outcomes.
  - Verified via: https://arxiv.org/abs/2210.13777

---

## 10. Sandboxed code-execution agent for data analysis

- **Executable Code Actions Elicit Better LLM Agents (CodeAct)**: Xingyao Wang et al., 2024, ICML 2024. arXiv:2402.01030. https://arxiv.org/abs/2402.01030
  - Technique to borrow: Python code as the single action space instead of JSON tool calls. The agent emits code, the interpreter runs it, and stdout or errors come back as the observation over multiple turns. The agent self-debugs from these observations. Up to 20% higher success than JSON or text actions across 17 LLMs.
  - Verified via: https://arxiv.org/abs/2402.01030

- **Biomni: A General-Purpose Biomedical AI Agent**: Kexin Huang et al., 2025, bioRxiv preprint (posted 2 Jun 2025). DOI 10.1101/2025.05.30.656746. https://doi.org/10.1101/2025.05.30.656746
  - Technique to borrow: Biomni-A1 is built on CodeAct.
    - It first writes a numbered-list plan and keeps track of progress against it.
    - A separate prompt-based LLM retriever picks the relevant subset of tools, databases and libraries, which keeps the context short.
    - It then generates code, runs it in Python, R or Bash, and feeds the observation back until it converges.
    - Biomni-E1 contains 150 tools, 105 software packages and 59 databases. Each database is wrapped as one unified query function.
    - An ablation that swaps code-based planning for ReAct chaining performs worse.
  - Verified via: bioRxiv PDF (https://cs.stanford.edu/people/jure/pubs/biomni-biorxiv25.pdf) and https://pubmed.ncbi.nlm.nih.gov/40501924/

- **BixBench: a Comprehensive Benchmark for LLM-based Agents in Computational Biology**: Ludovico Mitchener et al., 2025, arXiv preprint (FutureHouse). arXiv:2503.00096. https://arxiv.org/abs/2503.00096
  - Technique to borrow: the notebook-sandbox harness and the evaluation.
    - The agent runs in Docker (`BixBench-env`, with Python, R and Bash bioinformatics packages) and has 3 tools: `edit_cell` (edit and run a Jupyter cell, which reruns the notebook), `list_workdir` and `submit_answer`. R and Bash run through `%%R` and `%%bash`.
    - The benchmark has 61 capsules and 205 open-answer questions.
    - Scoring: an LLM judge grades open answers 0/1. The multiple-choice variant includes an "Insufficient information" refusal option and takes a majority vote over 5 runs.
  - Verified via: https://arxiv.org/html/2503.00096

- **Data Interpreter: An LLM Agent For Data Science**: Sirui Hong et al., 2024, arXiv preprint. arXiv:2402.18679. https://arxiv.org/abs/2402.18679
  - Technique to borrow: hierarchical graph modeling. A data-science task becomes a task graph of subproblems that is refined as dependencies change. Programmable node generation refines and verifies the code for each node. Reported results: InfiAgent-DABench 75.9% to 94.9%. A good fit for multi-step Scanpy pipelines.
  - Verified via: https://arxiv.org/abs/2402.18679

---

## 15. Human-in-the-loop (clarifying questions, plan approval)

- **CLAM: Selective Clarification for Ambiguous Questions with Generative Language Models**: Lorenz Kuhn et al., 2022, arXiv preprint. arXiv:2212.07769. https://arxiv.org/abs/2212.07769
  - Technique to borrow: a selective clarification pipeline. (1) Prompt the LLM to decide whether the question is ambiguous. (2) Only if it is, generate a clarifying question for the user. (3) Answer using the clarified question.
  - Verified via: https://arxiv.org/abs/2212.07769

- **Clarify When Necessary: Resolving Ambiguity Through Interaction with LMs**: Michael J.Q. Zhang and Eunsol Choi, 2023, Findings of NAACL 2025 (pp. 5526-5543, per a citing paper and the author page). arXiv:2311.09469. https://arxiv.org/abs/2311.09469
  - Technique to borrow: INTENT-SIM. Simulate several plausible user intents for the query, estimate the entropy over those intents, and ask for clarification only when entropy is high. With clarification limited to 10% of examples, it roughly doubles the gain of random selection. This gives a principled threshold for when to ask.
  - Verified via: https://arxiv.org/abs/2311.09469 and https://www.cs.utexas.edu/~mjqzhang/

- **Asking Clarifying Questions in Open-Domain Information-Seeking Conversations**: Mohammad Aliannejadi et al., 2019, SIGIR 2019. arXiv:1907.06554. DOI 10.1145/3331184.3331265. https://arxiv.org/abs/1907.06554
  - Technique to borrow: a three-stage pipeline: question retrieval (candidate clarifying questions), question selection (conditioned on the query and earlier Q/A turns), then document retrieval using the answers. Use it to pick from a curated bank of biomedical clarifiers (species, tissue, disease subtype, endpoint).
  - Verified via: https://arxiv.org/abs/1907.06554

- **Magentic-UI: Towards Human-in-the-loop Agentic Systems**: Hussein Mozannar et al., 2025, arXiv preprint (Microsoft Research). arXiv:2507.22358. https://arxiv.org/abs/2507.22358
  - Technique to borrow:
    - **Co-planning:** the user edits the plan in a plan editor, or gives text feedback, before anything runs.
    - **Action guards:** the system asks for approval before potentially irreversible actions, and the user sets how often it asks.
    - Also co-tasking, multi-tasking and long-term memory. The paper says there are six mechanisms; only these five were confirmed on the abstract page.
    - It builds on Magentic-One and AutoGen.
  - This is the direct template for plan approval before expensive COPASI or Scanpy runs.
  - Verified via: https://arxiv.org/abs/2507.22358

---

### Venue notes

- arXiv-only, no peer-reviewed venue found: ReWOO, CRAG, PaperQA2, Magentic-One, Magentic-UI, FrugalGPT, BATS, Robin, BixBench, Data Interpreter, CLAM.
- bioRxiv preprint: Biomni.
- AI co-scientist: the arXiv comments field cites Nature (2026) and gives a related DOI. The Nature page itself was not opened.
