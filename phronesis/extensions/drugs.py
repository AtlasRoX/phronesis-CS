"""Extension module owned by DRUGS (feature 2, ChEMBL + openFDA). Registers that feature's hooks, sources and tools."""

from __future__ import annotations

from typing import Any

from phronesis.drugintel.checks import check_faers, check_fda_approval, check_pharmacology
from phronesis.drugintel.sources import ChemblSource, OpenFdaSource
from phronesis.extensions import hooks
from phronesis.sources import register_source

PROMPT_COMPONENTS = {
    "drugs_chembl": "- Synapta (ChEMBL): exact drug resolution to parent molecules, mechanisms of action, and IC50/Ki/Kd "
    "bioactivity records against the named target",
    "drugs_openfda": "- Lithoscribe (openFDA): FAERS spontaneous adverse-event report counts and FDA label excerpts "
    "(boxed warning, warnings, contraindications)",
}

PROMPT_RULES = {
    "drugs_bioactivity": "Quote ChEMBL bioactivities with their type, relation, value, units and assay ChEMBL ID exactly "
    "as given. Never average, convert, or round them, and name the "
    "assay variant when the assay description mentions a mutant or a different construct.",
    "drugs_faers": "FAERS counts are spontaneous reports that mention the drug in any role; they show neither incidence "
    "nor causation. Never compute rates or percentages from them, never say the drug causes a reported event, and "
    "include one plain-language sentence saying so whenever you cite FAERS counts.",
    "drugs_label": "Quote FDA label warnings only from the label excerpt in the evidence, naming the product and its "
    "application number (or saying they are unknown when openFDA did not harmonize the label). When the label is a "
    "combination product (combination_product=\"true\"), say so and do not attribute its warnings to the queried drug alone.",
    "drugs_approval": "ChEMBL max phase 4 means approved by at least one regulator somewhere. Do not call a drug "
    "\"FDA-approved\" unless Drugs@FDA or an openFDA label in the evidence shows its NDA or BLA.",
}

register_source(ChemblSource())
register_source(OpenFdaSource())
hooks.register_verification_check("drug_pharmacology", check_pharmacology)
hooks.register_verification_check("drug_faers", check_faers)
hooks.register_verification_check("drug_fda_approval", check_fda_approval)


def register_prompt_hooks() -> None:
    """Prompt lines describing the drug tools; registered together with the tools they describe."""
    for key, line in PROMPT_COMPONENTS.items():
        hooks.register_prompt_component(key, line)
    for key, text in PROMPT_RULES.items():
        hooks.register_prompt_rule(key, text)


def register_tools(registry: Any) -> None:
    from phronesis.drugintel.engine import get_drug_intel_engine
    from phronesis.drugintel.schemas import (
        ChemblProfileInput,
        ChemblProfileOutput,
        ChemblResolveInput,
        ChemblResolveOutput,
        OpenFdaSafetyInput,
        OpenFdaSafetyOutput,
    )
    from phronesis.tools.registry import AdapterType, ToolUniverseEntry

    engine = get_drug_intel_engine()
    registry.register(
        ToolUniverseEntry(
            name="chembl_resolve_drugs",
            description="Resolve drug names, trade names or ChEMBL IDs to ChEMBL parent molecules by exact match, "
            "and a gene symbol to its human single-protein ChEMBL target.",
            domain="synapta",
            adapter_type=AdapterType.DIRECT_REST,
            input_schema=ChemblResolveInput,
            output_schema=ChemblResolveOutput,
            canonical_id_types=["chembl_id"],
            # Room for a name resolve's three sequential requests, each with one retry (chembl_client).
            timeout_seconds=40.0,
            tags=["drugs", "chembl", "resolve"],
        ),
        executor=engine.resolve_drugs,
    )
    registry.register(
        ToolUniverseEntry(
            name="chembl_drug_profile",
            description="ChEMBL mechanisms of action (by parent molecule), IC50/Ki/Kd bioactivities against a target, "
            "and approved drugs acting on a target.",
            domain="synapta",
            adapter_type=AdapterType.DIRECT_REST,
            input_schema=ChemblProfileInput,
            output_schema=ChemblProfileOutput,
            canonical_id_types=["chembl_id"],
            # The approved-drugs lookup is two sequential requests, then target names; each may retry once.
            timeout_seconds=60.0,
            tags=["drugs", "chembl", "mechanism", "bioactivity", "potency"],
        ),
        executor=engine.drug_profile,
    )
    registry.register(
        ToolUniverseEntry(
            name="openfda_drug_safety",
            description="openFDA FAERS adverse-event report counts and the FDA label boxed warning, warnings and "
            "contraindications for an active ingredient.",
            domain="lithoscribe",
            adapter_type=AdapterType.DIRECT_REST,
            input_schema=OpenFdaSafetyInput,
            output_schema=OpenFdaSafetyOutput,
            canonical_id_types=["set_id"],
            timeout_seconds=35.0,
            tags=["drugs", "safety", "faers", "label", "openfda"],
        ),
        executor=engine.drug_safety,
    )
    register_prompt_hooks()
