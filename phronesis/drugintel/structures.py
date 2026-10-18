"""
2D chemical structure drawings for ChEMBL molecules, for the UI (GET /api/molecules/{chembl_id}/structure).

get_structure(chembl_id) returns {chembl_id, pref_name, molecule_type, biologic, smiles, inchi_key, formula, mw, svg}
from two ChEMBL REST calls: the molecule record (molecule_structures, molecule_properties, molecule_type) and the
depiction ChEMBL renders with RDKit (/image/{id}.svg). Antibodies, proteins and other biologics have no
small-molecule drawing: svg is None and `biologic` is True.

The SVG is untrusted markup that the browser injects into the page, so sanitize_svg() rebuilds it from an
allowlist: only basic shape, text and grouping elements; only geometry and presentation attributes with plain
values (no url(), no script, no external references, no event handlers); no DTDs or entities; at most
MAX_SVG_BYTES. It is also made theme-friendly: the white background rect is dropped and black (#000000) strokes
and fills become currentColor, so the drawing follows the text colour in dark mode; heteroatom colours (O red,
N blue, S yellow...) stay as ChEMBL draws them.

Results are cached per id for STRUCTURE_TTL_S (a molecule's structure does not change between releases); upstream
failures are not cached.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

import httpx

from ..utils.http import HttpError, get_json, request
from ..utils.http_limits import TTLCache, get_limiter
from .chembl_client import CHEMBL_BASE, CONNECT_TIMEOUT_S, DEFAULT_TIMEOUT_S

logger = logging.getLogger(__name__)

__all__ = [
    "CHEMBL_ID_RE",
    "StructureError",
    "get_structure",
    "sanitize_svg",
    "valid_chembl_id",
    "STRUCTURE_CACHE",
]

CHEMBL_ID_RE = re.compile(r"^CHEMBL\d{1,10}$")
MOLECULE_ONLY = "molecule_chembl_id,pref_name,molecule_type,molecule_structures,molecule_properties"

STRUCTURE_TTL_S = 30 * 24 * 3600
STRUCTURE_CACHE = TTLCache(maxsize=1024, ttl_s=STRUCTURE_TTL_S)
# Polite spacing between structure requests to the EBI API (molecule + image per id).
_LIMITER_NAME = "chembl_structures"
_LIMITER_INTERVAL_S = 0.05

MAX_SVG_INPUT_BYTES = 1_000_000  # refuse to parse anything larger
MAX_SVG_BYTES = 200_000  # sanitised output cap
MAX_SVG_ELEMENTS = 20_000
MAX_ATTR_LEN = 50_000

# Molecule types that are not drawn as small molecules.
_BIOLOGIC_TYPES = {"antibody", "protein", "enzyme", "cell", "gene", "oligonucleotide", "oligosaccharide",
                   "antibody drug conjugate"}


class StructureError(Exception):
    """kind: "invalid" (not a ChEMBL id), "not_found" (no such molecule) or "upstream" (ChEMBL failed)."""

    def __init__(self, message: str, kind: str = "upstream") -> None:
        super().__init__(message)
        self.kind = kind


def valid_chembl_id(value: Any) -> bool:
    return isinstance(value, str) and bool(CHEMBL_ID_RE.match(value))


# ---------------------------------------------------------------------------------------------------------------
# SVG sanitising
# ---------------------------------------------------------------------------------------------------------------

SVG_NS = "http://www.w3.org/2000/svg"
ALLOWED_ELEMENTS = {
    "svg", "g", "path", "line", "polyline", "polygon", "rect", "circle", "ellipse", "text", "tspan", "title", "desc",
    "defs",
}
_TEXT_ELEMENTS = {"text", "tspan", "title", "desc"}

_GEOMETRY_ATTRS = {
    "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "dx", "dy",
}
_PRESENTATION_ATTRS = {
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-linecap",
    "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset", "opacity", "font-family",
    "font-size", "font-weight", "font-style", "text-anchor", "dominant-baseline", "alignment-baseline",
    "clip-rule", "visibility",
}
# Attribute value grammars: path data and point lists are numbers and path commands only.
_PATH_RE = re.compile(r"^[0-9eE.,+\-\sMmLlHhVvCcSsQqTtAaZz]*$")
_POINTS_RE = re.compile(r"^[0-9eE.,+\-\s]*$")
_NUMBER_RE = re.compile(r"^\s*[+\-]?(\d+\.?\d*|\.\d+)([eE][+\-]?\d+)?\s*(px|pt|em|%)?\s*$")
_TRANSFORM_RE = re.compile(r"^(\s*(matrix|translate|scale|rotate|skewX|skewY)\s*\([0-9eE.,+\-\s]*\)\s*,?)*\s*$")
_VIEWBOX_RE = re.compile(r"^[0-9eE.,+\-\s]+$")
_PLAIN_VALUE_RE = re.compile(r"^[#\w\s.,%\-]*$")
_RGB_RE = re.compile(r"^rgba?\(\s*[\d.]+%?\s*,\s*[\d.]+%?\s*,\s*[\d.]+%?\s*(,\s*[\d.]+\s*)?\)$")
_BLACK = {"#000000", "#000", "black", "rgb(0,0,0)"}
_WHITE = {"#ffffff", "#fff", "white", "rgb(255,255,255)"}


def _local(tag: Any) -> Optional[str]:
    """The SVG element name of an ElementTree tag, or None for other namespaces (and comments/PIs)."""
    if not isinstance(tag, str):
        return None
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return name if ns == SVG_NS else None
    return tag


def _themed_colour(value: str) -> str:
    v = value.strip()
    return "currentColor" if re.sub(r"\s+", "", v).lower() in _BLACK else v


def _safe_paint_value(value: str) -> Optional[str]:
    v = value.strip()
    if _PLAIN_VALUE_RE.match(v) or _RGB_RE.match(v):
        return v
    return None


def _clean_style(style: str) -> Optional[str]:
    """Keep only allowlisted presentation properties with plain values; black becomes currentColor."""
    out: List[str] = []
    for decl in style.split(";"):
        prop, sep, value = decl.partition(":")
        if not sep:
            continue
        prop = prop.strip().lower()
        if prop not in _PRESENTATION_ATTRS:
            continue
        safe = _safe_paint_value(value)
        if safe is None:
            continue
        if prop in ("fill", "stroke"):
            safe = _themed_colour(safe)
        out.append(f"{prop}:{safe}")
    return ";".join(out) or None


def _clean_attr(element: str, name: str, value: str) -> Optional[str]:
    if len(value) > MAX_ATTR_LEN:
        return None
    lowered = value.lower()
    if "url(" in lowered or "javascript" in lowered or "expression(" in lowered or "<" in value or ">" in value:
        return None
    if name == "d":
        return re.sub(r"\s+", " ", value).strip() if _PATH_RE.match(value) else None
    if name == "points":
        return re.sub(r"\s+", " ", value).strip() if _POINTS_RE.match(value) else None
    if name in _GEOMETRY_ATTRS:
        return value.strip() if _NUMBER_RE.match(value) else None
    if name == "transform":
        return value.strip() if _TRANSFORM_RE.match(value) else None
    if name == "viewBox":
        return re.sub(r"\s+", " ", value).strip() if element == "svg" and _VIEWBOX_RE.match(value) else None
    if name == "style":
        return _clean_style(value)
    if name in _PRESENTATION_ATTRS:
        safe = _safe_paint_value(value)
        if safe is None:
            return None
        return _themed_colour(safe) if name in ("fill", "stroke") else safe
    return None  # everything else (on*, href, class, id, xml:*, rdkit:*...) is dropped


def _paint(attrs: Dict[str, str], prop: str) -> Optional[str]:
    """The effective fill/stroke of an element from its attribute or style (the style wins)."""
    value = attrs.get(prop)
    for decl in (attrs.get("style") or "").split(";"):
        p, sep, v = decl.partition(":")
        if sep and p.strip().lower() == prop:
            value = v
    return re.sub(r"\s+", "", value).lower() if value else None


def _is_background(element: str, attrs: Dict[str, str]) -> bool:
    """RDKit paints a white full-canvas rect first; any white rect is a background in a structure depiction."""
    return element == "rect" and _paint(attrs, "fill") in _WHITE


def _number(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    m = re.match(r"^\s*([+\-]?\d+\.?\d*)", value)
    return float(m.group(1)) if m else None


class _Budget:
    def __init__(self) -> None:
        self.elements = 0


def _serialise(el: ET.Element, budget: _Budget, parts: List[str], *, root: bool = False) -> None:
    name = _local(el.tag)
    if name is None or name not in ALLOWED_ELEMENTS or (name == "svg" and not root):
        return  # dropped with its whole subtree (script, style, foreignObject, use, image, a, nested svg...)
    budget.elements += 1
    if budget.elements > MAX_SVG_ELEMENTS:
        raise ValueError("too many elements")
    raw = {k: v for k, v in el.attrib.items() if not k.startswith("{")}
    if _is_background(name, raw):
        return
    attrs: List[Tuple[str, str]] = []
    if root:
        attrs.append(("xmlns", SVG_NS))
        view_box = raw.get("viewBox")
        if not view_box:
            w, h = _number(raw.get("width")), _number(raw.get("height"))
            if w and h:
                view_box = f"0 0 {w:g} {h:g}"
        cleaned = _clean_attr("svg", "viewBox", view_box) if view_box else None
        if cleaned:
            attrs.append(("viewBox", cleaned))
        for key in ("fill", "stroke", "style"):
            if key in raw:
                value = _clean_attr("svg", key, raw[key])
                if value:
                    attrs.append((key, value))
    else:
        for key, value in raw.items():
            cleaned = _clean_attr(name, key, value)
            if cleaned is not None:
                attrs.append((key, cleaned))
    parts.append("<" + name + "".join(f" {k}={quoteattr(v)}" for k, v in attrs) + ">")
    if name in _TEXT_ELEMENTS and el.text:
        parts.append(escape(el.text))
    for child in el:
        _serialise(child, budget, parts)
        if name in _TEXT_ELEMENTS and child.tail:
            parts.append(escape(child.tail))
    parts.append(f"</{name}>")


def sanitize_svg(raw: Any, max_bytes: int = MAX_SVG_BYTES) -> Optional[str]:
    """An allowlisted, theme-friendly copy of an SVG document, or None when it is not a safe SVG within max_bytes."""
    if isinstance(raw, bytes):
        data = raw
    elif isinstance(raw, str):
        data = raw.encode("utf-8")
    else:
        return None
    if not data or len(data) > MAX_SVG_INPUT_BYTES:
        return None
    head = data[:4096].lower()
    # No DTDs: they are the only way to declare entities (billion laughs, external entities).
    if b"<!doctype" in data.lower() or b"<!entity" in head:
        return None
    if isinstance(raw, str):
        # ElementTree refuses a str whose XML declaration names an encoding; drop the declaration.
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", raw)
        source: Any = text
    else:
        source = data
    try:
        root = ET.fromstring(source)
    except (ET.ParseError, ValueError):
        return None
    if _local(root.tag) != "svg":
        return None
    parts: List[str] = []
    try:
        _serialise(root, _Budget(), parts, root=True)
    except (ValueError, RecursionError):
        return None
    svg = "".join(parts)
    if not svg.startswith("<svg") or len(svg.encode("utf-8")) > max_bytes:
        return None
    return svg


# ---------------------------------------------------------------------------------------------------------------
# ChEMBL lookups
# ---------------------------------------------------------------------------------------------------------------


def _float(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _is_biologic(molecule_type: Optional[str], smiles: Optional[str]) -> bool:
    t = (molecule_type or "").strip().lower()
    if t in _BIOLOGIC_TYPES:
        return True
    return not smiles and bool(t) and t not in ("small molecule", "unknown", "unclassified")


def _client(transport: Optional[httpx.AsyncBaseTransport], timeout_s: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s, connect=min(CONNECT_TIMEOUT_S, timeout_s)),
        transport=transport,
        headers={"User-Agent": "Phronesis/1.0 (research assistant)"},
        follow_redirects=True,
    )


def _retryable(resp: httpx.Response) -> bool:
    return resp.status_code >= 500 or resp.status_code == 429


async def _fetch_molecule(client: httpx.AsyncClient, base: str, chembl_id: str, limiter: Any) -> Dict[str, Any]:
    try:
        body = await get_json(
            client, f"{base}/molecule/{chembl_id}.json", params={"only": MOLECULE_ONLY},
            headers={"Accept": "application/json"}, retries=1, backoff_s=1.0, retry_on=_retryable,
            respect_retry_after=False, limiter=limiter, accept=(200,),
        )
    except HttpError as exc:
        if exc.status == 404:
            raise StructureError(f"{chembl_id} is not a ChEMBL molecule", kind="not_found") from exc
        raise StructureError(f"ChEMBL molecule lookup failed ({exc.kind})", kind="upstream") from exc
    if not isinstance(body, dict):
        raise StructureError("ChEMBL returned an unexpected molecule record", kind="upstream")
    return body


async def _fetch_svg(client: httpx.AsyncClient, base: str, chembl_id: str, limiter: Any) -> Tuple[Optional[bytes], bool]:
    """(svg bytes or None, failed): a 404 is "no drawing" (biologics); other failures are reported as failed."""
    try:
        resp = await request(
            client, "GET", f"{base}/image/{chembl_id}.svg", headers={"Accept": "image/svg+xml"},
            retries=1, backoff_s=1.0, retry_on=_retryable, respect_retry_after=False, limiter=limiter,
            accept=(200, 404),
        )
    except HttpError as exc:
        logger.info("ChEMBL image for %s failed: %s", chembl_id, exc.message)
        return None, True
    if resp.status_code == 404 or not resp.content:
        return None, False
    return resp.content, False


async def get_structure(
    chembl_id: str,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    cache: Optional[TTLCache] = None,
    base_url: str = CHEMBL_BASE,
    timeout_s: Optional[float] = None,
) -> Dict[str, Any]:
    """The structure record for one ChEMBL molecule id (see the module docstring). Raises StructureError."""
    chembl_id = (chembl_id or "").strip().upper()
    if not valid_chembl_id(chembl_id):
        raise StructureError("expected a ChEMBL molecule id such as CHEMBL521686", kind="invalid")
    # An injected transport is a test fixture: it gets its own cache unless one is passed.
    store = cache if cache is not None else (STRUCTURE_CACHE if transport is None else None)
    base = base_url.rstrip("/")
    key = (base, chembl_id)
    if store is not None:
        hit = store.get(key)
        if hit is not None:
            return dict(hit)

    timeout = timeout_s or float(os.getenv("PHRONESIS_DRUGINTEL_TIMEOUT", str(DEFAULT_TIMEOUT_S)))
    limiter = get_limiter(_LIMITER_NAME, _LIMITER_INTERVAL_S)
    async with _client(transport, timeout) as client:
        molecule, (svg_raw, svg_failed) = await asyncio.gather(
            _fetch_molecule(client, base, chembl_id, limiter), _fetch_svg(client, base, chembl_id, limiter)
        )

    structures = molecule.get("molecule_structures") or {}
    props = molecule.get("molecule_properties") or {}
    smiles = structures.get("canonical_smiles") or None
    molecule_type = molecule.get("molecule_type") or None
    biologic = _is_biologic(molecule_type, smiles)
    svg = None
    note = None
    if not biologic and svg_raw is not None:
        svg = sanitize_svg(svg_raw)
        if svg is None:
            note = "ChEMBL's drawing could not be used safely"
    elif not biologic and svg_failed:
        note = "ChEMBL's drawing could not be retrieved"
    record: Dict[str, Any] = {
        "chembl_id": molecule.get("molecule_chembl_id") or chembl_id,
        "pref_name": molecule.get("pref_name") or None,
        "molecule_type": molecule_type,
        "biologic": biologic,
        "smiles": smiles,
        "inchi_key": structures.get("standard_inchi_key") or None,
        "formula": props.get("full_molformula") or None,
        "mw": _float(props.get("full_mwt")),
        "svg": svg,
        "source": "ChEMBL",
    }
    if note:
        record["note"] = note
    # A failed image fetch is retried on the next request; everything else is stable and cached.
    if store is not None and not svg_failed:
        store.set(key, dict(record))
    return record

