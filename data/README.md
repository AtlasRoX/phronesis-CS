# Phronesis — Datasets Directory

> **Phronesis** — Biomedical Research Assistant  
> **Made by ChronoStrider Engineering (ChronoStrider Enginnerings)**

Default root for dataset paths typed into a question (`PHRONESIS_DATASET_ROOTS` overrides it; see
`phronesis/datasets/paths.py`). Uploaded datasets live in the per-user upload store, not here.

## pbmc3k_processed.h5ad

| Field | Value |
|---|---|
| Source | `scanpy.datasets.pbmc3k_processed()` backup URL: https://raw.githubusercontent.com/chanzuckerberg/cellxgene/main/example-dataset/pbmc3k.h5ad |
| Original data | 10x Genomics "3k PBMCs from a Healthy Donor", processed as in the Scanpy PBMC3k tutorial |
| Size | 24,653,425 bytes |
| SHA-256 | `0db367b991dd95809732b218539ede489bea99113807f62ebd7ccc970025fe38` |
| Retrieved | 14 October 2026 |
| Dimensions | 2,638 cells × 1,838 genes in `X`; `.raw` holds 13,714 genes |

Matrix contents, as inspected:

- `X` is centered and scaled (negative values, clipped at 10); it is not used for QC or marker tests.
- `.raw.X` is log1p of unnormalized counts (`expm1` of each cell's values sums to an integer total). Lumicell
  detects this as `log1p_counts`, recovers counts with `expm1` and rounding, then applies count QC,
  `normalize_total(1e4)` and `log1p` itself. The applied steps are listed in the artifact's `transformations`.
- `obs['louvain']` holds the tutorial's cluster labels; Lumicell re-clusters and does not use them.

### Replaced truncated copy

The copy previously committed here was truncated: 11,886,592 bytes, SHA-256
`67a64adfc5238dec265a6f120b41f913cda526130a2eddde79edd94b45c4145a`. HDF5 expected an end of file at
24,653,425 bytes and the file could not be opened (the upload validator rejected it as `hdf5_unreadable`).
Those bytes are an exact prefix of the file above. The file was re-downloaded rather than repaired.

To fetch it again:

```sh
curl -L -o data/pbmc3k_processed.h5ad \
  https://raw.githubusercontent.com/chanzuckerberg/cellxgene/main/example-dataset/pbmc3k.h5ad
sha256sum data/pbmc3k_processed.h5ad   # 0db367b9...025fe38
```

Use this file only for demonstrations on PBMC data. It must never stand in for a dataset the user asked about.
