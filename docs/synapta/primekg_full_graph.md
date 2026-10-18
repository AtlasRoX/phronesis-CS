# Full PrimeKG: local knowledge-graph analytics

Synapta queries the **complete** PrimeKG release (Chandak, Huang & Zitnik, *Sci Data* 2023; Harvard Dataverse
[doi:10.7910/DVN/IXA7BM](https://doi.org/10.7910/DVN/IXA7BM), version 2.1) in memory: 129,375 nodes, 4,050,249
undirected edges, 10 node types and 30 relation types, plus DrugBank drug text and MONDO / Orphanet / Mayo Clinic
disease text. It needs no Milvus, Docker, GPU or network access once built. It is separate from the Milvus-backed
`subgraph_extraction` tool, which serves an embedding-indexed IBD subset.

## Build the store (once)

```sh
uv run phronesis-primekg build      # downloads ~520 MB from Harvard Dataverse (resumable), compiles in ~15-80 s
uv run phronesis-primekg stats
```

| Path | Contents |
|---|---|
| `$PHRONESIS_PRIMEKG_DIR/raw/` | the Dataverse originals: `nodes.tab`, `edges.csv`, `drug_features.tab`, `disease_features.tab` |
| `$PHRONESIS_PRIMEKG_DIR/store/` | ~63 MB: Parquet tables, a persisted CSR adjacency (`csr_*.npy`), `manifest.json` |

`PHRONESIS_PRIMEKG_DIR` defaults to `~/.cache/phronesis/primekg`. The manifest records the Dataverse file ids,
byte sizes and SHA-256 of every raw file, the build time and the per-relation edge counts. The raw `edges.csv`
lists every edge in both directions (8,100,498 rows); the store keeps each `(relation, display_relation, pair)`
once. The grouped disease nodes keep all their MONDO member ids and names; the names double as synonyms for
entity resolution.

Loading takes about 6 s and about 0.5 GB of memory. The FastAPI backend preloads the graph in a background
thread at startup; set `PHRONESIS_PRIMEKG_PRELOAD=0` to disable this.

## What it answers

| Operation | What it returns |
|---|---|
| `drug_profile` | indications, contraindications, off-label uses, targets / enzymes / transporters / carriers, side effects, interaction count, approval groups, ATC levels, mechanism of action, half-life |
| `disease_profile` | associated genes, phenotypes present / absent, indicated / contraindicated / off-label drugs, related diseases, exposures, definition, prevalence, symptoms, causes, management |
| `gene_profile` | drugs by role, diseases, Reactome pathways, GO terms, phenotypes, tissues with / without expression, PPI partners |
| `paths` | shortest typed paths between any two nodes, ranked toward specific (low-degree) connectors; hubs are avoided when an alternative exists, and drug-drug interaction edges are excluded by default |
| `drug_disease` | the recorded relation (indicated / contraindicated / off-label / none), shared genes, disease genes one PPI step from the drug's targets, and mechanism paths |
| `repurposing` | ranked drug hypotheses for a disease with per-component scores and a calibration check (below) |
| `phenotype_diagnosis` | diseases ranked by information-content-weighted overlap with the present phenotypes, penalised for phenotypes stated absent |
| `polypharmacy` | per pair: recorded DrugBank interaction, shared metabolising enzymes (CYPs), transporters, targets and side effects; contraindications for stated conditions |
| `enrichment` | hypergeometric over-representation of a gene list in pathways, GO terms, diseases, phenotypes, tissues or drug target sets, with Benjamini-Hochberg q-values |
| `similar` | the same-type nodes with the most similar neighbourhoods (Jaccard) |
| `neighbors`, `resolve`, `stats` | typed neighbours, entity resolution, store statistics |

The resolver accepts names, MONDO / HP / GO / UBERON ids, DrugBank ids, NCBI Gene ids and gene symbols. It also
handles possessives and plurals ("Crohn's disease"), salt forms ("imatinib mesylate"), PrimeKG qualifiers
("ulcerative colitis (disease)") and a curated alias table (`phronesis/kgintel/aliases.py`) of disease
abbreviations (IPF, NSCLC, CML ...), INN variants (aspirin, paracetamol ...) and brand names. An alias is kept
only if its target resolves exactly in the loaded store.

```sh
uv run phronesis-primekg query repurposing disease="idiopathic pulmonary fibrosis" limit=10 --markdown
uv run phronesis-primekg query phenotype_diagnosis present=Seizure,Ataxia,Hypotonia absent="Hearing impairment"
uv run phronesis-primekg query polypharmacy drugs=warfarin,fluconazole,aspirin
uv run phronesis-primekg ask "How might metformin be linked to polycystic ovary syndrome?" --markdown
```

## Repurposing score and its calibration

Each drug is scored on three PrimeKG signals, each 0-3 before weighting:

- **target overlap**: disease genes the drug targets, weighted by gene specificity (inverse disease frequency,
  so ESR1 or TNF count less), and discounted for drugs with many targets
- **network proximity**: the mean shortest PPI distance from the drug's targets to the nearest disease gene,
  z-scored against 200 degree-matched random target sets (Guney et al., *Nat Commun* 2016); score = −z / 1.5
- **similar-disease indication**: 3 × the similarity (mean Jaccard of genes and phenotypes) of the most similar
  disease the drug is indicated for

The default weights are target 1, proximity 0.5 and similar-disease 2. They were chosen on a 20-disease
benchmark that measures how well the score ranks each disease's own known indications (AUROC against every
other drug; `scripts/benchmark_primekg_repurposing.py`). With direct ontology parents and children held out of
the similar-disease signal (`--strict`), the results are:

| Scoring | Mean AUROC (strict) | Mean AUROC (default) | Known indications in top 100 (strict) |
|---|---|---|---|
| target overlap only | 0.661 | 0.661 | 81 / 534 |
| network proximity only | 0.695 | 0.695 | 45 / 534 |
| similar-disease indication only | 0.751 | 0.908 | 206 / 534 |
| equal weights (1, 1, 1) | 0.787 | 0.891 | 111 / 534 |
| **default (1, 0.5, 2)** | **0.811** | **0.919** | **153 / 534** |

The signals are complementary: the weighted sum beats each one alone in both settings. The default column is
inflated because disease subtypes share indications by construction, so the strict column is the honest
estimate. The 20-disease panel is listed in the script.

Every repurposing result carries this calibration for its own disease: how many known indications rank in the
top 20 and top 100, their median rank and the AUROC. A low value means the graph carries little signal for that
disease. Known indications and contraindicated drugs are scored but excluded from the candidate list. Salt and
ester forms of one parent drug are collapsed into one candidate.

## How it reaches the answer

- **Orchestrator** (`phronesis/extensions/primekg.py`): the `primekg` evidence source (agent Synapta) runs when
  the store exists and the question names a drug, disease or gene, or asks a phenotype, regimen, repurposing or
  enrichment question. It plans up to five analyses (`phronesis/kgintel/planner.py`) and runs them in one
  `primekg_investigate` call (a `LOCAL_COMPUTE` registry tool). It contributes escaped evidence XML with
  reading rules, DrugBank / MONDO / HPO citations, verifier-allowed identifiers, a `primekg` artifact card in
  the web UI, a deterministic fallback summary and scope notes. `primekg_query` exposes any single operation.
- **Citations and verification**: DrugBank ids (`DB00945`) are a citation key in both the backend and web
  grammars, and an identifier family the verifier checks. A DrugBank id in an answer that no tool returned is
  a critical defect, as for ChEMBL ids.
- **Provenance**: the run manifest digest for `primekg_investigate` records the PrimeKG version, the SHA-256 of
  `edges.csv`, the analyses run and the resolved identifiers.
- **Synapta LangGraph agent**: the `primekg_query` LangChain tool (`phronesis/synapta/tools/primekg_query.py`)
  is bound alongside subgraph extraction, so the Streamlit Synapta and Phronesis apps can use the full graph.

## Limits

- PrimeKG is a 2022/2023 snapshot; drugs approved since then are absent.
- Edges record that a source asserts an association. Indication and contraindication edges are label facts;
  disease-gene edges are associations, not causal evidence. Side-effect edges carry no frequency, and
  drug-drug edges carry no severity.
- Repurposing scores and phenotype rankings are hypotheses, and the evidence rules instruct the synthesis
  model to present them that way.
