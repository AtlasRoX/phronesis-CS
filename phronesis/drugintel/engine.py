"""
DrugIntelEngine: the three drug-intelligence registry tools, each returning a ToolResult.

  chembl_resolve_drugs  exact ChEMBL name/synonym/id resolution collapsed to parent molecules
  chembl_drug_profile   mechanisms (by parent), IC50/Ki/Kd activities against a target, approved drugs for a target
  openfda_drug_safety   FAERS reaction counts, total and serious report counts, a truncated label excerpt, and the
                        Drugs@FDA approval status (application number, first approval, efficacy supplements)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from ..tools.registry import ErrorType, ToolResult
from .chembl_client import COMPOUND_URL, TARGET_URL, ChemblClient, ChemblError
from .drugsfda import DRUGSFDA_LIMIT, drugsfda_search, summarize_approval
from .names import normalize_target_symbol, us_drug_name
from .openfda_client import (
    DAILYMED_URL,
    OpenFdaClient,
    OpenFdaError,
    faers_generic_search,
    faers_search,
    is_combination,
    label_exact_search,
    label_fallback_search,
    label_search,
    single_ingredient_name,
)
from .schemas import (
    FAERS_CAVEAT,
    ApprovedDrug,
    BioactiveCompound,
    Bioactivity,
    ChemblProfileOutput,
    ChemblResolveOutput,
    DrugRecord,
    FaersReaction,
    FdaApproval,
    LabelExcerpt,
    Mechanism,
    OpenFdaSafetyOutput,
    TargetRef,
)

logger = logging.getLogger(__name__)

BOXED_WARNING_MAX = 2000
WARNINGS_MAX = 1500
CONTRAINDICATIONS_MAX = 600
INDICATIONS_MAX = 1500
MAX_TRADE_NAMES = 6
MAX_APPROVED_FOR_TARGET = 25
MAX_APPROVED_MECHANISM_ROWS = 500
# The label search can take four requests; past this deadline it is abandoned so the FAERS counts already
# fetched are still returned inside the 35 s tool timeout.
LABEL_DEADLINE_S = 20.0
# The Drugs@FDA lookup is one request (plus one retry on a 5xx); it runs next to the FAERS and label requests.
APPROVAL_DEADLINE_S = 20.0
# Compounds with measured activity against a target, shown when no drug is named.
MAX_BIOACTIVE_FOR_TARGET = 15
TARGET_ACTIVITY_ROWS = 200
# FAERS reaction counts: fetch more terms than are shown, since the non-reaction terms are dropped.
FAERS_FETCH = 40
# chembl_resolve_drugs takes up to 8 names (schemas.MAX_RESOLVE_NAMES); at most this many resolve at once, so a
# long list does not put more parallel load on ChEMBL than the former three-name cap did.
NAME_RESOLVE_CONCURRENCY = 3

# MedDRA preferred terms that FAERS counts as "reactions" but that are outcomes, the treated disease, or
# product-use / efficacy statements rather than adverse reactions to the drug.
NON_REACTION_TERMS = frozenset({
    "DEATH",
    "MALIGNANT NEOPLASM PROGRESSION",
    "NEOPLASM PROGRESSION",
    "DISEASE PROGRESSION",
    "OFF LABEL USE",
    "PRODUCT USE IN UNAPPROVED INDICATION",
    "PRODUCT USE ISSUE",
    "INTENTIONAL PRODUCT USE ISSUE",
    "DRUG INEFFECTIVE",
    "THERAPY NON-RESPONDER",
    "NO ADVERSE EVENT",
    "INAPPROPRIATE SCHEDULE OF PRODUCT ADMINISTRATION",
    "PRODUCT DOSE OMISSION ISSUE",
    "WRONG TECHNIQUE IN PRODUCT USAGE PROCESS",
    "TREATMENT FAILURE",
})
NON_REACTION_RE = re.compile(r"PRODUCT .* ISSUE|OFF LABEL|UNAPPROVED", re.IGNORECASE)


def is_non_reaction_term(term: str) -> bool:
    """True for a FAERS MedDRA term that is not an adverse reaction (death, progression, off-label use, ...)."""
    t = " ".join(str(term or "").upper().split())
    return t in NON_REACTION_TERMS or bool(NON_REACTION_RE.search(t))


def _float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    f = _float(value)
    return int(f) if f is not None else None


def max_phase_meaning(phase: Optional[float]) -> str:
    """ChEMBL max_phase semantics: 4 is approval by at least one regulator anywhere, not FDA approval."""
    if phase is None:
        return "preclinical or no phase recorded"
    if phase >= 4:
        return "approved by at least one regulator (not FDA-specific)"
    if phase in (1.0, 2.0, 3.0):
        return f"reached phase {int(phase)} clinical trials"
    if phase == 0.5:
        return "early phase 1"
    if phase < 0:
        return "phase unknown"
    return f"max phase {phase:g}"


def format_phase(phase: Optional[float]) -> str:
    return "none" if phase is None else f"{phase:g}"


def _parent_of(mol: Dict[str, Any]) -> str:
    return str((mol.get("molecule_hierarchy") or {}).get("parent_chembl_id") or mol.get("molecule_chembl_id"))


def _trade_names(mols: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for mol in mols:
        for syn in mol.get("molecule_synonyms") or []:
            if syn.get("syn_type") != "TRADE_NAME":
                continue
            name = str(syn.get("molecule_synonym") or "").strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    return out[:MAX_TRADE_NAMES]


def _drug_record(
    parent: Dict[str, Any], *, query_name: str, matched_via: str, forms: List[str], related: List[Dict[str, Any]]
) -> DrugRecord:
    phase = _float(parent.get("max_phase"))
    withdrawn = parent.get("withdrawn_flag")
    return DrugRecord(
        query_name=query_name,
        chembl_id=str(parent.get("molecule_chembl_id")),
        pref_name=parent.get("pref_name"),
        forms=forms,
        matched_via=matched_via,  # type: ignore[arg-type]
        max_phase=phase,
        max_phase_meaning=max_phase_meaning(phase),
        first_approval=_int(parent.get("first_approval")),
        molecule_type=parent.get("molecule_type"),
        chembl_black_box_flag=_int(parent.get("black_box_warning")),
        withdrawn_flag=bool(withdrawn) if withdrawn is not None else None,
        trade_names=_trade_names([parent, *related]),
    )


def _bioactivity(row: Dict[str, Any]) -> Optional[Bioactivity]:
    """Only the standard_* fields (ChEMBL-normalised units); raw value/units are never copied."""
    activity_id = _int(row.get("activity_id"))
    if activity_id is None or not row.get("assay_chembl_id") or not row.get("standard_type"):
        return None
    return Bioactivity(
        activity_id=activity_id,
        assay_chembl_id=str(row["assay_chembl_id"]),
        assay_description=row.get("assay_description"),
        assay_type=row.get("assay_type"),
        document_chembl_id=row.get("document_chembl_id"),
        molecule_chembl_id=str(row.get("molecule_chembl_id") or ""),
        target_chembl_id=str(row.get("target_chembl_id") or ""),
        target_organism=row.get("target_organism"),
        standard_type=str(row["standard_type"]),
        standard_relation=row.get("standard_relation"),
        standard_value=_float(row.get("standard_value")),
        standard_units=row.get("standard_units"),
        pchembl_value=_float(row.get("pchembl_value")),
        data_validity_comment=row.get("data_validity_comment"),
    )


def _mechanisms(rows: List[Dict[str, Any]], parent_id: str, target_names: Dict[str, Optional[str]]) -> List[Mechanism]:
    """One Mechanism per (action type, target, mechanism text). ChEMBL records the same mechanism on the parent
    and on its salt forms (niraparib and niraparib tosylate); the parent-form row is kept."""
    by_key: "OrderedDict[Tuple[str, str, str], Mechanism]" = OrderedDict()
    for r in rows:
        moa = str(r.get("mechanism_of_action") or "").strip()
        if not moa:
            continue
        mech = Mechanism(
            mechanism_of_action=moa,
            action_type=r.get("action_type"),
            target_chembl_id=r.get("target_chembl_id"),
            target_pref_name=target_names.get(r.get("target_chembl_id")),
            form_chembl_id=str(r.get("molecule_chembl_id") or parent_id),
            max_phase=_float(r.get("max_phase")),
        )
        key = (str(mech.action_type or "").upper(), str(mech.target_chembl_id or ""), " ".join(moa.lower().split()))
        kept = by_key.get(key)
        if kept is None or (kept.form_chembl_id != parent_id and mech.form_chembl_id == parent_id):
            by_key[key] = mech
    return list(by_key.values())


def _bioactive_compounds(rows: List[Dict[str, Any]], limit: int) -> Tuple[List[BioactiveCompound], int]:
    """(top distinct compounds by best pChEMBL, number of distinct compounds) from target activity rows, keyed
    by parent molecule so a salt form does not count as a second compound."""
    best: "OrderedDict[str, BioactiveCompound]" = OrderedDict()
    for r in rows:
        mid = str(r.get("parent_molecule_chembl_id") or r.get("molecule_chembl_id") or "")
        if not mid:
            continue
        pchembl = _float(r.get("pchembl_value"))
        seen = best.get(mid)
        if seen is not None:
            seen.records += 1
            if pchembl is not None and (seen.best_pchembl is None or pchembl > seen.best_pchembl):
                seen.best_pchembl = pchembl
                seen.standard_type = r.get("standard_type")
                seen.standard_relation = r.get("standard_relation")
                seen.standard_value = _float(r.get("standard_value"))
                seen.standard_units = r.get("standard_units")
                seen.assay_chembl_id = r.get("assay_chembl_id")
                seen.activity_id = _int(r.get("activity_id"))
            continue
        best[mid] = BioactiveCompound(
            chembl_id=mid,
            pref_name=r.get("molecule_pref_name") or None,
            best_pchembl=pchembl,
            standard_type=r.get("standard_type"),
            standard_relation=r.get("standard_relation"),
            standard_value=_float(r.get("standard_value")),
            standard_units=r.get("standard_units"),
            assay_chembl_id=r.get("assay_chembl_id"),
            activity_id=_int(r.get("activity_id")),
        )
    ranked = sorted(best.values(), key=lambda c: -(c.best_pchembl if c.best_pchembl is not None else -1.0))
    return ranked[:limit], len(best)


def truncate_section(text: Optional[str], limit: int) -> Tuple[Optional[str], bool]:
    """A verbatim prefix of at most `limit` characters, cut at a word boundary; (text, truncated)."""
    if text is None:
        return None, False
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.8:
        cut = cut[:space]
    return cut.rstrip(), True


def _failure(tool_name: str, message: str, error_type: ErrorType, t0: float) -> ToolResult:
    return ToolResult(
        success=False,
        tool_name=tool_name,
        error=message,
        error_type=error_type,
        execution_time_seconds=time.perf_counter() - t0,
    )


def _report_total(body: Optional[Dict[str, Any]], found: bool) -> int:
    """The FAERS report count in a total_reports response (0 when openFDA found none)."""
    return int((((body or {}).get("meta") or {}).get("results") or {}).get("total") or 0) if found else 0


def _openfda_error_type(exc: OpenFdaError) -> ErrorType:
    return ErrorType.TIMEOUT if exc.kind == "timeout" else ErrorType.UPSTREAM_API_ERROR


class DrugIntelEngine:
    def __init__(self, chembl: Optional[ChemblClient] = None, openfda: Optional[OpenFdaClient] = None) -> None:
        self.chembl = chembl or ChemblClient()
        self.openfda = openfda or OpenFdaClient()

    # -------------------------------------------------------------------------
    # chembl_resolve_drugs
    # -------------------------------------------------------------------------
    async def _resolve_name(self, name: str) -> Optional[DrugRecord]:
        hits = await self.chembl.molecules_by_pref_name(name)
        via = "pref_name"
        if not hits:
            hits = await self.chembl.molecules_by_synonym(name)
            via = "synonym"
        if not hits:
            return None
        groups: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
        for mol in hits:
            groups.setdefault(_parent_of(mol), []).append(mol)
        parent_id, members = next(iter(groups.items()))
        parent = next((m for m in members if m.get("molecule_chembl_id") == parent_id), None)
        if parent is None:
            parent = await self.chembl.molecule(parent_id) or members[0]
        forms = [str(m["molecule_chembl_id"]) for m in members if m.get("molecule_chembl_id") != parent_id]
        return _drug_record(parent, query_name=name, matched_via=via, forms=forms, related=members)

    async def _resolve_id(self, chembl_id: str) -> Optional[DrugRecord]:
        mol = await self.chembl.molecule(chembl_id)
        if mol is None:
            return None
        parent_id = _parent_of(mol)
        if parent_id == chembl_id:
            return _drug_record(mol, query_name=chembl_id, matched_via="chembl_id", forms=[], related=[])
        parent = await self.chembl.molecule(parent_id) or mol
        return _drug_record(parent, query_name=chembl_id, matched_via="chembl_id", forms=[chembl_id], related=[mol])

    async def _resolve_target(self, symbol: Optional[str]) -> Tuple[Optional[TargetRef], str, Optional[ChemblError]]:
        sym = normalize_target_symbol(symbol)
        if not sym:
            return None, "skipped", None
        try:
            targets = await self.chembl.human_target_for_symbol(sym)
        except ChemblError as exc:
            logger.warning("ChEMBL target lookup for %s failed: %s", sym, exc)
            return None, "error", exc
        if not targets:
            return None, "not_found", None
        t = targets[0]
        ref = TargetRef(target_chembl_id=str(t["target_chembl_id"]), pref_name=t.get("pref_name"), symbol_query=sym)
        return ref, "resolved", None

    async def resolve_drugs(
        self, names: Optional[List[str]] = None, chembl_ids: Optional[List[str]] = None, target_symbol: Optional[str] = None
    ) -> ToolResult:
        t0 = time.perf_counter()
        names = list(dict.fromkeys(n.strip() for n in (names or []) if n and n.strip()))
        chembl_ids = [c.strip().upper() for c in (chembl_ids or []) if c and c.strip()]
        sem = asyncio.Semaphore(NAME_RESOLVE_CONCURRENCY)

        async def bounded(coro: Any) -> Any:
            async with sem:
                return await coro

        # Each name resolves independently: one slow or failing lookup must not discard the others.
        release, target_info, *resolved = await asyncio.gather(
            self.chembl.release(),
            self._resolve_target(target_symbol),
            *(bounded(self._resolve_name(n)) for n in names),
            *(bounded(self._resolve_id(c)) for c in chembl_ids),
            return_exceptions=True,
        )
        if isinstance(release, BaseException):
            release = None
        if isinstance(target_info, BaseException):
            logger.warning("ChEMBL target lookup failed: %s", target_info)
            target_info = (None, "error", ChemblError(f"{type(target_info).__name__}: {target_info}", kind="internal"))
        queries = [*names, *chembl_ids]
        failures = [(q, r) for q, r in zip(queries, resolved) if isinstance(r, BaseException)]
        for _, exc in failures:
            if not isinstance(exc, ChemblError):
                raise exc
        if queries and len(failures) == len(queries):
            return _failure("chembl_resolve_drugs", str(failures[0][1]), ErrorType.UPSTREAM_API_ERROR, t0)

        target, target_lookup, target_error = target_info
        drugs: List[DrugRecord] = []
        unresolved: List[str] = []
        invalid: List[str] = []
        lookup_errors = [{"query": q, "error": str(exc)[:240]} for q, exc in failures]
        for query, rec in zip(queries, resolved):
            if isinstance(rec, BaseException):
                continue
            if rec is None:
                (invalid if query in chembl_ids else unresolved).append(query)
                continue
            dup = next((d for d in drugs if d.chembl_id == rec.chembl_id), None)
            if dup is not None:  # "imatinib" and "Gleevec" in one question collapse to one parent
                dup.forms = list(dict.fromkeys([*dup.forms, *rec.forms]))
                continue
            drugs.append(rec)

        out = ChemblResolveOutput(
            chembl_release=release,
            target=target,
            target_lookup=target_lookup,  # type: ignore[arg-type]
            target_lookup_error=str(target_error)[:240] if target_error is not None else None,
            target_lookup_error_kind=getattr(target_error, "kind", None),
            drugs=drugs,
            unresolved_names=unresolved,
            invalid_chembl_ids=invalid,
            lookup_errors=lookup_errors,
        )
        ids = [i for d in drugs for i in (d.chembl_id, *d.forms)] + ([target.target_chembl_id] if target else [])
        return ToolResult(
            success=True,
            tool_name="chembl_resolve_drugs",
            data=out.model_dump(),
            canonical_ids={"chembl_ids": ",".join(dict.fromkeys(ids))} if ids else {},
            execution_time_seconds=time.perf_counter() - t0,
            metadata={"chembl_release": release, "source": "ChEMBL REST"},
        )

    # -------------------------------------------------------------------------
    # chembl_drug_profile
    # -------------------------------------------------------------------------
    async def _approved_for_target(self, target_chembl_id: str) -> Tuple[List[ApprovedDrug], Optional[int]]:
        """Approved drugs (by parent) acting on a target; the total is None when ChEMBL had more mechanism rows than
        were fetched, because the distinct-drug count is then unknown."""
        rows, total_rows = await self.chembl.approved_mechanisms_for_target(target_chembl_id, limit=MAX_APPROVED_MECHANISM_ROWS)
        moa_by_parent: "OrderedDict[str, Optional[str]]" = OrderedDict()
        for r in rows:
            parent = str(r.get("parent_molecule_chembl_id") or r.get("molecule_chembl_id") or "")
            if parent and parent not in moa_by_parent:
                moa_by_parent[parent] = r.get("mechanism_of_action")
        parents = list(moa_by_parent)[:MAX_APPROVED_FOR_TARGET]
        try:
            mols = {m.get("molecule_chembl_id"): m for m in await self.chembl.molecule_names_by_ids(parents)}
        except ChemblError as exc:
            # The approved drugs are known from their mechanisms; only their names and approval years are missing.
            logger.warning("ChEMBL molecule lookup for approved drugs on %s failed: %s", target_chembl_id, exc)
            mols = {}
        drugs = [
            ApprovedDrug(
                chembl_id=pid,
                pref_name=(mols.get(pid) or {}).get("pref_name"),
                mechanism_of_action=moa_by_parent[pid],
                first_approval=_int((mols.get(pid) or {}).get("first_approval")),
                molecule_type=(mols.get(pid) or {}).get("molecule_type"),
            )
            for pid in parents
        ]
        return drugs, (len(moa_by_parent) if total_rows <= len(rows) else None)

    async def _bioactive_for_target(
        self, target_chembl_id: str
    ) -> Tuple[List[BioactiveCompound], int, Optional[int], int]:
        """(top distinct compounds, distinct compounds among the rows fetched, activity records in ChEMBL, rows fetched)."""
        rows, total = await self.chembl.activities_for_target(target_chembl_id, limit=TARGET_ACTIVITY_ROWS)
        compounds, distinct = _bioactive_compounds(rows, MAX_BIOACTIVE_FOR_TARGET)
        if compounds:
            try:
                mols = {m.get("molecule_chembl_id"): m for m in await self.chembl.molecule_names_by_ids([c.chembl_id for c in compounds])}
            except ChemblError as exc:
                logger.warning("ChEMBL molecule lookup for target compounds failed: %s", exc)
                mols = {}
            for c in compounds:
                mol = mols.get(c.chembl_id) or {}
                c.pref_name = mol.get("pref_name") or c.pref_name
                c.max_phase = _float(mol.get("max_phase"))
                c.molecule_type = mol.get("molecule_type")
        return compounds, distinct, total, len(rows)

    async def drug_profile(
        self,
        parent_chembl_ids: Optional[List[str]] = None,
        target_chembl_id: Optional[str] = None,
        activity_limit: int = 10,
        target_symbol: Optional[str] = None,
        query_names: Optional[Dict[str, str]] = None,
        forms: Optional[Dict[str, List[str]]] = None,
        matched_via: Optional[Dict[str, str]] = None,
        include_target_approved: Optional[bool] = None,
        mentions: Optional[Dict[str, str]] = None,
        include_target_bioactive: Optional[bool] = None,
        target_pref_name: Optional[str] = None,
    ) -> ToolResult:
        t0 = time.perf_counter()
        parents = list(dict.fromkeys(parent_chembl_ids or []))
        query_names = query_names or {}
        forms = forms or {}
        matched_via = matched_via or {}
        mentions = mentions or {}
        want_approved = bool(target_chembl_id) and (include_target_approved if include_target_approved is not None else not parents)
        want_bioactive = bool(target_chembl_id) and (
            include_target_bioactive if include_target_bioactive is not None else not parents
        )

        async def no_activities() -> Tuple[List[Dict[str, Any]], Optional[int]]:
            return [], None

        async def no_approved() -> Tuple[List[ApprovedDrug], Optional[int]]:
            return [], None

        async def no_bioactive() -> Tuple[List[BioactiveCompound], Optional[int], Optional[int], Optional[int]]:
            return [], None, None, None

        async def no_names() -> None:
            return None

        # With no drug ids the only target name needed is the target's own, so fetch it alongside the rest
        # instead of after it (the EBI API can take 10-20 s per request); not at all when the resolve step
        # already returned it.
        early_names = bool(target_chembl_id) and not parents and not target_pref_name
        release, molecules, approved, bioactive, names_first, *per_drug = await asyncio.gather(
            self.chembl.release(),
            self.chembl.molecules_by_ids(parents),
            self._approved_for_target(target_chembl_id) if want_approved else no_approved(),
            self._bioactive_for_target(target_chembl_id) if want_bioactive else no_bioactive(),
            self.chembl.targets_by_ids([target_chembl_id]) if early_names else no_names(),
            *(self.chembl.mechanisms_for_parent(p) for p in parents),
            *(
                self.chembl.activities(p, target_chembl_id, activity_limit) if target_chembl_id else no_activities()
                for p in parents
            ),
            return_exceptions=True,
        )
        for part in (molecules, approved, *per_drug):
            if isinstance(part, BaseException) and not isinstance(part, ChemblError):
                raise part
        if isinstance(molecules, ChemblError):
            return _failure("chembl_drug_profile", str(molecules), ErrorType.UPSTREAM_API_ERROR, t0)
        if isinstance(release, BaseException):
            release = None
        lookup_errors: List[Dict[str, str]] = []
        approved_status: Optional[str] = "ok" if want_approved else None
        approved_error: Optional[str] = None
        approved_error_kind: Optional[str] = None
        if isinstance(approved, ChemblError):
            # A failed lookup is "unknown", never "0 approved drugs": the status travels with the empty list.
            approved_status, approved_error, approved_error_kind = "failed", str(approved)[:240], approved.kind
            lookup_errors.append({"query": f"approved drugs for {target_chembl_id}", "error": approved_error})
            approved = ([], None)
        if isinstance(bioactive, BaseException):
            # An optional enrichment: its failure is reported, never fatal to the profile.
            logger.warning("ChEMBL target activity lookup for %s failed: %s", target_chembl_id, bioactive)
            lookup_errors.append({"query": f"compounds with measured activity against {target_chembl_id}",
                                  "error": str(bioactive)[:240] or type(bioactive).__name__})
            bioactive = ([], None, None, None)

        mech_rows: List[List[Dict[str, Any]]] = []
        act_rows: List[Tuple[List[Dict[str, Any]], Optional[int]]] = []
        for i, pid in enumerate(parents):
            mech, act = per_drug[i], per_drug[len(parents) + i]
            if isinstance(mech, ChemblError):
                lookup_errors.append({"query": f"mechanisms for {pid}", "error": str(mech)[:240]})
                mech = []
            if isinstance(act, ChemblError):
                lookup_errors.append({"query": f"activities for {pid}", "error": str(act)[:240]})
                act = ([], None)
            mech_rows.append(mech)
            act_rows.append(act)
        mol_by_id = {m.get("molecule_chembl_id"): m for m in molecules}
        target_ids = sorted({str(r["target_chembl_id"]) for rows in mech_rows for r in rows if r.get("target_chembl_id")})
        if target_chembl_id and target_chembl_id not in target_ids:
            target_ids.append(target_chembl_id)
        if early_names:
            if isinstance(names_first, BaseException):
                logger.warning("ChEMBL target names lookup failed: %s", names_first)
            target_names = names_first if isinstance(names_first, dict) else {}
        elif target_chembl_id and not parents:
            target_names = {}  # the resolve step's name is used below
        else:
            try:
                target_names = await self.chembl.targets_by_ids(target_ids)
            except ChemblError as exc:
                logger.warning("ChEMBL target names lookup failed: %s", exc)
                target_names = {}
        if target_chembl_id and target_pref_name and not target_names.get(target_chembl_id):
            target_names[target_chembl_id] = target_pref_name

        drugs: List[DrugRecord] = []
        unresolved: List[str] = []
        for pid, mrows, (arows, atotal) in zip(parents, mech_rows, act_rows):
            mol = mol_by_id.get(pid)
            if mol is None:
                unresolved.append(query_names.get(pid, pid))
                continue
            via = matched_via.get(pid, "chembl_id")
            rec = _drug_record(
                mol,
                query_name=query_names.get(pid, pid),
                matched_via=via if via in ("pref_name", "synonym", "chembl_id") else "chembl_id",
                forms=list(forms.get(pid, [])),
                related=[],
            )
            rec.mention = mentions.get(pid) or None
            rec.mechanisms = _mechanisms(mrows, pid, target_names)
            for m in rec.mechanisms:
                if m.form_chembl_id != pid and m.form_chembl_id not in rec.forms:
                    rec.forms.append(m.form_chembl_id)
            rec.bioactivities = [b for b in (_bioactivity(r) for r in arows) if b is not None]
            rec.bioactivity_total = atotal
            drugs.append(rec)

        target = None
        if target_chembl_id:
            target = TargetRef(
                target_chembl_id=target_chembl_id,
                pref_name=target_names.get(target_chembl_id),
                symbol_query=normalize_target_symbol(target_symbol),
            )
        approved_drugs, approved_total = approved
        bioactive_compounds, bioactive_distinct, activity_total, activity_fetched = bioactive
        out = ChemblProfileOutput(
            chembl_release=release,
            target=target,
            drugs=drugs,
            unresolved_names=unresolved,
            target_approved_drugs=approved_drugs,
            target_approved_total=approved_total,
            target_approved_status=approved_status,
            target_approved_error=approved_error,
            target_approved_error_kind=approved_error_kind,
            target_bioactive_compounds=bioactive_compounds,
            target_bioactive_distinct=bioactive_distinct,
            target_activity_records_total=activity_total,
            target_activity_records_fetched=activity_fetched,
            lookup_errors=lookup_errors,
        )
        data = out.model_dump()
        data["citation_records"] = [
            {
                "type": "chembl",
                "id": d.chembl_id,
                "title": d.pref_name or d.chembl_id,
                "url": COMPOUND_URL.format(id=d.chembl_id),
            }
            for d in drugs
        ] + [
            {"type": "chembl", "id": a.chembl_id, "title": a.pref_name or a.chembl_id, "url": COMPOUND_URL.format(id=a.chembl_id)}
            for a in approved_drugs
        ] + [
            {"type": "chembl", "id": c.chembl_id, "title": c.pref_name or c.chembl_id, "url": COMPOUND_URL.format(id=c.chembl_id)}
            for c in bioactive_compounds
            if c.chembl_id not in {a.chembl_id for a in approved_drugs}
        ] + [
            # Mechanism targets are ChEMBL target ids, so they link to the target page, not a compound page.
            {"type": "chembl", "id": tid, "title": target_names.get(tid) or tid, "url": TARGET_URL.format(id=tid)}
            for tid in target_ids
        ]
        ids = (
            [i for d in drugs for i in (d.chembl_id, *d.forms)]
            + [a.chembl_id for a in approved_drugs]
            + [c.chembl_id for c in bioactive_compounds]
            + target_ids
        )
        return ToolResult(
            success=True,
            tool_name="chembl_drug_profile",
            data=data,
            canonical_ids={"chembl_ids": ",".join(dict.fromkeys(ids))} if ids else {},
            execution_time_seconds=time.perf_counter() - t0,
            metadata={"chembl_release": release, "source": "ChEMBL REST"},
        )

    # -------------------------------------------------------------------------
    # openfda_drug_safety
    # -------------------------------------------------------------------------
    async def _label(self, ingredient: str) -> Tuple[Optional[LabelExcerpt], Optional[str], Optional[Dict[str, Any]]]:
        """(excerpt, search used, meta). Prefers a single-ingredient NDA/BLA label (the generic_name phrase
        search alone often ranks a combination product first: metformin -> sitagliptin and metformin), then
        any NDA/BLA label, then an SPL text match."""
        body, found = None, False
        try:
            single = single_ingredient_name(ingredient, await self.openfda.label_generic_names(label_search(ingredient)))
        except OpenFdaError as exc:
            logger.warning("openFDA label generic-name count failed for %s: %s", ingredient, exc)
            single = None
        if single:
            search = label_exact_search(single)
            body, found = await self.openfda.label(search)
        if not found or not (body or {}).get("results"):
            search = label_search(ingredient)
            body, found = await self.openfda.label(search)
        if not found or not (body or {}).get("results"):
            search = label_fallback_search(ingredient)
            body, found = await self.openfda.label(search)
        if not found or not (body or {}).get("results"):
            return None, search, (body or {}).get("meta") if body else None
        r = body["results"][0]
        ofda = r.get("openfda") or {}

        def first(key: str) -> Optional[str]:
            values = ofda.get(key) or []
            return str(values[0]) if values else None

        def section(key: str) -> Optional[str]:
            values = r.get(key) or []
            return str(values[0]) if values else None

        boxed, boxed_cut = truncate_section(section("boxed_warning"), BOXED_WARNING_MAX)
        warnings_key = "warnings_and_cautions" if r.get("warnings_and_cautions") else ("warnings" if r.get("warnings") else None)
        warnings, warnings_cut = truncate_section(section(warnings_key) if warnings_key else None, WARNINGS_MAX)
        contra, contra_cut = truncate_section(section("contraindications"), CONTRAINDICATIONS_MAX)
        indications, ind_cut = truncate_section(section("indications_and_usage"), INDICATIONS_MAX)
        set_id = str(r.get("set_id") or "")
        generic_name = first("generic_name")
        excerpt = LabelExcerpt(
            set_id=set_id,
            effective_time=r.get("effective_time"),
            generic_name=generic_name,
            combination_product=is_combination(generic_name),
            brand_name=first("brand_name"),
            manufacturer_name=first("manufacturer_name"),
            application_number=first("application_number"),
            harmonized=bool(ofda),
            boxed_warning=boxed,
            boxed_warning_truncated=boxed_cut,
            warnings=warnings,
            warnings_section=warnings_key,  # type: ignore[arg-type]
            warnings_truncated=warnings_cut,
            contraindications=contra,
            contraindications_truncated=contra_cut,
            indications_and_usage=indications,
            indications_truncated=ind_cut,
            dailymed_url=DAILYMED_URL.format(set_id=set_id),
        )
        return excerpt, search, body.get("meta")

    async def _label_with_deadline(self, ingredient: str) -> Tuple[Optional[LabelExcerpt], Optional[str], Optional[Dict[str, Any]]]:
        try:
            return await asyncio.wait_for(self._label(ingredient), timeout=LABEL_DEADLINE_S)
        except asyncio.TimeoutError as exc:
            raise OpenFdaError("timeout", f"openFDA label lookup exceeded {LABEL_DEADLINE_S:g}s") from exc

    async def drug_approval(self, ingredient: str) -> FdaApproval:
        """Drugs@FDA approval status of an ingredient; a failed lookup is status "error", never "not_found"."""
        search = drugsfda_search(ingredient)

        async def fetch() -> Tuple[Optional[Dict[str, Any]], bool]:
            try:
                return await self.openfda.drugsfda(search, DRUGSFDA_LIMIT)
            except OpenFdaError as exc:
                if exc.kind != "timeout":
                    raise  # a 5xx was already retried once by the client
                return await self.openfda.drugsfda(search, DRUGSFDA_LIMIT)  # one retry after a timeout

        try:
            body, found = await asyncio.wait_for(fetch(), timeout=APPROVAL_DEADLINE_S)
            return summarize_approval(ingredient, body, found, search)
        except asyncio.TimeoutError:
            error = f"Drugs@FDA lookup exceeded {APPROVAL_DEADLINE_S:g}s"
        except OpenFdaError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - any other failure still means "status unknown"
            error = f"{type(exc).__name__}: {str(exc)[:160]}"
        logger.warning("Drugs@FDA lookup failed for %s: %s", ingredient, error)
        return FdaApproval(status="error", ingredient=ingredient, search=search, error=error)

    async def drug_safety(
        self, ingredient: str, brand_names: Optional[List[str]] = None, top_n: int = 10, include_label: bool = True
    ) -> ToolResult:
        t0 = time.perf_counter()
        search = faers_search(ingredient, brand_names)
        # FAERS and FDA labels use US adopted names: "mesalazine" has 0 reports, "mesalamine" tens of thousands.
        us_name = us_drug_name(ingredient)
        fetch = max(FAERS_FETCH, top_n + 30)

        async def no_label() -> Tuple[None, None, None]:
            return None, None, None

        counts, total, serious, label, approval = await asyncio.gather(
            self.openfda.reaction_counts(search, fetch),
            self.openfda.total_reports(search),
            self.openfda.serious_split(search),
            self._label_with_deadline(us_name or ingredient) if include_label else no_label(),
            self.drug_approval(us_name or ingredient),
            return_exceptions=True,
        )
        if not isinstance(approval, FdaApproval):  # drug_approval catches its own errors; this is a cancellation
            approval = FdaApproval(status="error", ingredient=us_name or ingredient, error=str(approval)[:200])
        for part in (counts, total):
            if isinstance(part, OpenFdaError):
                return _failure("openfda_drug_safety", str(part), _openfda_error_type(part), t0)
            if isinstance(part, BaseException):
                return _failure("openfda_drug_safety", f"{type(part).__name__}: {part}", ErrorType.INTERNAL_ERROR, t0)

        counts_body, counts_found = counts
        total_body, total_found = total
        tried = [search]
        matched: Optional[Tuple[str, str]] = (ingredient, "activesubstance")
        if not _report_total(total_body, total_found) and us_name:
            # No report under an international name: retry with the US name, on the active substance and then on
            # openFDA's harmonized generic name (a US name, so searching it by the international name cannot match).
            matched = None
            retries = [(us_name, "activesubstance", faers_search(us_name, brand_names)),
                       (us_name, "openfda.generic_name", faers_generic_search(us_name, brand_names))]
            for name, field, alt in retries:
                tried.append(alt)
                try:
                    alt_total = await self.openfda.total_reports(alt)
                    if not _report_total(*alt_total):
                        continue
                    alt_counts, alt_serious = await asyncio.gather(
                        self.openfda.reaction_counts(alt, fetch), self.openfda.serious_split(alt), return_exceptions=True
                    )
                except OpenFdaError as exc:
                    logger.warning("openFDA FAERS retry %s for %s failed: %s", alt, ingredient, exc)
                    break
                if isinstance(alt_counts, BaseException):
                    logger.warning("openFDA FAERS reaction counts for %s failed: %s", alt, alt_counts)
                    break
                search, matched = alt, (name, field)
                total_body, total_found = alt_total
                counts_body, counts_found = alt_counts
                serious = alt_serious
                break
        all_terms = [
            FaersReaction(term=str(r["term"]), count=int(r["count"]))
            for r in ((counts_body or {}).get("results") or [])
            if r.get("term") is not None and r.get("count") is not None
        ]
        excluded = [r for r in all_terms if is_non_reaction_term(r.term)]
        reactions = [r for r in all_terms if not is_non_reaction_term(r.term)][:top_n]
        deaths_reported = next((r.count for r in all_terms if r.term.strip().upper() == "DEATH"), None)
        total_reports = _report_total(total_body, total_found)

        serious_reports: Optional[int] = None
        if isinstance(serious, tuple):
            s_body, s_found = serious
            if s_found:
                serious_reports = next(
                    (int(r["count"]) for r in (s_body or {}).get("results") or [] if str(r.get("term")) == "1"), 0
                )
            elif total_found is False:
                serious_reports = 0
        else:
            logger.warning("openFDA serious split failed for %s: %s", ingredient, serious)

        label_excerpt: Optional[LabelExcerpt] = None
        label_used: Optional[str] = None
        label_meta: Optional[Dict[str, Any]] = None
        label_error: Optional[str] = None
        if isinstance(label, BaseException):
            label_error = str(label)
            logger.warning("openFDA label lookup failed for %s: %s", ingredient, label)
        else:
            label_excerpt, label_used, label_meta = label

        metas = [((counts_body or {}).get("meta")), ((total_body or {}).get("meta")), label_meta]
        disclaimer = next((m.get("disclaimer") for m in metas if isinstance(m, dict) and m.get("disclaimer")), "")
        last_updated = next((m.get("last_updated") for m in metas[:2] if isinstance(m, dict) and m.get("last_updated")), None)

        out = OpenFdaSafetyOutput(
            ingredient=ingredient,
            disclaimer=disclaimer,
            last_updated=last_updated,
            faers_search=search,
            total_reports=total_reports,
            serious_reports=serious_reports,
            top_reactions=reactions,
            excluded_terms=len(excluded),
            excluded_reactions=excluded,
            deaths_reported=deaths_reported,
            label=label_excerpt,
            label_search=label_used,
            caveats=[FAERS_CAVEAT],
            us_name=us_name,
            faers_matched_name=matched[0] if matched and total_reports else None,
            faers_match_field=matched[1] if matched and total_reports else None,  # type: ignore[arg-type]
            faers_searches_tried=tried,
            approval=approval,
        )
        data = out.model_dump()
        data["label_error"] = label_error
        data["citation_records"] = []
        if label_excerpt is not None and label_excerpt.set_id:
            product = label_excerpt.brand_name or ingredient
            app = f" ({label_excerpt.application_number})" if label_excerpt.application_number else ""
            data["citation_records"].append(
                {
                    "type": "dailymed",
                    "id": label_excerpt.set_id,
                    "title": f"{product}{app} prescribing information",
                    "url": label_excerpt.dailymed_url,
                }
            )
        if approval.status in ("approved", "combination_only") and approval.application_number and approval.url:
            brands = "/".join(approval.brand_names[:2]) or ingredient
            data["citation_records"].append(
                {
                    "type": "url",
                    "id": approval.application_number,
                    "title": f"Drugs@FDA: {brands} ({approval.application_number}) approval history",
                    "url": approval.url,
                    "section": "Drugs@FDA (openFDA drugsfda)",
                }
            )
        canonical: Dict[str, str] = {}
        if label_excerpt and label_excerpt.set_id:
            canonical["set_ids"] = label_excerpt.set_id
        apps = [a.application_number for a in approval.applications]
        if apps:
            canonical["application_numbers"] = ",".join(apps)
        return ToolResult(
            success=True,
            tool_name="openfda_drug_safety",
            data=data,
            canonical_ids=canonical,
            execution_time_seconds=time.perf_counter() - t0,
            metadata={"openfda_last_updated": last_updated, "source": "openFDA"},
        )


_ENGINE: Optional[DrugIntelEngine] = None


def get_drug_intel_engine() -> DrugIntelEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = DrugIntelEngine()
    return _ENGINE
