"""
Drugs@FDA (openFDA drug/drugsfda.json) approval status of one active ingredient.

The search matches the ingredient in the products of NDA and BLA applications (ANDA generics are left out):
    products.active_ingredients.name:"OLAPARIB" AND application_number:(NDA* OR BLA*)
openfda.generic_name is not used because Drugs@FDA harmonizes only some records: the discontinued original
olaparib capsule NDA206162 (approved 2014-12-19) has no openfda block, and osimertinib has none at all.

An application counts for the ingredient when one of its products has the ingredient as its only active
ingredient (the name alone, with a salt word such as HYDROCHLORIDE or MESYLATE, or with an FDA biologic suffix such
as -NXKI). Applications that list it only inside combination products are kept apart. Drugs@FDA covers drugs and
therapeutic biologics regulated by CDER; CBER products (vaccines, blood products, cell and gene therapies) are not
listed, so "not found" there is not proof that a product has no FDA licence of any kind.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .openfda_client import lucene_term
from .schemas import FdaApplication, FdaApproval, FdaSupplement

DRUGSFDA_URL = "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={number}"
MAX_APPLICATIONS = 8
MAX_SUPPLEMENTS = 25
DRUGSFDA_LIMIT = 100
COVERAGE_NOTE = (
    "Drugs@FDA lists drugs and therapeutic biologics regulated by CDER; CBER products (vaccines, blood products, "
    "cell and gene therapies) are not in it."
)

_SALT_WORD_RE = re.compile(
    r"^(?:\w+ATE|\w+IDE|\w*HYDRATE|ANHYDROUS|SODIUM|POTASSIUM|CALCIUM|MAGNESIUM|ZINC|LITHIUM|ALUMINUM|LYSINE|"
    r"ARGININE|MEGLUMINE|TROMETHAMINE|DIETHYLAMINE|OLAMINE|HCL|MONO|DI|TRI|HEMI|SESQUI|BASE|FREE|ACID)$"
)
_BIOLOGIC_SUFFIX_RE = re.compile(r"-[A-Z]{4}$")
_ADC_PREFIX_RE = re.compile(r"^(?:ADO|FAM)-")
_COMBINATION_NAME_RE = re.compile(r"\bAND\b|[,/+;]")
_MARKETED = ("PRESCRIPTION", "OVER-THE-COUNTER", "OVER THE COUNTER", "OTC")


def drugsfda_search(ingredient: str) -> str:
    return f'products.active_ingredients.name:"{lucene_term(ingredient).upper()}" AND application_number:(NDA* OR BLA*)'


def _iso(date: Any) -> Optional[str]:
    raw = re.sub(r"\D", "", str(date or ""))
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 else None


def _core_name(name: str) -> str:
    """'FAM-TRASTUZUMAB DERUXTECAN-NXKI' -> 'TRASTUZUMAB DERUXTECAN'; upper case, single spaces."""
    n = re.sub(r"\s+", " ", str(name or "").upper()).strip()
    n = _ADC_PREFIX_RE.sub("", n)
    return _BIOLOGIC_SUFFIX_RE.sub("", n)


def ingredient_matches(name: str, ingredient: str) -> bool:
    """True when an active-ingredient name is the ingredient itself (bare, as a salt, or with a biologic suffix)."""
    inn = re.sub(r"\s+", " ", lucene_term(ingredient).upper()).strip()
    core = _core_name(name)
    if not inn or _COMBINATION_NAME_RE.search(core):
        return False
    if core == inn:
        return True
    if not core.startswith(inn + " "):
        return False
    return all(_SALT_WORD_RE.match(w) for w in core[len(inn) + 1:].split())


def _product_kind(product: Dict[str, Any], ingredient: str) -> Optional[str]:
    """'single' (the ingredient alone), 'combination' (with other ingredients) or None (does not contain it)."""
    actives = [a for a in product.get("active_ingredients") or [] if isinstance(a, dict) and a.get("name")]
    names = [str(a["name"]) for a in actives]
    if len(names) == 1 and ingredient_matches(names[0], ingredient):
        return "single"
    inn = lucene_term(ingredient).upper()
    if inn and any(re.search(rf"(?<![A-Z0-9]){re.escape(inn)}(?![A-Z0-9])", _core_name(n)) for n in names):
        return "combination"
    return None


def _original_approval(submissions: List[Dict[str, Any]]) -> Optional[str]:
    dates = [
        _iso(s.get("submission_status_date"))
        for s in submissions
        if str(s.get("submission_type") or "").upper() == "ORIG" and str(s.get("submission_status") or "").upper() == "AP"
    ]
    dates = [d for d in dates if d]
    return min(dates) if dates else None


def summarize_approval(ingredient: str, body: Optional[Dict[str, Any]], found: bool, search: str) -> FdaApproval:
    """FdaApproval from a drugsfda response. A body whose records carry no application number is a malformed
    response and raises ValueError (reported as a failed lookup, never as 'not found')."""
    records = [r for r in ((body or {}).get("results") or []) if isinstance(r, dict)] if found else []
    if found and records and not any(r.get("application_number") for r in records):
        raise ValueError("Drugs@FDA returned records without application numbers")
    single: List[Tuple[FdaApplication, List[Dict[str, Any]]]] = []
    combination: List[FdaApplication] = []
    unapproved: List[str] = []
    for r in records:
        number = re.sub(r"[\s-]", "", str(r.get("application_number") or "")).upper()
        if not re.fullmatch(r"(?:NDA|BLA)\d{6}", number):
            continue
        products = [p for p in r.get("products") or [] if isinstance(p, dict)]
        kinds = [_product_kind(p, ingredient) for p in products]
        if not any(kinds):
            continue
        subs = [s for s in r.get("submissions") or [] if isinstance(s, dict)]
        approved_on = _original_approval(subs)
        if approved_on is None:
            unapproved.append(number)
            continue
        own = [p for p, k in zip(products, kinds) if k == "single"] or [p for p, k in zip(products, kinds) if k]
        statuses = list(dict.fromkeys(str(p.get("marketing_status")) for p in own if p.get("marketing_status")))
        app = FdaApplication(
            application_number=number,
            sponsor=r.get("sponsor_name"),
            brand_names=list(dict.fromkeys(str(p["brand_name"]).upper() for p in own if p.get("brand_name")))[:4],
            marketing_status=statuses,
            original_approval_date=approved_on,
            combination="single" not in kinds,
            marketed=any(s.upper() in _MARKETED for s in statuses),
        )
        if app.combination:
            combination.append(app)
        else:
            single.append((app, subs))
    base = {"ingredient": ingredient, "search": search, "unapproved_applications": unapproved}
    if not single and not combination:
        return FdaApproval(status="not_found", **base)
    if not single:
        combination.sort(key=lambda a: a.original_approval_date or "9999")
        first = combination[0]
        return FdaApproval(
            status="combination_only",
            application_number=first.application_number,
            first_approval_date=first.original_approval_date,
            first_approval_application=first.application_number,
            applications=combination[:MAX_APPLICATIONS],
            applications_total=len(combination),
            url=DRUGSFDA_URL.format(number=re.sub(r"\D", "", first.application_number)),
            **base,
        )
    single.sort(key=lambda pair: pair[0].original_approval_date or "9999")
    first = single[0][0]
    # The originator's applications: the first approval's sponsor (biosimilars and 505(b)(2) reformulations by other
    # sponsors are listed as applications but their supplements are not counted as new indications).
    originator = [pair for pair in single if (pair[0].sponsor or "") == (first.sponsor or "")]
    current = next((a for a, _ in originator if a.marketed), None) or next((a for a, _ in single if a.marketed), None) or first
    supplements: List[FdaSupplement] = []
    for app, subs in originator:
        for s in subs:
            if (
                str(s.get("submission_type") or "").upper() == "SUPPL"
                and str(s.get("submission_status") or "").upper() == "AP"
                and str(s.get("submission_class_code") or "").upper() == "EFFICACY"
            ):
                supplements.append(FdaSupplement(
                    application_number=app.application_number,
                    submission_number=str(s.get("submission_number")) if s.get("submission_number") is not None else None,
                    date=_iso(s.get("submission_status_date")),
                    review_priority=s.get("review_priority"),
                ))
    supplements.sort(key=lambda x: (x.date or "", x.application_number, int(x.submission_number or 0) if str(x.submission_number or "").isdigit() else 0))
    apps = [a for a, _ in single]
    ordered = [current, *[a for a in apps if a is not current]]
    return FdaApproval(
        status="approved",
        application_number=current.application_number,
        sponsor=current.sponsor,
        brand_names=current.brand_names,
        marketed=current.marketed,
        first_approval_date=first.original_approval_date,
        first_approval_application=first.application_number,
        applications=ordered[:MAX_APPLICATIONS],
        applications_total=len(apps),
        efficacy_supplements=supplements[-MAX_SUPPLEMENTS:],
        efficacy_supplements_total=len(supplements),
        url=DRUGSFDA_URL.format(number=re.sub(r"\D", "", current.application_number)),
        **base,
    )


def approval_phrase(approval: Optional[Dict[str, Any]]) -> str:
    """'FDA-approved (NDA208558, first approved 2014-12-19 under NDA206162)', a not-found or failure statement."""
    if not approval:
        return ""
    status = approval.get("status")
    name = approval.get("ingredient") or "the drug"
    if status == "approved":
        app = approval.get("application_number")
        first, first_app = approval.get("first_approval_date"), approval.get("first_approval_application")
        when = f", first approved {first}" if first else ""
        if first and first_app and first_app != app:
            when += f" under {first_app}"
        return f"FDA-approved ({app}{when})"
    if status == "combination_only":
        return (f"in Drugs@FDA only as part of combination products (e.g. {approval.get('application_number')}, "
                f"approved {approval.get('first_approval_date') or 'date unknown'}); no single-ingredient NDA/BLA")
    if status == "not_found":
        return f"not found in Drugs@FDA (no approved NDA/BLA lists {name})"
    return "Drugs@FDA lookup failed (approval status unknown)"


__all__ = [
    "COVERAGE_NOTE",
    "DRUGSFDA_LIMIT",
    "DRUGSFDA_URL",
    "approval_phrase",
    "drugsfda_search",
    "ingredient_matches",
    "summarize_approval",
]
