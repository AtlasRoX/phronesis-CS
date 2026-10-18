"""Evidence XML, fallback Markdown, citation records and verifier identifiers for PrimeKG results (all values escaped)."""

from __future__ import annotations

from html import escape
from typing import Any, Dict, Iterable, List, Optional, Set

HOW_TO_READ = (
    "PrimeKG is a curated graph integrating 20 resources (DrugBank, DisGeNET, Mondo, HPO, Reactome, GO, SIDER, "
    "Bgee, CTD and others). An edge records that a source asserts an association: indication, contraindication and "
    "off-label edges come from drug labels and clinical resources; disease-gene edges are associations, not causal "
    "proof; drug side-effect edges carry no frequency; drug-drug edges carry no severity. An indication or "
    "contraindication edge is a curated label fact and may be stated as such, naming PrimeKG as the source. "
    "Repurposing candidates and phenotype-ranked diseases are graph-derived hypotheses: call them hypotheses, give "
    "the score components and the calibration for this disease, and never present them as efficacy, safety or a "
    "diagnosis. Cite PrimeKG nodes by the DrugBank, MONDO or HP identifiers shown, and quote counts exactly."
)


def _a(v: Any) -> str:
    return escape("" if v is None else str(v), quote=True)


def _t(v: Any) -> str:
    return escape("" if v is None else str(v), quote=False)


def _names(items: Iterable[Dict[str, Any]], limit: int = 12, with_id: bool = True) -> str:
    out = []
    for it in list(items)[:limit]:
        cid = it.get("curie") if with_id else None
        out.append(f"{it.get('name')} ({cid})" if cid else str(it.get("name")))
    return "; ".join(out)


def _sec(tag: str, sec: Optional[Dict[str, Any]], limit: int = 12, with_id: bool = True) -> str:
    if not sec or not sec.get("total"):
        return ""
    return f'<{tag} total="{_a(sec["total"])}">{_t(_names(sec.get("items") or [], limit, with_id))}</{tag}>'


def _clinical(clin: Dict[str, Any], keys: Iterable[str]) -> str:
    parts = []
    for k in keys:
        v = clin.get(k)
        if v:
            parts.append(f'<{k}>{_t("; ".join(map(str, v)) if isinstance(v, list) else v)}</{k}>')
    return "".join(parts)


def analysis_xml(entry: Dict[str, Any]) -> str:
    op, res = entry.get("operation"), entry.get("result") or {}
    if op == "drug_profile":
        d = res.get("drug") or {}
        roles = res.get("proteins") or {}
        return "".join([
            f'<drug_profile name="{_a(d.get("name"))}" drugbank_id="{_a(d.get("id"))}">',
            _clinical(res.get("clinical") or {}, ("groups", "atc", "indication_text", "mechanism_of_action", "half_life",
                                                   "protein_binding")),
            _sec("indications", res.get("indications")), _sec("contraindications", res.get("contraindications")),
            _sec("off_label_uses", res.get("off_label_uses")),
            *[_sec(f"{role}s", roles.get(role), 15, with_id=False) for role in ("target", "enzyme", "transporter", "carrier")],
            _sec("side_effects", res.get("side_effects"), 15, with_id=False),
            f'<drug_interactions total="{_a((res.get("drug_interactions") or {}).get("total"))}"/>',
            "</drug_profile>",
        ])
    if op == "disease_profile":
        d = res.get("disease") or {}
        return "".join([
            f'<disease_profile name="{_a(d.get("name"))}" mondo_ids="{_a(", ".join((d.get("mondo_ids") or [])[:6]))}">',
            _clinical(res.get("clinical") or {}, ("definition", "prevalence", "symptoms", "causes",
                                                   "management_and_treatment", "complications")),
            _sec("indicated_drugs", res.get("indicated_drugs"), 20), _sec("contraindicated_drugs", res.get("contraindicated_drugs")),
            _sec("off_label_drugs", res.get("off_label_drugs")),
            _sec("associated_genes", res.get("genes"), 20, with_id=False),
            _sec("phenotypes_present", res.get("phenotypes_present"), 15),
            _sec("phenotypes_absent", res.get("phenotypes_absent"), 8),
            _sec("related_diseases", res.get("related_diseases"), 10), _sec("exposures", res.get("exposures"), 8, with_id=False),
            *[_related_indications_xml(r) for r in res.get("indications_from_related") or []],
            "</disease_profile>",
        ])
    if op == "gene_profile":
        g = res.get("gene") or {}
        drugs = res.get("drugs") or {}
        ctx_names = ", ".join(str(d.get("name")) for d in ((res.get("context") or {}).get("diseases") or []))
        ranked = (f' ranked_for="{_a(ctx_names)}" context_matched_diseases="{_a((res.get("diseases") or {}).get("context_matches"))}"'
                  if ctx_names else "")
        return "".join([
            f'<gene_profile symbol="{_a(g.get("name"))}" ncbi_gene="{_a(g.get("id"))}"{ranked}>',
            *[_sec(f"drugs_{role}", drugs.get(role), 12) for role in ("target", "enzyme", "transporter", "carrier")],
            _sec("diseases", res.get("diseases"), 15), _sec("pathways", res.get("pathways"), 12),
            _sec("biological_processes", res.get("biological_processes"), 10, with_id=False),
            _sec("molecular_functions", res.get("molecular_functions"), 8, with_id=False),
            _sec("phenotypes", res.get("phenotypes"), 10), _sec("expressed_in", res.get("expressed_in"), 10, with_id=False),
            _sec("interactors", res.get("interactors"), 15, with_id=False),
            "</gene_profile>",
        ])
    if op == "paths":
        return _paths_xml(res)
    if op == "drug_disease":
        d, dis = res.get("drug") or {}, res.get("disease") or {}
        adj = res.get("disease_genes_adjacent_to_targets") or {}
        return "".join([
            f'<drug_disease drug="{_a(d.get("name"))}" drugbank_id="{_a(d.get("id"))}" disease="{_a(dis.get("name"))}" '
            f'mondo_id="{_a(dis.get("curie"))}" status="{_a(res.get("status"))}" '
            f'drug_targets="{_a(res.get("drug_targets_total"))}" disease_genes="{_a(res.get("disease_genes_total"))}">',
            f'<shared_genes>{_t(_names(res.get("shared_genes") or [], 20, False))}</shared_genes>',
            f'<disease_genes_one_ppi_step_from_targets total="{_a(adj.get("total"))}">{_t(_names(adj.get("items") or [], 12, False))}'
            "</disease_genes_one_ppi_step_from_targets>",
            f'<indicated_for_related_diseases>{_t(_names(res.get("indicated_for_related_diseases") or [], 8))}</indicated_for_related_diseases>',
            _paths_xml(res.get("paths") or {}),
            "</drug_disease>",
        ])
    if op == "repurposing":
        return _repurposing_xml(res)
    if op == "phenotype_diagnosis":
        lines = [f'<phenotype_differential present="{_a(_names(res.get("present") or [], 20))}" '
                 f'absent="{_a(_names(res.get("absent") or [], 10))}">',
                 f"<method>{_t(res.get('method'))} {_t(res.get('caveat'))}</method>"]
        for k, r in enumerate(res.get("ranking") or [], 1):
            lines.append(
                f'<disease rank="{k}" name="{_a(r.get("name"))}" mondo_id="{_a(r.get("curie"))}" score="{_a(r.get("score"))}" '
                f'relative_score="{_a(r.get("relative_score"))}" coverage="{_a(r.get("coverage"))}" '
                f'matched="{_a("; ".join(r.get("matched_query_phenotypes") or []))}" '
                f'genes="{_a(", ".join(r.get("top_genes") or []))}" drugs="{_a(", ".join(r.get("indicated_drugs") or []))}"/>'
            )
        lines.append("</phenotype_differential>")
        return "".join(lines)
    if op == "polypharmacy":
        lines = [f"<polypharmacy drugs=\"{_a(_names(res.get('drugs') or [], 10))}\"><caveat>{_t(res.get('caveat'))}</caveat>"]
        for p in res.get("pairs") or []:
            a, b = (p.get("drugs") or [{}, {}])[:2]
            sh = p.get("shared") or {}
            lines.append(
                f'<pair a="{_a(a.get("name"))}" b="{_a(b.get("name"))}" recorded_interaction="{_a(str(p.get("recorded_interaction")).lower())}" '
                f'shared_enzymes="{_a(", ".join(sh.get("enzyme") or []))}" shared_transporters="{_a(", ".join(sh.get("transporter") or []))}" '
                f'shared_targets="{_a(", ".join(sh.get("target") or []))}" shared_side_effects="{_a(p.get("shared_side_effects_total"))}"/>'
            )
        for c in res.get("disease_contraindications") or []:
            lines.append(f'<contraindicated_in disease="{_a((c.get("disease") or {}).get("name"))}" '
                         f'drugs="{_a(", ".join(c.get("contraindicated_drugs") or []))}"/>')
        lines.append("</polypharmacy>")
        return "".join(lines)
    if op == "enrichment":
        lines = [f'<gene_set_enrichment genes="{_a(len(res.get("genes") or []))}"><method>{_t(res.get("method"))}</method>']
        for name, col in (res.get("collections") or {}).items():
            lines.append(f'<collection name="{_a(name)}" universe="{_a(col.get("universe"))}" annotated="{_a(col.get("query_annotated"))}">')
            for t in (col.get("terms") or [])[:8]:
                lines.append(f'<term name="{_a(t.get("name"))}" id="{_a(t.get("curie"))}" overlap="{_a(t.get("overlap"))}" '
                             f'size="{_a(t.get("term_size"))}" fold="{_a(t.get("fold_enrichment"))}" p="{_a(t.get("p_value"))}" '
                             f'q="{_a(t.get("q_value"))}" genes="{_a(", ".join(t.get("genes") or []))}"/>')
            lines.append("</collection>")
        lines.append("</gene_set_enrichment>")
        return "".join(lines)
    if op == "similar":
        node = res.get("node") or {}
        lines = [f'<similar_to name="{_a(node.get("name"))}" type="{_a(node.get("type"))}">']
        for s in res.get("similar") or []:
            lines.append(f'<entity name="{_a(s.get("name"))}" id="{_a(s.get("curie"))}" jaccard="{_a(s.get("jaccard"))}" '
                         f'shared="{_a(s.get("shared_neighbors"))}" examples="{_a(", ".join(s.get("most_specific_shared") or []))}"/>')
        lines.append("</similar_to>")
        return "".join(lines)
    return ""


def _paths_xml(res: Dict[str, Any]) -> str:
    if not res:
        return ""
    s, t = res.get("source") or {}, res.get("target") or {}
    lines = [f'<mechanism_paths from="{_a(s.get("name"))}" to="{_a(t.get("name"))}" shortest_hops="{_a(res.get("shortest_length"))}" '
             f'hubs_avoided="{_a(str(res.get("hubs_avoided")).lower())}">']
    for p in (res.get("paths") or [])[:6]:
        lines.append(f"<path>{_t(p.get('text'))}</path>")
    for m in (res.get("metapaths") or [])[:4]:
        lines.append(f'<pattern count="{_a(m.get("count"))}">{_t(m.get("pattern"))}</pattern>')
    lines.append("</mechanism_paths>")
    return "".join(lines)


def _repurposing_xml(res: Dict[str, Any]) -> str:
    d = res.get("disease") or {}
    cal = res.get("calibration") or {}
    similar = "; ".join(f"{s.get('name')} ({s.get('similarity')})" for s in (res.get("similar_diseases") or [])[:6])
    lines = [
        f'<repurposing disease="{_a(d.get("name"))}" mondo_id="{_a(d.get("curie"))}" disease_genes="{_a(res.get("disease_genes_total"))}">',
        f"<method>{_t(res.get('method'))}</method><caveat>{_t(res.get('caveat'))}</caveat>",
        f'<calibration signal="{_a(cal.get("signal"))}" auroc="{_a(cal.get("auroc_known_indications"))}" '
        f'known_indications="{_a(cal.get("known_indications"))}" in_top_20="{_a(cal.get("in_top_20"))}" '
        f'in_top_100="{_a(cal.get("in_top_100"))}" median_rank="{_a(cal.get("median_rank"))}" drugs_ranked="{_a(cal.get("drugs_ranked"))}">'
        f"{_t(cal.get('note'))}</calibration>",
        f'<candidate_filter experimental_not_listed="{_a(res.get("experimental_compounds_not_listed"))}">'
        f"{_t(res.get('candidate_filter'))}</candidate_filter>",
        f'<already_indicated>{_t(_names(res.get("already_indicated") or [], 15))}</already_indicated>',
        f"<similar_diseases>{_t(similar)}</similar_diseases>",
    ]
    for p in res.get("indications_borrowed_from") or []:
        lines.insert(1, f'<indications_borrowed_from disease="{_a(p.get("name"))}" mondo_id="{_a(p.get("curie"))}" '
                        f'indications="{_a(p.get("indications"))}">{_a(d.get("name"))} has no recorded indications in '
                        f'PrimeKG; the known indications used for calibration and exclusion are those of '
                        f'{_t(p.get("name"))}: {_t(_names(res.get("borrowed_indications") or [], 12, False))}'
                        "</indications_borrowed_from>")
    if res.get("mutant_genes"):
        lines.append(f'<pathway_context mutant_genes="{_a(", ".join(res["mutant_genes"]))}" '
                     f'neighbourhood_genes="{_a(res.get("pathway_neighbourhood_size"))}">Candidates targeting the '
                     "mutated gene's pathway neighbourhood are tagged and ranked up; this is a prior from the "
                     "question, not graph evidence.</pathway_context>")
    if res.get("excluded_non_therapeutic"):
        ex = "; ".join(f"{e.get('name')} ({e.get('reason')})" for e in res["excluded_non_therapeutic"][:10])
        lines.append(f'<excluded_non_therapeutic total="{_a(res.get("excluded_non_therapeutic_total"))}">{_t(ex)}'
                     "</excluded_non_therapeutic>")
    for k, c in enumerate(res.get("candidates") or [], 1):
        comp = c.get("components") or {}
        prox = c.get("network_proximity") or {}
        # Only the signals that scored are written, so no attribute suggests evidence the drug lacks.
        attrs = {
            "rank": k, "name": c.get("name"), "drugbank_id": c.get("id"), "score": c.get("score"),
            "approval": ", ".join(c.get("approval_groups") or []) or None, "status": c.get("status_for_disease"),
            "target_overlap": comp.get("target_overlap") or None,
            "network_proximity": comp.get("network_proximity") or None,
            "similar_disease_indication": comp.get("similar_disease_indication") or None,
            "proximity_z": prox.get("z_score") if comp.get("network_proximity") else None,
            "tags": ", ".join(c.get("tags") or []) or None,
            "pathway_targets": ", ".join(c.get("pathway_targets") or []) or None,
        }
        attr_text = " ".join(f'{key}="{_a(v)}"' for key, v in attrs.items() if v is not None)
        mech = f"<mechanism>{_t(c['mechanism_of_action'])}</mechanism>" if c.get("mechanism_of_action") else ""
        lines.append(f"<candidate {attr_text}><basis>{_t(c.get('basis') or '')}</basis>{mech}</candidate>")
    for c in res.get("pathway_candidates") or []:
        # Drugs on the mutated gene's core axis with little graph signal for the disease itself.
        lines.append(f'<pathway_candidate name="{_a(c.get("name"))}" drugbank_id="{_a(c.get("id"))}" '
                     f'score="{_a(c.get("score"))}" approval="{_a(", ".join(c.get("approval_groups") or []))}" '
                     f'pathway_targets="{_a(", ".join(c.get("pathway_targets") or []))}"/>')
    for c in res.get("contraindicated_but_scored") or []:
        lines.append(f'<contraindicated_candidate name="{_a(c.get("name"))}" drugbank_id="{_a(c.get("id"))}" score="{_a(c.get("score"))}"/>')
    lines.append("</repurposing>")
    return "".join(lines)


def _related_indications_xml(rel: Dict[str, Any]) -> str:
    d = rel.get("disease") or {}
    return (f'<indications_from_related disease="{_a(d.get("name"))}" mondo_id="{_a(d.get("curie"))}">'
            + _sec("indicated_drugs", rel.get("indicated_drugs"), 20) + _sec("off_label_drugs", rel.get("off_label_drugs"))
            + "</indications_from_related>")


def evidence_xml(data: Dict[str, Any]) -> str:
    prov = data.get("provenance") or {}
    lines = [f'<primekg source="PrimeKG {_a(prov.get("dataverse_version"))} ({_a(prov.get("dataverse_doi"))})" '
             f'citation="{_a(prov.get("citation"))}">', f"<how_to_read>{_t(HOW_TO_READ)}</how_to_read>"]
    for entry in data.get("analyses") or []:
        if entry.get("error"):
            lines.append(f'<analysis_failed operation="{_a(entry.get("operation"))}">{_t(entry["error"])}</analysis_failed>')
            continue
        lines.append(analysis_xml(entry))
    if data.get("unresolved"):
        lines.append(f"<unresolved>{_t(', '.join(data['unresolved']))}</unresolved>")
    lines.append("</primekg>")
    return "\n".join(x for x in lines if x)


# -----------------------------------------------------------------------------
# identifiers and citations
# -----------------------------------------------------------------------------

def _walk_nodes(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, dict):
        if "index" in obj and "type" in obj and "name" in obj:
            yield obj
        for v in obj.values():
            yield from _walk_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_nodes(v)


def identifiers(data: Dict[str, Any]) -> Dict[str, Set[str]]:
    drugbank: Set[str] = set()
    ontology: Set[str] = set()
    for n in _walk_nodes(data.get("analyses") or []):
        if n.get("type") == "drug" and n.get("id"):
            drugbank.add(str(n["id"]).upper())
        curie = str(n.get("curie") or "")
        if curie.startswith(("MONDO_", "HP:")):
            ontology.add(curie.replace(":", "_"))
        for m in n.get("mondo_ids") or []:
            ontology.add(str(m))
    for entry in data.get("analyses") or []:
        for r in (entry.get("result") or {}).get("candidates") or []:
            for s in r.get("indicated_for_similar") or []:
                if s.get("curie"):
                    ontology.add(str(s["curie"]).replace(":", "_"))
    return {"drugbank": drugbank, "ontology": ontology}


def citation_dicts(data: Dict[str, Any], limit: int = 120) -> List[Dict[str, Any]]:
    """drugbank records for drug nodes and url records for MONDO / HPO nodes (deduplicated)."""
    seen: Set[str] = set()
    out: List[Dict[str, Any]] = []
    for n in _walk_nodes(data.get("analyses") or []):
        t, nid, url = n.get("type"), str(n.get("id") or ""), n.get("url")
        if t == "drug" and nid and nid not in seen:
            seen.add(nid)
            out.append({"type": "drugbank", "id": nid, "title": f"{n.get('name')} (DrugBank via PrimeKG)", "url": url})
        elif t in ("disease", "effect/phenotype") and url and url not in seen:
            seen.add(url)
            out.append({"type": "url", "id": url, "title": f"{n.get('name')} ({n.get('curie')}, PrimeKG)", "url": url})
        if len(out) >= limit:
            break
    return out


def fallback_markdown(data: Dict[str, Any]) -> str:
    """Deterministic summary when the synthesis LLM is unavailable."""
    out = ["### PrimeKG knowledge graph"]
    for entry in data.get("analyses") or []:
        op, res = entry.get("operation"), entry.get("result") or {}
        if entry.get("error"):
            out.append(f"- {op}: failed ({entry['error']})")
            continue
        if op == "drug_profile":
            d = res.get("drug") or {}
            out.append(f"**{d.get('name')}** ({d.get('id')}): indicated for {res['indications']['total']} diseases "
                       f"({_names(res['indications']['items'], 6, False) or 'none listed'}); "
                       f"{res['contraindications']['total']} contraindications; targets: "
                       f"{_names(res['proteins']['target']['items'], 8, False) or 'none listed'}.")
        elif op == "disease_profile":
            d = res.get("disease") or {}
            out.append(f"**{d.get('name')}** ({d.get('curie')}): {res['genes']['total']} associated genes, "
                       f"{res['indicated_drugs']['total']} indicated drugs ({_names(res['indicated_drugs']['items'], 8, False) or 'none'}).")
            for rel in res.get("indications_from_related") or []:
                sec = rel.get("indicated_drugs") or {}
                out.append(f"  - indicated for the related {(rel.get('disease') or {}).get('name')}: "
                           f"{_names(sec.get('items') or [], 8, False) or 'none'}")
        elif op == "gene_profile":
            g = res.get("gene") or {}
            out.append(f"**{g.get('name')}** (NCBI Gene {g.get('id')}): {res['diseases']['total']} diseases, "
                       f"{res['drugs']['target']['total']} drugs targeting it, {res['interactors']['total']} interactors.")
        elif op == "drug_disease":
            out.append(f"**{(res.get('drug') or {}).get('name')} → {(res.get('disease') or {}).get('name')}**: "
                       f"{res.get('status')}; shared genes: {_names(res.get('shared_genes') or [], 8, False) or 'none'}.")
            for p in ((res.get("paths") or {}).get("paths") or [])[:3]:
                out.append(f"  - {p['text']}")
        elif op == "paths":
            out.append(f"Paths {(res.get('source') or {}).get('name')} → {(res.get('target') or {}).get('name')} "
                       f"({res.get('shortest_length') or 'none'} hops):")
            for p in (res.get("paths") or [])[:4]:
                out.append(f"  - {p['text']}")
        elif op == "repurposing":
            cal = res.get("calibration") or {}
            borrowed = ", ".join(str(p.get("name")) for p in res.get("indications_borrowed_from") or [])
            out.append(f"Repurposing hypotheses for **{(res.get('disease') or {}).get('name')}** "
                       f"(calibration: {cal.get('in_top_100')}/{cal.get('known_indications')} known indications rank in the top 100"
                       f"{f'; known indications from {borrowed}' if borrowed else ''}):")
            for c in (res.get("candidates") or [])[:8]:
                tags = f" [{', '.join(c['tags'])}]" if c.get("tags") else ""
                out.append(f"  - {c['name']} ({c['id']}), score {c['score']}{tags}")
            if res.get("pathway_candidates"):
                out.append(f"  {'/'.join(res.get('mutant_genes') or [])}-pathway drugs: " + ", ".join(
                    f"{c['name']} ({', '.join(c.get('pathway_targets') or [])})" for c in res["pathway_candidates"][:6]))
            out.append(f"  _{res.get('caveat')}_")
        elif op == "phenotype_diagnosis":
            out.append("Phenotype-ranked diseases: " + "; ".join(
                f"{r['name']} ({r['coverage']})" for r in (res.get("ranking") or [])[:8]) + f". _{res.get('caveat')}_")
        elif op == "polypharmacy":
            for p in res.get("pairs") or []:
                if p.get("flags"):
                    out.append(f"- {p['drugs'][0]['name']} + {p['drugs'][1]['name']}: {'; '.join(p['flags'])}")
        elif op == "enrichment":
            for name, col in (res.get("collections") or {}).items():
                terms = col.get("terms") or []
                if terms:
                    out.append(f"- {name}: " + "; ".join(f"{t['name']} (q={t['q_value']})" for t in terms[:4]))
        elif op == "similar":
            out.append("Similar: " + ", ".join(s["name"] for s in (res.get("similar") or [])[:8]))
    prov = data.get("provenance") or {}
    out.append(f"\n_Source: PrimeKG {prov.get('dataverse_version') or ''} ({prov.get('dataverse_doi')})._")
    return "\n".join(out)
