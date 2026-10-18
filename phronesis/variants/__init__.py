"""
Synapta variant & biomarker intelligence: ClinVar classifications, CIViC curated clinical evidence,
cBioPortal per-study mutation prevalence and gnomAD germline population frequency for one named variant.

Kept outside phronesis.synapta, whose package __init__ imports heavy subpackages.
"""

from .models import VariantIntelInput, VariantMention
from .parse import extract_cancer_keyword, parse_variant_mentions
from .render import describe_variant_result, render_variant_evidence, variant_markdown_summary
from .service import TOOL_NAME, compute_prevalence, run_variant_intelligence, select_studies, spdi_to_gnomad_id

__all__ = [
    "TOOL_NAME",
    "VariantIntelInput",
    "VariantMention",
    "compute_prevalence",
    "describe_variant_result",
    "extract_cancer_keyword",
    "parse_variant_mentions",
    "render_variant_evidence",
    "run_variant_intelligence",
    "select_studies",
    "spdi_to_gnomad_id",
    "variant_markdown_summary",
]
