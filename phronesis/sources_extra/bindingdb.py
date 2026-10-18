"""
BindingDB measured binding affinities (Ki, Kd, IC50, EC50) for one protein target, optionally matched to named drugs.

Reference: Liu T. et al., "BindingDB in 2024: a FAIR knowledgebase of protein-small molecule binding data",
Nucleic Acids Res. 2025;53(D1):D1633-D1644, doi:10.1093/nar/gkae1075.

API (no key): `GET https://bindingdb.org/rest/getLigandsByUniprots?uniprot=P00533&cutoff=10&response=application/json`
returns {"getLindsByUniprotsResponse": {"affinities": [{"query": "<target name>", "monomerid", "smile",
"affinity_type": "IC50|Ki|Kd|EC50", "affinity": "0.006" | "<1" | ">0.53", "pmid": "" | "18077363", "doi"}]}}.
Values are in nM and `cutoff` keeps records at or below it (nM). An unknown accession is 200 with an empty list.
The service is slow (10-30 s for EGFR, 3-7 MB), so the read timeout is generous and responses are cached in memory.

The response carries no ligand names, only BindingDB monomer ids and SMILES. A named drug is matched by structure:
its PubChem SMILES (isomeric when available) and each BindingDB SMILES are reduced to the largest heavy-atom fragment
(so salts match) and compared by a Weisfeiler-Lehman hash of atoms labelled by element, isotope, formal charge,
degree and hydrogen count (the hydrogen count carries saturation and bond order, and is the same for aromatic and
Kekule SMILES). Stereochemistry is compared separately from canonical R/S and E/Z descriptors: a record whose
specified stereochemistry differs from the drug's is not attributed to it, and every attributed row states its
match_basis (e.g. stereochemistry specified in neither structure). The ligand label stays the BindingDB monomer id.
When no record matches, the top-affinity ligands are reported and the drug is listed as unmatched (not as non-binding).

The records are not annotated with a mutation (the "query" field is the target's protein name), so they cannot be
read as mutant-specific affinities (e.g. EGFR T790M or C797S); the source says so when the question names a variant.

Verifier: every record's value is the upstream affinity string in positional notation, never rounded or in
e-notation (value, and value_text "<value> nM"), so the drug pharmacology check (phronesis/drugintel/checks.py)
reads the value BindingDB reported. PMIDs and DOIs are returned through canonical_ids and allowed_identifiers so
citing them passes the identifier check.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from pydantic import BaseModel, Field, field_validator

from ..agents.query_context import QueryContext
from . import common
from phronesis.utils.http_limits import TTLCache

TOOL_NAME = "bindingdb_affinities"
SOURCE_KEY = "bindingdb"
BASE_URL = "https://bindingdb.org/rest/getLigandsByUniprots"
UNIPROT_SEARCH_URL = "https://rest.uniprot.org/uniprotkb/search"
MONOMER_URL = "https://www.bindingdb.org/rwd/bind/chemsearch/marvin/MolStructure.jsp?monomerid={}"
READ_TIMEOUT_S = 45.0
DEFAULT_CUTOFF_NM = 10.0  # top-affinity ligands when no drug is named
DRUG_CUTOFF_NM = 1000.0  # wider window when the question names drugs, so their records are in the response
MAX_ROWS = 20
PER_DRUG_ROWS = 6
_CACHE_TTL_S = 6 * 3600.0
EMPTY_RETRY_DELAY_S = 3.0
_CACHE_MAX = 4

INTENT_RE = re.compile(
    r"\b(binding\s+affinit(?:y|ies)|affinit(?:y|ies)|potenc(?:y|ies)|potent(?:ly)?|ic50s?|ic₅₀|ec50s?|"
    r"k[id]\s+values?|binding\s+(?:constants?|data|predictions?|strength|measurements?)|"
    r"dissociation\s+constants?|inhibition\s+constants?|nanomolar|bindingdb)\b",
    re.I,
)
INTENT_CASE_RE = re.compile(r"\b(?:Ki|Kd|pKi|pKd|pIC50|K_i|K_d)\b")
NAME_RE = re.compile(r"\bbindingdb\b", re.I)
_PROTEIN_CHANGE_RE = re.compile(r"\b([ACDEFGHIKLMNPQRSTVWY])(\d{1,4})([ACDEFGHIKLMNPQRSTVWY])\b")
_VALUE_RE = re.compile(r"^\s*([<>~=]{0,2})\s*(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*$")


class BindingdbInput(BaseModel):
    gene: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9\-]{0,19}$", description="HGNC symbol of the target")
    uniprot: Optional[str] = Field(default=None, description="UniProt accession; resolved from the symbol when absent")
    drugs: List[str] = Field(default_factory=list, max_length=3, description="drug names to match by structure")
    cutoff_nm: Optional[float] = Field(default=None, gt=0, le=100000, description="keep records at or below (nM)")
    variants: List[str] = Field(default_factory=list, max_length=4, description="protein changes named in the question")

    @field_validator("uniprot")
    @classmethod
    def _acc(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip().upper()
        if not common._UNIPROT_RE.match(value):
            raise ValueError("not a UniProt accession")
        return value

    @field_validator("drugs")
    @classmethod
    def _drugs(cls, names: List[str]) -> List[str]:
        out = []
        for n in names:
            n = n.strip()
            if not (1 <= len(n) <= 100) or re.search(r"[/\\?#]", n):
                raise ValueError(f"unsupported drug name: {n[:40]!r}")
            out.append(n)
        return out


# -----------------------------------------------------------------------------
# Structure matching: a constitution hash of a SMILES string, plus a stereo comparison (no RDKit dependency)
# -----------------------------------------------------------------------------
# Each heavy atom is labelled by element, isotope, formal charge, heavy-atom degree, hydrogen count and the isotopes
# of its explicit hydrogens. The hydrogen count comes from the SMILES valence model, so it encodes saturation and
# bond order (benzene CH vs cyclohexane CH2, alkyne CH vs ethyl CH3) and is the same for the aromatic and the Kekule
# form of one molecule. Stereochemistry (R/S, E/Z) is not part of the key; it is compared separately
# (stereo_relation) and a record whose specified stereochemistry differs from the drug's is never attributed to it.

_ATOM_RE = re.compile(r"\[[^\]]+\]|Br|Cl|[BCNOPSFI]|[bcnops]|\*")
_BRACKET_RE = re.compile(
    r"^\[(?P<iso>\d+)?(?P<el>se|as|[bcnops]|[A-Z][a-z]?|\*)"
    r"(?P<chi>@@|@(?:TH[12]|AL[12]|SP[123]|TB\d{1,2}|OH\d{1,2})?)?"
    r"(?P<h>H\d?)?(?P<chg>[+-]{1,2}\d?)?(?::\d+)?\]$"
)
_VALENCES = {"B": (3,), "C": (4,), "N": (3, 5), "O": (2,), "P": (3, 5), "S": (2, 4, 6), "F": (1,), "Cl": (1,),
             "Br": (1,), "I": (1,), "*": (0,)}
_BOND_ORDER = {"-": 1, "/": 1, "\\": 1, "=": 2, "#": 3, "$": 4, ":": 1}
_WL_MAX_ROUNDS = 8


class _Mol:
    """A parsed SMILES: atoms with their bracket properties, bonds with their symbol in writing order, and for every
    atom its neighbours in SMILES order (the order a chirality mark refers to)."""

    def __init__(self) -> None:
        self.el: List[str] = []
        self.aromatic: List[bool] = []
        self.bracket: List[bool] = []
        self.iso: List[int] = []
        self.charge: List[int] = []
        self.hcount: List[int] = []  # hydrogens written inside a bracket atom ([C@H] -> 1)
        self.chiral: List[Optional[str]] = []
        self.has_prev: List[bool] = []
        self.order: List[List[Optional[int]]] = []
        self.bonds: Dict[Tuple[int, int], str] = {}  # (written first, written second) -> bond symbol ('' = implicit)
        self.ring_bonds: Set[Tuple[int, int]] = set()

    def bond(self, a: int, b: int) -> str:
        return self.bonds.get((a, b), self.bonds.get((b, a), ""))


def _parse_smiles(smiles: str) -> _Mol:
    """Raises ValueError on input that is not a readable SMILES string."""
    mol = _Mol()
    rings: Dict[str, Tuple[int, str, int]] = {}
    stack: List[int] = []
    prev: Optional[int] = None
    pending = ""
    i, s = 0, smiles.strip()
    while i < len(s):
        ch = s[i]
        if ch == "(":
            if prev is None:
                raise ValueError("branch before atom")
            stack.append(prev)
            i += 1
        elif ch == ")":
            if not stack:
                raise ValueError("unbalanced branch")
            prev = stack.pop()
            i += 1
        elif ch in "-=#$:/\\":
            pending = ch
            i += 1
        elif ch == ".":
            prev, pending = None, ""
            i += 1
        elif ch.isdigit() or ch == "%":
            label = s[i + 1:i + 3] if ch == "%" else ch
            i += 3 if ch == "%" else 1
            if prev is None:
                raise ValueError("ring bond before atom")
            if label in rings:
                other, other_bond, slot = rings.pop(label)
                if other == prev:
                    raise ValueError("ring bond to itself")
                mol.bonds[(other, prev)] = pending or other_bond
                mol.ring_bonds.add((other, prev))
                mol.order[other][slot] = prev
                mol.order[prev].append(other)
            else:
                rings[label] = (prev, pending, len(mol.order[prev]))
                mol.order[prev].append(None)
            pending = ""
        else:
            m = _ATOM_RE.match(s, i)
            if not m:
                raise ValueError(f"unexpected {ch!r}")
            tok = m.group(0)
            if tok.startswith("["):
                bm = _BRACKET_RE.match(tok)
                if not bm:
                    raise ValueError("bad bracket atom")
                el = bm.group("el")
                chg_txt = bm.group("chg") or ""
                charge = 0
                if chg_txt:
                    digits = chg_txt.lstrip("+-")
                    charge = (-1 if chg_txt[0] == "-" else 1) * (int(digits) if digits else len(chg_txt))
                h_txt = bm.group("h") or ""
                chi = bm.group("chi")
                mol.el.append("*" if el == "*" else el.capitalize())
                mol.aromatic.append(el.islower())
                mol.bracket.append(True)
                mol.iso.append(int(bm.group("iso")) if bm.group("iso") else 0)
                mol.charge.append(charge)
                mol.hcount.append((int(h_txt[1:]) if len(h_txt) > 1 else 1) if h_txt else 0)
                mol.chiral.append(chi if chi in (None, "@", "@@") else "?")
            else:
                mol.el.append("*" if tok == "*" else tok.capitalize())
                mol.aromatic.append(tok.islower())
                mol.bracket.append(False)
                mol.iso.append(0)
                mol.charge.append(0)
                mol.hcount.append(0)
                mol.chiral.append(None)
            idx = len(mol.el) - 1
            mol.order.append([])
            mol.has_prev.append(prev is not None)
            if prev is not None:
                mol.bonds[(prev, idx)] = pending
                mol.order[idx].append(prev)
                mol.order[prev].append(idx)
            prev, pending = idx, ""
            i = m.end()
    if rings or stack or not mol.el:
        raise ValueError("incomplete SMILES")
    return mol


def _implicit_h(mol: _Mol, k: int) -> int:
    """Implicit hydrogens of an organic-subset atom (OpenSMILES valence model; aromatic bonds count 1, plus 1 for the
    aromatic system), so the aromatic and the Kekule form of a molecule give the same count."""
    if mol.bracket[k]:
        return 0
    used = 0
    for n in mol.order[k]:
        if n is None:
            continue
        sym = mol.bond(k, n)
        if not sym and mol.aromatic[k] and mol.aromatic[n]:
            sym = ":"
        used += _BOND_ORDER.get(sym, 1)
    valences = _VALENCES.get(mol.el[k], (0,))
    if mol.aromatic[k]:
        return max(0, valences[0] - used - 1)
    for v in valences:
        if v >= used:
            return v - used
    return 0


def _permutation_parity(perm: List[int]) -> bool:
    """True for an odd permutation."""
    p = list(perm)
    odd = False
    for i in range(len(p)):
        while p[i] != i:
            j = p[i]
            p[i], p[j] = p[j], p[i]
            odd = not odd
    return odd


class _Structure:
    """The largest heavy-atom fragment of a parsed SMILES, its atoms coloured by a stable Weisfeiler-Lehman refinement."""

    def __init__(self, mol: _Mol) -> None:
        self.mol = mol
        n = len(mol.el)
        is_h = [mol.el[k] == "H" for k in range(n)]
        adj = [{x for x in mol.order[k] if x is not None} for k in range(n)]
        seen: Set[int] = set()
        best: List[int] = []
        for start in (k for k in range(n) if not is_h[k]):
            if start in seen:
                continue
            comp, todo = [], [start]
            seen.add(start)
            while todo:
                a = todo.pop()
                comp.append(a)
                for b in adj[a]:
                    if not is_h[b] and b not in seen:
                        seen.add(b)
                        todo.append(b)
            if len(comp) > len(best):
                best = comp
        if not best:
            raise ValueError("no heavy atom")
        self.atoms = sorted(best)
        keep = set(self.atoms)
        labels: Dict[int, str] = {}
        self.h_labels: Dict[int, str] = {}
        for k in self.atoms:
            h_atoms = [b for b in adj[k] if is_h[b]]
            for b in h_atoms:
                self.h_labels[b] = f"H{mol.iso[b] or ''}"
            h_total = mol.hcount[k] + _implicit_h(mol, k) + len(h_atoms)
            h_iso = ",".join(sorted(str(mol.iso[b]) for b in h_atoms if mol.iso[b]))
            degree = sum(1 for b in adj[k] if b in keep)
            labels[k] = f"{mol.iso[k] or ''}{mol.el[k]}|d{degree}|h{h_total}|q{mol.charge[k]}|i{h_iso}"
        self.graph = {k: [b for b in adj[k] if b in keep] for k in self.atoms}
        distinct = len(set(labels.values()))
        for _ in range(_WL_MAX_ROUNDS):
            labels = {
                k: hashlib.blake2b((labels[k] + "|" + ",".join(sorted(labels[b] for b in self.graph[k]))).encode(),
                                   digest_size=8).hexdigest()
                for k in self.atoms
            }
            count = len(set(labels.values()))
            if count == distinct:
                break
            distinct = count
        self.labels = labels

    def key(self) -> str:
        return f"{len(self.atoms)}:" + hashlib.sha1(",".join(sorted(self.labels.values())).encode()).hexdigest()

    def _label(self, atom: Optional[int]) -> str:
        if atom is None:
            return "H"  # the hydrogen written inside a bracket atom such as [C@H]
        return self.labels.get(atom) or self.h_labels.get(atom, "?")

    def _side(self, end: int, x: int) -> Optional[int]:
        """+1 when substituent x is 'up' from double-bond atom `end`, -1 when 'down' (SMILES '/' and '\\')."""
        mol = self.mol
        if (end, x) in mol.ring_bonds or (x, end) in mol.ring_bonds:
            return None  # directional ring-closure bonds are not interpreted
        if (x, end) in mol.bonds:  # written "x/end"
            return -1 if mol.bonds[(x, end)] == "/" else 1
        return 1 if mol.bonds[(end, x)] == "/" else -1

    def stereo(self) -> Tuple[bool, Optional[Tuple[str, ...]]]:
        """(stereochemistry specified, canonical tetrahedral and double-bond descriptors; None when a specified centre
        cannot be resolved). A descriptor does not depend on the order the SMILES was written in."""
        mol = self.mol
        specified, resolved = False, True
        out: List[str] = []
        for k in self.atoms:
            chi = mol.chiral[k]
            if chi is None:
                continue
            specified = True
            nbrs: List[Optional[int]] = [x for x in mol.order[k] if x is not None]
            if mol.hcount[k] == 1:
                nbrs.insert(1 if mol.has_prev[k] else 0, None)
            names = [self._label(x) for x in nbrs]
            if chi not in ("@", "@@") or len(nbrs) != 4 or len(set(names)) != 4:
                resolved = False
                continue
            parity = _permutation_parity(sorted(range(4), key=lambda j: names[j]))
            out.append(f"T:{self.labels[k]}:{int((chi == '@@') ^ parity)}")
        for (a, b), sym in mol.bonds.items():
            if sym != "=" or a not in self.labels or b not in self.labels:
                continue
            marks = []
            for end, other in ((a, b), (b, a)):
                marks.append([x for x in mol.order[end]
                              if x is not None and x != other and mol.bond(end, x) in ("/", "\\")])
            if not marks[0] or not marks[1]:
                continue  # a directional bond on one side only specifies nothing (e.g. next to a Kekule ring bond)
            specified = True
            value, ok = 0, True
            for (end, other), marked in zip(((a, b), (b, a)), marks):
                if not ok:
                    break
                subs = [x for x in mol.order[end] if x is not None and x != other]
                sub_names = [self._label(x) for x in subs]
                side = self._side(end, marked[0])
                if side is None or (len(subs) == 2 and sub_names[0] == sub_names[1]):
                    ok = False
                    break
                value += 0 if side > 0 else 1
                if marked[0] != max(subs, key=self._label):
                    value += 1  # refer the configuration to the higher-ranked substituent at this end
            if not ok:
                resolved = False
                continue
            ends = sorted((self.labels[a], self.labels[b]))
            out.append(f"D:{ends[0]}:{ends[1]}:{value % 2}")
        return specified, (tuple(sorted(out)) if resolved else None)


def _structure(smiles: str) -> Optional[_Structure]:
    parts = (smiles or "").split()  # drop a ChemAxon extension such as " |r|"
    try:
        return _Structure(_parse_smiles(parts[0] if parts else ""))
    except (ValueError, IndexError, KeyError):
        return None


def skeleton_key(smiles: str) -> Optional[str]:
    """Constitution hash of the largest fragment (element, isotope, charge, degree and hydrogen count per atom; the
    aromatic and Kekule forms agree; salts and solvents are dropped); None when the SMILES cannot be read.
    Stereochemistry is not part of the key (see stereo_relation)."""
    st = _structure(smiles)
    return st.key() if st is not None else None


def stereo_signature(smiles: str) -> Tuple[bool, Optional[Tuple[str, ...]]]:
    """(stereochemistry specified, canonical R/S and E/Z descriptors, or None when they could not be resolved)."""
    st = _structure(smiles)
    return st.stereo() if st is not None else (False, None)


def stereo_relation(drug_smiles: str, row_smiles: str) -> str:
    """How two structures of the same constitution compare in stereochemistry: 'identical' (both specify it, equal),
    'different' (both specify it, not equal), 'unspecified' (neither specifies it), 'drug_only' / 'record_only' (only
    one specifies it) or 'not_compared' (both specify it but a centre could not be resolved)."""
    d_spec, d_sig = stereo_signature(drug_smiles)
    r_spec, r_sig = stereo_signature(row_smiles)
    if not d_spec and not r_spec:
        return "unspecified"
    if d_spec and not r_spec:
        return "drug_only"
    if r_spec and not d_spec:
        return "record_only"
    if d_sig is None or r_sig is None:
        return "not_compared"
    return "identical" if d_sig == r_sig else "different"


# Stereo relations under which a BindingDB record is attributed to the named drug, and how that match is described.
MATCH_BASIS = {
    "identical": "same structure, including the specified stereochemistry",
    "unspecified": "same constitution; stereochemistry specified in neither structure",
    "drug_only": "same constitution; the BindingDB structure leaves the drug's stereochemistry unspecified "
                 "(racemate or unassigned), so stereochemistry was not compared",
    "record_only": "same constitution; the BindingDB structure specifies stereochemistry that the drug's PubChem "
                   "structure does not, so this may be a single stereoisomer of the drug; not compared",
}


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------

_CACHE = TTLCache(maxsize=_CACHE_MAX, ttl_s=_CACHE_TTL_S)  # (accession, cutoff) -> affinity rows
_KEY_CACHE = TTLCache(maxsize=100_000, ttl_s=24 * 3600.0)  # SMILES -> (skeleton key or None,)


def _bdb_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(READ_TIMEOUT_S, connect=10.0),
        headers={"User-Agent": common.USER_AGENT, "Accept": "application/json"},
        transport=common.TRANSPORT,
        follow_redirects=True,
    )


async def resolve_uniprot(http: httpx.AsyncClient, symbol: str) -> Optional[str]:
    """Reviewed human UniProt accession for an exact gene symbol (UniProt REST), e.g. EGFR -> P00533."""
    doc = await common.get_json(
        http, UNIPROT_SEARCH_URL, source="UniProt",
        params={"query": f"gene_exact:{symbol} AND organism_id:9606 AND reviewed:true", "fields": "accession",
                "format": "json", "size": "1"},
    )
    results = (doc or {}).get("results") or []
    acc = str((results[0] or {}).get("primaryAccession") or "").upper() if results else ""
    return acc if acc and common._UNIPROT_RE.match(acc) else None


async def fetch_affinities(http: httpx.AsyncClient, accession: str, cutoff_nm: float) -> List[Dict[str, Any]]:
    key = (accession, float(cutoff_nm))
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    cutoff = int(cutoff_nm) if float(cutoff_nm).is_integer() else cutoff_nm
    params = {"uniprot": accession, "cutoff": str(cutoff), "response": "application/json"}
    try:
        doc = await common.get_json(http, BASE_URL, source="BindingDB", params=params)
    except common.UpstreamError as exc:
        # The service intermittently answers 200 with an empty body (seen live 2026-10-16); retry that once.
        if exc.status != 200:
            raise
        await asyncio.sleep(EMPTY_RETRY_DELAY_S)
        doc = await common.get_json(http, BASE_URL, source="BindingDB", params=params)
    if not isinstance(doc, dict):
        raise common.UpstreamError("BindingDB returned an unexpected response", "upstream")
    body = doc.get("getLindsByUniprotsResponse") or doc.get("getLigandsByUniprotsResponse")
    if not isinstance(body, dict):
        raise common.UpstreamError("BindingDB response has no affinities block", "upstream")
    raw = body.get("affinities") or []
    if isinstance(raw, dict):  # a single record may come back as an object
        raw = [raw]
    rows = [r for r in raw if isinstance(r, dict)]
    _CACHE.set(key, rows)
    return rows


# -----------------------------------------------------------------------------
# Records
# -----------------------------------------------------------------------------

def plain_decimal(raw: Any) -> str:
    """The exact decimal value of a number string in positional notation: never rounded and never in e-notation
    (DAT-013). Only padding zeros after the decimal point are dropped ("0.100000" -> "0.1", "31622.78" and
    "1234567" unchanged, "1.5e-05" -> "0.000015")."""
    try:
        d = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return str(raw).strip()
    if not d.is_finite():
        return str(raw).strip()
    out = format(d, "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return out or "0"


def _fmt(value: float) -> str:
    """A float shown exactly as its shortest repr, in positional notation (used for the cutoff)."""
    return plain_decimal(repr(float(value)))


def parse_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    m = _VALUE_RE.match(str(rec.get("affinity") or ""))
    if not m:
        return None
    relation = m.group(1).replace("==", "=") or "="
    value = float(m.group(2))  # for sorting only; the shown value is the upstream string (DAT-013)
    if value <= 0:  # a zero affinity is a data-entry artefact, not a measurement
        return None
    mid = str(rec.get("monomerid") or "").strip()
    pmid = str(rec.get("pmid") or "").strip()
    doi = str(rec.get("doi") or "").strip()
    shown = plain_decimal(m.group(2))
    out = {
        "monomer_id": mid,
        "affinity_type": str(rec.get("affinity_type") or "").strip(),
        "relation": relation,
        "value": shown,
        "units": "nM",
        "value_text": f"{'' if relation == '=' else relation}{shown} nM",
        "target_name": str(rec.get("query") or "").strip(),
        "pmid": pmid if pmid.isdigit() else "",
        "doi": doi,
        "smiles": str(rec.get("smile") or "").strip(),
        "url": MONOMER_URL.format(mid) if mid else None,
        "_v": value,
    }
    return out


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in row.items() if not k.startswith("_") and v not in (None, "")}
    if len(out.get("smiles") or "") > 200:
        out["smiles"] = out["smiles"][:200] + "..."
    return out


def _rank(row: Dict[str, Any]) -> Tuple[int, int, float, int]:
    # Records for the queried protein itself first (the response can include a partner chain such as erbB-2), then
    # exact values before "<" bounds; ">" bounds say little about potency and sort last.
    rel = {"=": 0, "~": 0, "<": 1, "<=": 1, ">": 2, ">=": 2}.get(row["relation"], 1)
    return 0 if row.get("_primary", True) else 1, rel, row["_v"], 0 if row.get("pmid") else 1


def heavy_atoms(smiles: str) -> int:
    """Heavy-atom count of the largest '.'-separated fragment, by tokens only (a cheap pre-filter)."""
    parts = (smiles or "").split()
    frags = parts[0].split(".") if parts else []
    return max((sum(1 for t in _ATOM_RE.findall(f) if not re.match(r"^\[\d*H[^a-z]", t)) for f in frags), default=0)


def _match_keys(rows: List[Dict[str, Any]], wanted: Dict[str, str]) -> Dict[str, List[Dict[str, Any]]]:
    """drug name -> rows whose SMILES has the drug's connectivity key (keys cached per SMILES)."""
    by_key = {v: k for k, v in wanted.items()}
    sizes = {int(k.split(":", 1)[0]) for k in by_key}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        smi = row.get("smiles") or ""
        cached = _KEY_CACHE.get(smi) if smi else None
        if not smi or (cached is None and heavy_atoms(smi) not in sizes):
            continue
        if cached is None:
            cached = (skeleton_key(smi),)
            _KEY_CACHE.set(smi, cached)
        key = cached[0]
        if key and int(key.split(":", 1)[0]) in sizes and key in by_key:
            out.setdefault(by_key[key], []).append(row)
    return out


async def _drug_keys(names: List[str]) -> Tuple[Dict[str, str], List[Dict[str, Any]], Dict[str, str]]:
    """drug -> constitution key from its PubChem SMILES, a per-drug status list, and drug -> the SMILES used (the
    isomeric SMILES when PubChem gives one, so its stereochemistry and isotopes can be compared)."""
    from . import pubchem

    if not names:
        return {}, [], {}
    res = await pubchem.pubchem_properties(names)
    items = {str(i.get("query")): i for i in (common.data_of(res).get("items") or [])} if res.success else {}
    keys: Dict[str, str] = {}
    smiles: Dict[str, str] = {}
    status: List[Dict[str, Any]] = []
    for n in names:
        it = items.get(n) or {}
        smi = it.get("smiles") or it.get("connectivity_smiles")
        key = skeleton_key(smi) if smi else None
        if key:
            keys[n] = key
            smiles[n] = str(smi)
            status.append({"drug": n, "pubchem_cid": it.get("cid"), "structure": "resolved"})
        else:
            why = "PubChem could not be queried" if not res.success and getattr(res.error_type, "value", "") != "NOT_FOUND" else "no PubChem structure for this name"
            status.append({"drug": n, "structure": "unresolved", "detail": why})
    return keys, status, smiles


def mutant_note(rows: List[Dict[str, Any]], variants: List[str]) -> Optional[str]:
    if not variants:
        return None
    names = Counter(r.get("target_name") or "" for r in rows)
    mutant_names = sorted(n for n in names if n and any(v.upper() in n.upper() for v in variants))
    if mutant_names:
        return f"BindingDB target names mention {', '.join(variants)} in: {'; '.join(mutant_names[:3])}."
    shown = "; ".join(f"{n} ({c} records)" for n, c in names.most_common(3) if n)
    return (
        f"No BindingDB record here is annotated with {', '.join(variants)}: the records name the target only "
        f"({shown or 'no target names'}), so none can be read as a mutant-specific affinity."
    )


async def bindingdb_affinities(
    gene: str,
    uniprot: Optional[str] = None,
    drugs: Optional[List[str]] = None,
    cutoff_nm: Optional[float] = None,
    variants: Optional[List[str]] = None,
) -> Any:
    """Measured affinities from BindingDB for one target (by UniProt), named drugs matched by structure first."""
    symbol = gene.strip().upper()
    drugs = list(drugs or [])[:3]
    variants = [v.strip().upper() for v in (variants or []) if v and v.strip()][:4]
    cutoff = float(cutoff_nm) if cutoff_nm else (DRUG_CUTOFF_NM if drugs else DEFAULT_CUTOFF_NM)
    subject = symbol
    try:
        async with _bdb_client() as http:
            acc = uniprot or await resolve_uniprot(http, symbol)
            if not acc:
                raise common.UpstreamError(f"no reviewed human UniProt entry for {symbol}", "not_found")
            subject = f"{symbol} ({acc})"
            fetched, (keys, drug_status, drug_smiles) = await asyncio.gather(
                fetch_affinities(http, acc, cutoff), _drug_keys(drugs)
            )
    except Exception as exc:  # noqa: BLE001
        return common.failure(TOOL_NAME, exc, subject)

    rows = [r for r in (parse_record(x) for x in fetched) if r is not None]
    names = Counter(r["target_name"] for r in rows if r["target_name"])
    primary = names.most_common(1)[0][0] if names else ""
    for r in rows:
        r["_primary"] = not primary or r["target_name"] == primary
    matched: Dict[str, List[Dict[str, Any]]] = {}
    if keys:
        matched = await asyncio.to_thread(_match_keys, rows, keys)
    picked: List[Dict[str, Any]] = []
    seen: Set[int] = set()
    excluded: Dict[str, int] = {}
    for name in drugs:
        # Same constitution is necessary but not sufficient (DAT-003): a record whose specified stereochemistry
        # differs from the drug's (another enantiomer, the E/Z isomer), or cannot be compared, is not attributed.
        attributable: List[Dict[str, Any]] = []
        for row in matched.get(name) or []:
            relation = stereo_relation(drug_smiles.get(name, ""), row.get("smiles") or "")
            if relation in MATCH_BASIS:
                row["_stereo"] = relation
                attributable.append(row)
            else:
                excluded[name] = excluded.get(name, 0) + 1
                row["same_constitution_as"] = name
                row["stereo_relation"] = relation
        matched[name] = attributable
        for row in sorted(attributable, key=_rank)[:PER_DRUG_ROWS]:
            # The ligand stays the BindingDB monomer; the drug it was matched to and the basis of the match are
            # stated next to it, never in its place.
            picked.append({
                **row,
                "ligand": f"BindingDB monomer {row['monomer_id']}" if row.get("monomer_id") else "BindingDB monomer",
                "matched_drug": name,
                "match_basis": MATCH_BASIS[row["_stereo"]],
                "stereo_relation": row["_stereo"],
            })
            seen.add(id(row))
    for st in drug_status:
        n = len(matched.get(st["drug"]) or [])
        st["n_records"] = n
        if excluded.get(st["drug"]):
            st["n_stereoisomer_records_excluded"] = excluded[st["drug"]]
        if st["structure"] == "resolved":
            st["status"] = "matched" if n else ("stereoisomer_only" if excluded.get(st["drug"]) else "not_in_records")
    best: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if id(row) in seen or row["relation"].startswith(">"):
            continue
        k = row["monomer_id"] or row["smiles"]
        if k not in best or _rank(row) < _rank(best[k]):
            best[k] = row
    for row in sorted(best.values(), key=_rank):
        if len(picked) >= MAX_ROWS:
            break
        picked.append({**row, "ligand": f"BindingDB monomer {row['monomer_id']}"})
    items = [_public(r) for r in picked[:MAX_ROWS]]
    note = mutant_note(rows, variants)
    pmids = list(dict.fromkeys(i["pmid"] for i in items if i.get("pmid")))
    dois = list(dict.fromkeys(i["doi"] for i in items if i.get("doi")))
    data = {
        "subject": subject,
        "gene": symbol,
        "uniprot": acc,
        "cutoff_nm": _fmt(cutoff),
        "n_records": len(rows),
        "n_ligands": len({r["monomer_id"] for r in rows}),
        "affinity_types": dict(Counter(r["affinity_type"] for r in rows).most_common()),
        "target_names": [n for n, _ in names.most_common(5)],
        "drugs": drug_status,
        "variants": variants,
        "mutant_note": note,
        "items": items,
        "citation_records": [
            *({"type": "pmid", "id": p, "url": f"https://pubmed.ncbi.nlm.nih.gov/{p}/",
               "section": f"BindingDB affinity records for {symbol}"} for p in pmids),
            {"type": "url", "id": f"BindingDB:{acc}", "title": f"BindingDB affinities for {symbol} ({acc}), <= {_fmt(cutoff)} nM",
             "url": f"https://www.bindingdb.org/rwd/bind/tabLuceneResult.jsp?thisInput={acc}&submit=Go",
             "section": "BindingDB getLigandsByUniprots"},
        ],
    }
    return common.ok(TOOL_NAME, data, {"uniprot_id": acc, "pmids": ",".join(pmids), "dois": ",".join(dois)})


def tool_entry() -> Any:
    from ..tools.registry import AdapterType, ToolUniverseEntry

    return ToolUniverseEntry(
        name=TOOL_NAME,
        description=(
            "BindingDB measured binding affinities (Ki, Kd, IC50, EC50 in nM, with PMID/DOI) of small molecules for "
            "a protein target by UniProt accession; named drugs are matched by structure, otherwise the most potent "
            "ligands are listed."
        ),
        domain="drugs",
        adapter_type=AdapterType.DIRECT_REST,
        input_schema=BindingdbInput,
        canonical_id_types=["uniprot_id", "pmids", "dois"],
        timeout_seconds=100.0,
        tags=["bindingdb", "binding_affinity", "affinity", "potency", "ic50", "ki", "kd", "ligand", "chemistry",
              "drug", "upstream:bindingdb"],
        is_async=True,
    )


class BindingdbSource(common.OmicsSource):
    key = SOURCE_KEY
    agent = "Synapta"
    action = "BindingDB measured binding affinities"
    tool_name = TOOL_NAME
    artifact_name = "bindingdb"
    label = "BindingDB"
    subject_kind = "protein target"
    nothing_found = "affinity records"
    name_re = NAME_RE
    order = 67

    def intent(self, ctx: QueryContext) -> bool:
        q = common.query_text(ctx)
        return bool(INTENT_RE.search(q) or INTENT_CASE_RE.search(q))

    def build_plan(self, ctx: QueryContext) -> Optional[Dict[str, Any]]:
        gene = common.gene_symbol(ctx)
        if not gene:
            return None
        drugs: List[str] = []
        for d in ctx.drugs:
            name = (d.mention or d.label or "").strip()
            if name and not re.search(r"[/\\?#]", name) and name.lower() not in {n.lower() for n in drugs}:
                drugs.append(name[:100])
        variants: List[str] = []
        for v in ctx.variants:
            pc = (v.protein_change or "").strip().upper()
            if pc and (not v.gene or v.gene.upper() == gene):
                variants.append(pc)
        variants.extend(m.group(0).upper() for m in _PROTEIN_CHANGE_RE.finditer(common.query_text(ctx)))
        plan: Dict[str, Any] = {"gene": gene, "_subject": gene}
        acc = common.uniprot_accession(ctx)
        if acc:
            plan["uniprot"] = acc
        if drugs:
            plan["drugs"] = drugs[:3]
        variants = list(dict.fromkeys(variants))[:4]
        if variants:
            plan["variants"] = variants
        return plan

    def describe(self, result: Any, ctx: QueryContext) -> Tuple[str, str]:
        if not result.success:
            return super().describe(result, ctx)
        d = common.data_of(result)
        matched = [s["drug"] for s in d.get("drugs") or [] if s.get("status") == "matched"]
        detail = f"{len(self.items(d))} shown"
        if matched:
            detail += f"; matched by structure: {', '.join(matched)}"
        return f"BindingDB: {d.get('n_records')} affinity records <= {d.get('cutoff_nm')} nM for {d.get('subject')}", detail

    def compact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        keys = ("gene", "uniprot", "cutoff_nm", "n_records", "n_ligands", "affinity_types", "target_names", "drugs",
                "variants", "mutant_note", "items")
        return {k: data.get(k) for k in keys}

    def evidence_xml(self, result: Any) -> str:
        d = common.data_of(result)
        lines = [
            f'<bindingdb_affinities source="BindingDB getLigandsByUniprots" target="{common.attr(d.get("gene"))}" '
            f'uniprot="{common.attr(d.get("uniprot"))}" cutoff_nm="{common.attr(d.get("cutoff_nm"))}" '
            f'n_records="{common.attr(d.get("n_records"))}" n_ligands="{common.attr(d.get("n_ligands"))}" '
            'note="Measured affinities in nM at or below the cutoff, one per row with its PMID or DOI. Quote type, '
            'relation, value and units exactly and cite the PMID/DOI of the row. Every ligand is a BindingDB monomer; '
            'a row is attributed to a named drug (matched_drug) only when the monomer has the same constitution as '
            'the PubChem structure of the drug (same atoms, hydrogens, charges and isotopes; salts ignored), and '
            'match_basis says whether stereochemistry was compared. Rows with the constitution of a drug but different '
            'stereochemistry (another enantiomer or the E/Z isomer) are not attributed to it (same_constitution_as). '
            'Unmatched rows are other compounds, not named drugs. Records are not annotated with mutations.">'
        ]
        for s in d.get("drugs") or []:
            lines.append("<drug " + " ".join(f'{k}="{common.attr(v)}"' for k, v in s.items() if v is not None) + "/>")
        for it in self.items(d):
            fields = {k: it.get(k) for k in ("ligand", "monomer_id", "matched_drug", "match_basis", "same_constitution_as",
                                             "stereo_relation", "affinity_type", "relation", "value", "units",
                                             "target_name", "pmid", "doi", "smiles")}
            lines.append("<affinity " + " ".join(f'{k}="{common.attr(v)}"' for k, v in fields.items() if v not in (None, "")) + "/>")
        if d.get("mutant_note"):
            lines.append(f"<mutant_note>{common.text(d['mutant_note'])}</mutant_note>")
        lines.append("</bindingdb_affinities>")
        return "\n".join(lines)

    def allowed_identifiers(self, result: Any) -> Dict[str, Set[str]]:
        items = self.items(common.data_of(result))
        dois: Set[str] = set()
        for i in items:
            if i.get("doi"):
                dois.update({str(i["doi"]), str(i["doi"]).lower()})
        out: Dict[str, Set[str]] = {"pmids": {str(i["pmid"]) for i in items if i.get("pmid")}, "dois": dois}
        acc = common.data_of(result).get("uniprot")
        if acc:
            out["uniprot"] = {str(acc)}
        return out

    def scope_notes(self, ctx: QueryContext, result: Optional[Any]) -> List[str]:
        notes = super().scope_notes(ctx, result)
        if result is None or not result.success:
            return notes
        d = common.data_of(result)
        for s in d.get("drugs") or []:
            if s.get("status") == "not_in_records":
                notes.append(
                    f"No BindingDB record at or below {d.get('cutoff_nm')} nM matched the structure of {s['drug']} for "
                    f"{d.get('gene')}; this is not evidence that it does not bind. Do not state a BindingDB value for it."
                )
            elif s.get("status") == "stereoisomer_only":
                notes.append(
                    f"BindingDB has records for {s['drug']}'s constitution only with different or uncomparable "
                    f"stereochemistry ({s.get('n_stereoisomer_records_excluded')} records, e.g. another enantiomer or "
                    f"the E/Z isomer); they are not {s['drug']}'s values. Do not state a BindingDB value for it."
                )
            elif s.get("structure") == "unresolved":
                notes.append(f"{s['drug']} could not be matched to BindingDB records ({s.get('detail')}); do not state a BindingDB value for it.")
        if d.get("mutant_note"):
            notes.append(str(d["mutant_note"]) + " Do not present these values as mutant-specific.")
        return notes
