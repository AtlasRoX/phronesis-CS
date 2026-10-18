# Auto routing: choosing the architecture per question

`framework: "auto"` lets Phronesis choose how to answer each question instead of making the user pick one of the
five orchestration architectures. Auto is the default in the web app.

## Why a router, and why this one

The evidence on multi-agent systems says the right architecture depends on the task, and that more agents are not
automatically better:

- **Task shape predicts the best architecture.** Across 260 agent configurations, coordination helped decomposable
  tasks (up to +81%) and hurt strictly sequential ones (down to −70%). A model built on task features (sequential
  dependence, tool density) picked the best architecture for 87% of held-out cases. Centralized coordination
  contained error amplification far better than independent peers
  ([Towards a Science of Scaling Agent Systems, 2025](https://arxiv.org/abs/2512.08296)).
- **Medicine benefits from adaptive collaboration.** MDAgents rates question complexity and routes to a solo agent, a
  multidisciplinary team or an integrated care team; it was best on 7 of 10 medical benchmarks
  ([NeurIPS 2024](https://arxiv.org/abs/2404.15155)). Tiered oversight escalates only on explicit triggers
  ([TAO, 2025](https://arxiv.org/abs/2506.12482)).
- **Debate is not a free upgrade.** Multi-agent debate does not reliably beat a single agent with self-consistency
  ([ICML 2024](https://arxiv.org/abs/2311.17371), [2025](https://arxiv.org/abs/2502.08788),
  [NeurIPS 2025](https://arxiv.org/abs/2508.17536)), and medical agent teams show authority bias and agents restating their opening positions
  ([MedAgentAudit, 2025](https://arxiv.org/abs/2510.10185)). Debate pays off for ranking hypotheses
  ([AI co-scientist](https://arxiv.org/abs/2502.18864)) and for specific contested claims.
- **Decide architecture first, then specialists, then depth** ([MasRouter, ACL 2025](https://arxiv.org/abs/2502.11133));
  bootstrap router labels from which route actually succeeded ([Adaptive-RAG, NAACL 2024](https://arxiv.org/abs/2403.14403)).

So Auto scores every architecture from the question's shape, explains the score, adjudicates only close calls,
escalates on explicit triggers, and learns from how its answers held up.

## Architecture

All five architectures run on one engine: the framework id only selects which agent phases run at each stage
(`phronesis/agentic/runtime.py:get_phases`). Auto therefore needs no new pipeline:

```
question ─► read_question (entities, intents, scope)
              │
              ├─ deterministic signals ─┐
              ├─ semantic features ─────┼─► blended features ─► scores (+ learned nudge) ─► softmax ─► guardrails
              │   (model, k samples)    │                                                              │
              └─ outcome history ───────┘                     close call? ─► model adjudication ◄──────┘
                                                                              │
                                   chosen framework's phases ─► first draft ─► ESCALATION (once) ─► critic debate
                                                                                                     │
                                   verification, claim confidence, task status ─► outcome store ◄────┘
```

1. **Features** (`phronesis/agentic/router.py`), each in [0, 1]: hypothesis generation, contested claim, safety-critical,
   asks for a recommendation, head-to-head comparison, target evaluation, exploratory, open-ended, computational,
   explicit parts, independent (decomposable) parts, sequential steps, several facets, several domains.
   - *Deterministic signals* come from the planner's query context and keyword patterns.
   - *Semantic features* come from one structured model call (`[phase:router]`) that reads the question's shape:
     question kind (therapy comparison, dosing, diagnosis, prognosis, variant interpretation, target assessment, trial
     landscape, repurposing, hypothesis generation, simulation, data analysis, exploration, ...), number of parts,
     decomposable versus sequential, domains, and the flags above. `PHRONESIS_ROUTER_SAMPLES` (1–5) averages several
     samples (self-consistency).
   - The blend is 65% semantic, 35% deterministic. Safety flags (safety-critical, computational) are OR-ed: the model can
     raise them but never clear them. Without a model the deterministic signals are used alone.
2. **Scores.** Each architecture's score is a weighted sum of the features (`score_terms`), grounded in the evidence
   above: plan & execute is the backbone and takes decomposable and computational work; debate takes hypotheses and
   contested conclusions, more strongly with stakes, a recommendation or a comparison; the supervisor takes open-ended,
   multi-facet questions without explicit parts; the swarm takes only low-stakes exploration. A softmax (temperature
   0.4) gives a probability per architecture; the top probability is the router's confidence.
3. **Outcome learning** (`phronesis/agentic/router_store.py`). Every agentic research run, Auto-routed or chosen by the
   user, stores its features, a coarse question signature (kind plus strong flags), the architecture and an outcome
   quality: 35% grounding (verification score), 35% calibrated answer confidence, 15% task completion, 15% core phase
   health. Runs the user routed by hand observe architectures Auto did not pick. At routing time each architecture's
   mean quality on the same signature is shrunk toward its global mean (5 pseudo-runs), compared with the mean over all
   architectures, and added to its score (at least 3 runs, at most ±0.6). Only features are stored, never the question.
4. **Guardrails and adjudication.** A safety-critical question is never routed to the swarm. When the top probability
   is under 50% and the runner-up is within 20 points, one model call (`[phase:router_adjudicate]`) picks between the
   two; any failure keeps the scored choice.
5. **Escalation** (`EscalationPhase`, after the first draft, at most once, Auto only). The draft goes to a critic debate
   when model-judged counter-evidence contradicts it, the question is safety-critical, the route was uncertain (under
   45%), the core phase fell back to a fixed plan, or at least two and more than half of the sub-questions still lack
   evidence, provided at least four agent model calls remain. The run keeps its architecture.
6. **Explanation.** The router emits a "Choosing the approach" step and records `execution_artifacts.framework_routing`:
   chosen architecture, confidence, probabilities, the factors behind the score, the semantic reading, learned nudges,
   guardrail, adjudication and escalation. The Agent run card shows them under "Why this approach".

Settings: `PHRONESIS_ROUTER_LLM` (off = deterministic only), `PHRONESIS_ROUTER_SAMPLES`, `PHRONESIS_ROUTER_LEARNING`
(off | auto | on), `PHRONESIS_ROUTER_DIR`.

## Evaluation

`scripts/eval_router.py` routes the labelled question sets in `tests/fixtures/routing/` and prints strict accuracy (the
gold architecture), acceptable accuracy (gold or a listed alternative), accuracy per question kind and every miss:

- `benchmark.json`: 60 questions across 20 clinical question kinds; the weights were set on it.
- `holdout.json`: 24 questions written afterwards and never used for tuning, so its score is the honest one.

```
python scripts/eval_router.py                         # deterministic signals, benchmark
python scripts/eval_router.py --set holdout           # deterministic signals, held-out set
python scripts/eval_router.py --semantic --samples 3  # with the model's semantic features (needs a model key)
```

Measured on 2026-10-18 (semantic features from `nvidia/nemotron-3-super-120b-a12b`, one sample):

| Router | Benchmark strict / acceptable | Held-out strict / acceptable |
| --- | --- | --- |
| Deterministic signals only | 95.0% / 100% | 83.3% / 95.8% |
| Flat 65/35 blend of semantic and deterministic features | 83.3% / 86.7% | 87.5% / 91.7% |
| Per-feature trust (current) | 96.7% / 98.3% | 87.5% / 95.8% |

The flat blend was worse: the model over-called "exploratory", "open-ended" and "needs computation" on plain
lookups and diluted precise keyword hits. Per-feature trust (keyword hits stand, model-only specialist flags are
discounted, structural features lean on the model) fixed that. The held-out set was inspected after the flat-blend run,
so its current score is slightly optimistic. Remaining misses are "explore the links between X, Y and Z" questions,
which the model reads as multi-part and routes to plan & execute rather than the swarm.

`tests/test_auto_router.py` guards the deterministic accuracy on both sets, and covers semantic features,
self-consistency, guardrails, adjudication, outcome learning, escalation and Auto end to end. The labels encode the
routing policy, not measured answer quality; the outcome store is what ties routing to how answers actually held up.
