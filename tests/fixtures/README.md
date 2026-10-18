# Test Fixtures — Phronesis

> **Phronesis** — Biomedical Research Assistant  
> **Made by ChronoStrider Engineering (ChronoStrider Enginnerings)**

## test_atlas.h5ad

Synthetic structural fixture, not biological data: 40 cells × 10 genes (`KRAS`, `EGFR`, `GAPDH`, `ACTB`,
`TP53`, `BRAF`, `MYC`, `CDK4`, `MT-CO1`, `MT-CO2`) with small integer counts and an `obs['donor_id']` column.
It has no `uns` provenance. It exercises upload validation, routing and the Lumicell pipeline end to end.
Results computed from it carry no scientific meaning and must not be presented as findings.
