"""Extension module owned by OMICS SOURCES (feature 20). Registers the omics evidence sources and their tools.

GWAS Catalog, GTEx, STRING, Reactome, PubChem, bioRxiv/medRxiv and a local DepMap file each run in Stage 1 only
when the question asks for what they hold (see phronesis/sources_extra). Their artifacts merge into
execution_artifacts["omics_intel"].

Prompt: the reporting rules (quote GWAS p-values / odds ratios, GTEx TPM, STRING scores and DepMap statistics exactly
with their source; preprints are not peer reviewed) travel in the `note` of each source's evidence section, so they
reach the model only when that source ran. The static component line and rule below are NOT registered by default:
the composed synthesis prompt is capped at 13 kB (tests/test_prompts.py) and the existing features already use it
up. Call register_static_prompt_hooks() (or set PHRONESIS_OMICS_PROMPT_RULES=on) to add them to every prompt.
"""

from __future__ import annotations

import os
from typing import Any

from phronesis.extensions import hooks
from phronesis.sources import register_source
from phronesis.sources_extra import all_sources, tool_entries

PROMPT_COMPONENT = (
    "- Synapta omics: GWAS Catalog, GTEx, STRING, Reactome, PubChem, DepMap (local file), bioRxiv/medRxiv preprints"
)
PROMPT_RULE = (
    "Quote omics values exactly with their source: GWAS p-values, odds ratios and betas with rsID and GCST accession; "
    "GTEx median TPM with tissue; STRING combined score with partner; DepMap statistics with release. GWAS hits are "
    "associations, not causation. Call every cited preprint not peer reviewed."
)

for _src in all_sources():
    register_source(_src)


def register_static_prompt_hooks() -> None:
    """Add the omics component line and rule to every synthesis prompt (opt-in; see the module docstring)."""
    hooks.register_prompt_component("omics_sources", PROMPT_COMPONENT)
    hooks.register_prompt_rule("omics_sources", PROMPT_RULE)


def register_tools(registry: Any) -> None:
    for entry, executor in tool_entries():
        registry.register(entry, executor=executor)
    if os.getenv("PHRONESIS_OMICS_PROMPT_RULES", "").strip().lower() in ("1", "on", "true", "yes"):
        register_static_prompt_hooks()
