"""Extension module owned by VARIANTS (feature 6, ClinVar/CIViC/gnomAD/cBioPortal). Registers that feature's hooks, sources and tools."""

from __future__ import annotations

from typing import Any

from phronesis.extensions import hooks
from phronesis.sources import register_source
from phronesis.variants.checks import check_clinvar_classification, check_variant_frequencies
from phronesis.variants.source import TOOL_NAME, VariantIntelSource

PROMPT_COMPONENT = (
    "- Synapta variant intelligence: ClinVar classifications, CIViC curated clinical evidence, cBioPortal per-study "
    "mutation prevalence and gnomAD germline population allele frequency for a named variant"
)
PROMPT_RULE = (
    "Variant evidence (<variant_intelligence>) has its own reporting rules, because each database answers a different "
    "question. Report cBioPortal prevalence per study as \"n/N profiled samples (x%)\" with the study name; never pool "
    "or average studies (cohorts overlap) and never generalize a figure beyond its cohort. gnomAD is germline "
    "population allele frequency, not tumour prevalence; when gnomAD marks a call as failing QC filters "
    "(exome_qc/genome_qc=\"FAIL\"), say so and call that frequency unreliable. Count patients only over profiled "
    "patients and samples only over profiled samples. Keep "
    "ClinVar's germline, oncogenicity and somatic clinical-impact classifications distinct, each with its review "
    "status, and quote them as written (\"Conflicting classifications of pathogenicity\" is not \"pathogenic\"). A "
    "ClinVar classification applies only to the condition(s) listed with it and at its review status (star rating): "
    "a germline classification for a developmental disorder says nothing about a tumour type. Give "
    "the CIViC evidence level, type and significance with its EID, and treat levels C-E as preliminary (case "
    "studies, preclinical or inferential). If a variant database is reported as failed or skipped, say its data is "
    "unavailable rather than filling it in."
)

register_source(VariantIntelSource())
hooks.register_verification_check("variant_frequencies", check_variant_frequencies)
hooks.register_verification_check("variant_clinvar_classification", check_clinvar_classification)


def register_prompt_hooks() -> None:
    hooks.register_prompt_component("variants", PROMPT_COMPONENT)
    hooks.register_prompt_rule("variants", PROMPT_RULE)


def register_tools(registry: Any) -> None:
    from phronesis.tools.registry import AdapterType, ToolUniverseEntry
    from phronesis.variants.models import VariantIntelInput
    from phronesis.variants.service import run_variant_intelligence

    registry.register(
        ToolUniverseEntry(
            name=TOOL_NAME,
            description=(
                "Synapta variant intelligence for one gene-level variant: ClinVar germline/oncogenicity/somatic "
                "classifications, CIViC accepted evidence and assertions, cBioPortal per-study mutation prevalence "
                "(panel-aware denominators, never pooled) and gnomAD v4 germline allele frequency."
            ),
            domain="synapta",
            adapter_type=AdapterType.DIRECT_REST,
            input_schema=VariantIntelInput,
            canonical_id_types=["clinvar_vcv", "rsid", "civic_eid", "pmid"],
            timeout_seconds=30.0,
            tags=["variant", "clinvar", "civic", "cbioportal", "gnomad", "biomarker"],
        ),
        executor=run_variant_intelligence,
    )
    register_prompt_hooks()
