"""
BioDiscoveryAgent Engine for Phronesis, inspired by BioDiscoveryAgent (2024).
Provides evidence-backed hypothesis prioritization, multi-factorial target ranking,
iterative experiment updates, and explicitly labeled laboratory validation protocols.
Strictly prohibits evidence-free rankings, fabricated scores, or ungrounded claims of simulations.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from ..tools.registry import ToolResult

logger = logging.getLogger(__name__)


def _num(value: Any) -> Optional[float]:
    """A finite float, or None for missing/non-numeric values (tool payloads can carry nulls)."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


# Modification suffixes/prefixes that still name the target species (Erk2-PP, RasGTP, pMEK).
_SPECIES_SUFFIXES = {"p", "pp", "ppp", "gtp", "gdp", "star", "act", "active", "inactive", "a", "i"}
_SPECIES_PREFIXES = {"p", "pp", "phospho"}


def _species_names_target(species: str, keys: List[str]) -> bool:
    """Whole-token match of a target symbol in a model species name ('Metabolite' does not name MET)."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", species.lower()) if t]
    for tok in tokens:
        for k in keys:
            if tok == k:
                return True
            if tok.startswith(k) and tok[len(k):] in _SPECIES_SUFFIXES:
                return True
            if tok.endswith(k) and tok[: -len(k)] in _SPECIES_PREFIXES:
                return True
    return False


class PrioritizedCandidate(BaseModel):
    """Ranked therapeutic target or combination hypothesis grounded strictly in observed evidence."""
    target: str
    rank: int
    composite_priority_score: Optional[float] = None  # None if insufficient data
    observed_components: Dict[str, Optional[float]] = Field(default_factory=dict)
    rationale: str
    prediction_label: str  # [PROPOSED HYPOTHESIS: ...]
    proposed_validation_experiment: str
    supporting_sources: List[str] = Field(default_factory=list)


class DiscoveryHypothesisReport(BaseModel):
    """Structured report prioritizing therapeutic hypotheses and laboratory validation plans."""
    inquiry: str
    primary_target: str
    status: str = "COMPLETED"  # COMPLETED | INSUFFICIENT_EVIDENCE
    candidates: List[PrioritizedCandidate] = Field(default_factory=list)
    iterative_update_summary: Optional[str] = None

    def to_markdown_summary(self) -> str:
        if self.status == "INSUFFICIENT_EVIDENCE" or not self.candidates:
            return (
                f"### Evidence-Backed Target & Combination Prioritization (BioDiscoveryAgent)\n"
                f"**Inquiry Focus**: {self.inquiry} | Target: **{self.primary_target}**\n\n"
                f"> [!NOTE]\n"
                f"> **Prioritization Disabled**: {self.iterative_update_summary or 'Insufficient empirical tool evidence observed to compute scientifically valid prioritization rankings. No ungrounded candidate scores were generated.'}\n"
            )

        lines = [
            f"### Evidence-Backed Target & Combination Prioritization (BioDiscoveryAgent)",
            f"**Inquiry Focus**: {self.inquiry} | Primary Target: **{self.primary_target}**",
            "",
            "| Rank | Proposed Intervention / Combination | Evidence Score | Hypothesis Tier | Evidence-Based Rationale | Proposed Laboratory Validation |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
        for c in self.candidates:
            score_str = f"`{c.composite_priority_score:.2f}`" if c.composite_priority_score is not None else "`Unknown`"
            lines.append(
                f"| **#{c.rank}** | **{c.target}** | {score_str} | `{c.prediction_label}` | {c.rationale} | {c.proposed_validation_experiment} |"
            )

        if self.iterative_update_summary:
            lines.extend([
                "",
                f"> **Evidence Integration**: {self.iterative_update_summary}",
            ])

        return "\n".join(lines)


class BioDiscoveryEngine:
    """
    Engine that evaluates and prioritizes therapeutic intervention hypotheses strictly from
    observed, verified multi-specialist tool outputs:
    - OpenTargets observed target-disease association (genetic/somatic evidence; NOT therapeutic probability)
    - Vivarix observed kinetic flux change (if actual ODE simulation executed)
    - Lumicell observed differential expression (if real AnnData dataset analyzed)
    - Lithoscribe observed literature passages and trial evidence
    """

    @staticmethod
    def prioritize_hypotheses(
        inquiry: str,
        primary_target: Optional[str],
        tool_results: Dict[str, ToolResult],
        *,
        target_aliases: Optional[List[str]] = None,
    ) -> DiscoveryHypothesisReport:
        """
        Evaluate candidate hypotheses strictly based on observed tool results.
        If no substantive tool evidence is present, disables ranking rather than inventing scores.
        target_aliases (e.g. ERK2 for MAPK1) also match model species and single-cell marker genes.
        """
        clean_target = (primary_target or "").strip().upper()
        if not clean_target:
            return DiscoveryHypothesisReport(
                inquiry=inquiry,
                primary_target="Unspecified",
                status="INSUFFICIENT_EVIDENCE",
                candidates=[],
                iterative_update_summary="No specific biological target identified in query. Hypothesis prioritization requires a defined molecular target.",
            )

        # 1. Audit observed evidence across tools
        has_any_evidence = False
        ot_association_score: Optional[float] = None
        ot_sources: List[str] = []
        observed_passages_count = 0
        lit_sources: List[str] = []
        sim_executed = False
        sim_flux_change: Optional[float] = None
        sim_species_basis = ""
        sc_executed = False
        sc_score: Optional[float] = None
        sc_basis = ""

        # Check Synapta evidence
        syn_res = tool_results.get("synapta") or tool_results.get("synapta_opentargets_associations")
        if syn_res and syn_res.success and isinstance(syn_res.data, dict):
            assocs = syn_res.data.get("associations", [])
            top_assoc = assocs[0] if assocs and isinstance(assocs[0], dict) else {}
            score = _num(top_assoc.get("overall_association_score"))
            if score is not None:
                has_any_evidence = True
                ot_association_score = score
                disease_name = top_assoc.get("disease_name") or "target-associated disease"
                ot_sources.append(f"OpenTargets association for {disease_name}: score={ot_association_score:.2f}")

        # Check Lithoscribe evidence
        lith_res = tool_results.get("lithoscribe") or tool_results.get("lithoscribe_literature_synthesis")
        lit_graded: Optional[Tuple[float, int, str]] = None  # (support, articles, rationale)
        if lith_res and lith_res.success and isinstance(lith_res.data, dict):
            passages = [p for p in lith_res.data.get("ranked_passages", []) or [] if isinstance(p, dict)]
            passages = [p for p in passages if p.get("retracted") is not True and p.get("integrity_status") not in ("retracted", "withdrawn")]
            if passages:
                has_any_evidence = True
                observed_passages_count = len(passages)
                lit_graded = BioDiscoveryEngine._graded_literature(passages, lith_res.data.get("excluded_articles") or [], lit_sources)
                if lit_graded is None:
                    for p in passages[:3]:
                        cit = p.get("pmid") or p.get("doi") or "EuropePMC"
                        lit_sources.append(f"Literature passage ({cit})")

        # Check Vivarix evidence
        viv_res = tool_results.get("vivarix") or tool_results.get("vivarix_simulate_time_course")
        if viv_res and viv_res.success and isinstance(viv_res.data, dict):
            final_concs = viv_res.data.get("final_concentrations", {})
            if final_concs:
                has_any_evidence = True
                sim_executed = True
                sim_flux_change, sim_species_basis = BioDiscoveryEngine._kinetic_response_score(
                    viv_res.data.get("initial_concentrations", {}) or {}, final_concs, clean_target, aliases=target_aliases
                )

        # Check Lumicell evidence
        lum_res = tool_results.get("lumicell") or tool_results.get("lumicell_analyze_single_cell")
        if lum_res and lum_res.success and isinstance(lum_res.data, dict):
            top_markers = lum_res.data.get("top_markers_by_cluster", {})
            if top_markers:
                has_any_evidence = True
                sc_executed = True
                sc_score, sc_basis = BioDiscoveryEngine._single_cell_evidence_score(
                    top_markers, lum_res.data.get("target_expression_summary", {}) or {}, clean_target, aliases=target_aliases
                )

        # 2. Gatekeeper: Zero-evidence protection
        if not has_any_evidence or (
            ot_association_score is None and observed_passages_count == 0 and not sim_executed and not sc_executed
        ):
            return DiscoveryHypothesisReport(
                inquiry=inquiry,
                primary_target=clean_target,
                status="INSUFFICIENT_EVIDENCE",
                candidates=[],
                iterative_update_summary="Prioritization disabled: Insufficient empirical evidence from tools. No valid target-disease associations, ODE simulations, or literature passages were observed.",
            )

        # 3. Calculate components from identified observations ONLY
        # Available components:
        # - disease_association: OpenTargets score (if observed)
        # - literature_support: graded passages -> min(1.0, sum of per-article study-type weights / 2.0), each
        #   weight x0.7 for a preprint, x0.5 under an expression of concern, x0.8 when inferred from text;
        #   ungraded passages -> min(1.0, count / 4.0) (if observed)
        # - pathway_kinetics: mean normalized |final - initial| concentration change (None if unsimulated)
        # - single_cell_marker_evidence: |logFC|-scaled strength of FDR-significant markers (None if unobserved)

        candidates: List[PrioritizedCandidate] = []

        components: Dict[str, Optional[float]] = {
            "disease_association": ot_association_score,
            "literature_support": (
                lit_graded[0] if lit_graded is not None
                else round(min(1.0, observed_passages_count / 4.0), 2) if observed_passages_count > 0 else None
            ),
            "pathway_kinetics": sim_flux_change if sim_executed else None,
            "single_cell_marker_evidence": sc_score if sc_executed else None,
        }

        # Compute composite priority score strictly as average of available non-None components
        valid_vals = [v for v in components.values() if v is not None]
        composite_score = round(sum(valid_vals) / len(valid_vals), 2) if valid_vals else None

        # Determine hypothesis tier based on breadth of empirical modalities
        n_modalities = sum(1 for v in components.values() if v is not None)
        if n_modalities >= 3:
            tier = "[PREDICTION: PROPOSED HYPOTHESIS - MULTI-MODAL EVIDENCE]"
        elif n_modalities >= 2:
            tier = "[PREDICTION: PROPOSED HYPOTHESIS - MODERATE EVIDENCE]"
        else:
            tier = "[PREDICTION: PROPOSED HYPOTHESIS - PRELIMINARY SINGLE MODALITY]"

        # Build grounded rationale based strictly on what was actually observed
        rationale_parts = []
        if ot_association_score is not None:
            rationale_parts.append(f"Observed OpenTargets target-disease association score of {ot_association_score:.2f}.")
        if lit_graded is not None:
            rationale_parts.append(lit_graded[2])
        elif observed_passages_count > 0:
            rationale_parts.append(f"Supported by {observed_passages_count} peer-reviewed passages retrieved from EuropePMC.")
        if sim_executed and sim_flux_change is not None:
            rationale_parts.append(
                f"COPASI/BASICO time-course simulation shows a mean normalized concentration change of "
                f"{sim_flux_change:.2f} across {sim_species_basis}."
            )
        elif sim_executed:
            rationale_parts.append("COPASI/BASICO time-course simulation executed, but no species had comparable non-zero concentrations.")
        elif viv_res is not None:
            # Only a simulation that was attempted (requested) and gave nothing is worth a caveat; one that was
            # never requested is out of scope and not mentioned.
            rationale_parts.append("Kinetics unsimulated (the requested simulation returned no usable result).")
        if sc_executed and sc_score is not None:
            rationale_parts.append(f"Single-cell evidence score {sc_score:.2f} ({sc_basis}).")
        elif sc_executed:
            rationale_parts.append(f"Single-cell dataset analyzed but provided no marker evidence ({sc_basis}).")
        elif lum_res is not None:
            rationale_parts.append("Single-cell expression unverified (the requested analysis returned no usable result).")

        rationale_str = " ".join(rationale_parts)

        candidate_name = f"{clean_target} Targeted Modality / Combination"
        all_sources = ot_sources + lit_sources

        candidates.append(
            PrioritizedCandidate(
                target=candidate_name,
                rank=1,
                composite_priority_score=composite_score,
                observed_components=components,
                rationale=rationale_str,
                prediction_label=tier,
                proposed_validation_experiment=f"In vitro target engagement assay, Western blot analysis, and dose-response synergy screen for {clean_target} measuring cellular proliferation and downstream pathway phosphorylation.",
                supporting_sources=all_sources,
            )
        )

        summary_parts = [
            f"Disease association: {_fmt(components['disease_association'])}",
            f"Literature: {_fmt(components['literature_support'])}",
        ]
        # Kinetics and single-cell appear only when they were attempted: an analysis nobody asked for is not a gap.
        if viv_res is not None:
            summary_parts.append(f"Kinetics: {_fmt(components['pathway_kinetics'])}")
        if lum_res is not None:
            summary_parts.append(f"Single-Cell: {_fmt(components['single_cell_marker_evidence'])}")
        update_summary = (
            f"Prioritization computed across {n_modalities} observed modality(ies). " + "; ".join(summary_parts) + "."
        )

        return DiscoveryHypothesisReport(
            inquiry=inquiry,
            primary_target=clean_target,
            status="COMPLETED",
            candidates=candidates,
            iterative_update_summary=update_summary,
        )

    @staticmethod
    def _graded_literature(
        passages: List[Dict[str, Any]], excluded: List[Dict[str, Any]], sources_out: List[str]
    ) -> Optional[Tuple[float, int, str]]:
        """
        Grade-weighted literature support over unique articles: min(1, sum(article_weight) / 2), where
        article_weight = policy weight x0.7 if preprint x0.5 if expression of concern x0.8 if text-heuristic.
        Returns None when any passage lacks a grade (the caller falls back to count / 4).
        """
        from ..lithoscribe.evidence_grading import basis_phrase, summary_phrases

        articles: Dict[str, Dict[str, Any]] = {}
        for p in passages:
            if not isinstance(p.get("evidence_grade"), dict):
                return None
            key = str(p.get("article_key") or p.get("pmid") or p.get("doi") or p.get("passage_id") or "")
            articles.setdefault(key or f"passage-{len(articles)}", p)

        total = 0.0
        counts: Dict[str, int] = {}
        for p in articles.values():
            g = p["evidence_grade"]
            weight = _num(g.get("weight")) or 0.0
            if g.get("is_preprint"):
                weight *= 0.7
            if p.get("integrity_status") == "expression_of_concern":
                weight *= 0.5
            if g.get("basis") == "text_heuristic":
                weight *= 0.8
            total += weight
            st = str(g.get("study_type") or "unclassified")
            counts[st] = counts.get(st, 0) + 1
            if len(sources_out) < 5:
                ident = f"PMID {p['pmid']}" if p.get("pmid") else (f"DOI {p['doi']}" if p.get("doi") else str(p.get("article_key") or "EuropePMC"))
                flags = [f for f, on in (("preprint", g.get("is_preprint")), ("expression of concern", p.get("integrity_status") == "expression_of_concern")) if on]
                sources_out.append(
                    f"{ident}: {str(g.get('label') or st).lower()} ({basis_phrase(g)}){'; ' + ', '.join(flags) if flags else ''}"
                )

        support = round(min(1.0, total / 2.0), 2)
        n = len(articles)
        text = f"Supported by {n} EuropePMC article{'s' if n != 1 else ''} ({', '.join(summary_phrases(counts, 'long'))})"
        excluded_counts: Dict[str, int] = {}
        for ex in excluded:
            reason = ex.get("excluded_reason") if isinstance(ex, dict) else None
            if reason in ("retracted", "withdrawn"):
                excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
        for reason, k in excluded_counts.items():
            text += f"; {k} {reason} article{'s' if k != 1 else ''} excluded"
        text += f". Literature support {support:.2f} = min(1, sum of study-type weights / 2)."
        return support, n, text

    @staticmethod
    def _kinetic_response_score(
        initial: Dict[str, Any], final: Dict[str, Any], target: str, aliases: Optional[List[str]] = None
    ) -> Tuple[Optional[float], str]:
        """
        Mean normalized concentration change |f - i| / max(|i|, |f|) in [0, 1] over simulated species.
        Species whose names contain the target symbol (e.g. 'Erk2-PP' for ERK2) are used when present.
        """
        def norm_change(sp: str) -> Optional[float]:
            f, i = _num(final.get(sp)), _num(initial.get(sp))
            if f is None or i is None:
                return None
            denom = max(abs(i), abs(f))
            return abs(f - i) / denom if denom > 0 else None

        names = ([target.split()[0]] if target else []) + [a for a in (aliases or []) if a]
        target_keys = [k for k in (re.sub(r"[^a-z0-9]", "", n.lower()) for n in names) if k]
        target_species = [sp for sp in final if _species_names_target(sp, target_keys)]
        for species, basis in ((target_species, "target species"), (list(final), "model species")):
            changes = [c for c in (norm_change(sp) for sp in species) if c is not None]
            if changes:
                return round(sum(changes) / len(changes), 3), f"{len(changes)} {basis}"
        return None, "no comparable species"

    @staticmethod
    def _single_cell_evidence_score(
        top_markers: Dict[str, List[Dict[str, Any]]],
        target_expression: Dict[str, Dict[str, float]],
        target: str,
        aliases: Optional[List[str]] = None,
    ) -> Tuple[Optional[float], str]:
        """
        Score single-cell evidence from FDR-significant (p_adj < 0.05) Wilcoxon markers, scaling |log2FC|
        so that a 4-unit change saturates at 1.0. Target-gene markers take precedence; otherwise the target's
        detected expression fraction; otherwise overall marker strength weighted by cluster coverage.
        """
        def strength(m: Dict[str, Any]) -> float:
            return min(1.0, abs(_num(m.get("log_fold_change")) or 0.0) / 4.0)

        markers = [m for ms in top_markers.values() for m in (ms or []) if isinstance(m, dict)]
        significant = [m for m in markers if (p := _num(m.get("p_adj"))) is not None and p < 0.05]
        target_upper = target.split()[0].upper() if target else ""
        target_names = {n for n in [target_upper, *(a.upper() for a in (aliases or []))] if n}

        target_hits = [m for m in significant if str(m.get("gene", "")).upper() in target_names]
        if target_hits:
            score = sum(strength(m) for m in target_hits) / len(target_hits)
            return round(score, 3), f"{target_upper} is a significant marker in {len(target_hits)} cluster(s)"

        if target_expression:
            pct = max((_num(v.get("percent_expressing_cells")) or 0.0) if isinstance(v, dict) else 0.0 for v in target_expression.values())
            return round(pct / 100.0, 3), f"{target_upper} detected in up to {pct:.1f}% of cells of a cluster; not a significant marker"

        if significant:
            clusters_with_sig = {str(m.get("cluster")) for m in significant}
            coverage = len(clusters_with_sig) / max(1, len(top_markers))
            score = coverage * sum(strength(m) for m in significant) / len(significant)
            return round(score, 3), f"{len(significant)} FDR-significant markers across {len(clusters_with_sig)} cluster(s)"

        return None, "no FDR-significant markers"


def _fmt(v: Optional[float]) -> str:
    return "Unknown" if v is None else f"{v:.2f}"
