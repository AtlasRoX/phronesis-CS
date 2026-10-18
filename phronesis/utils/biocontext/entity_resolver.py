"""
Live entity normalization for the query planner: OpenTargets mapIds (targets, drugs, diseases in one batched
call), HGNC fetch (symbol, alias, previous symbol, MANE transcript), NCBI dbSNP esummary, NCBI Variation HGVS
validation and ChEMBL synonym lookup for research codes OpenTargets does not know.

mapIds matching is fuzzy and case-insensitive ("in" -> CD44, "MET" -> METHADONE, "AML" -> AMLODIPINE), so every
hit passes strict acceptance rules before it becomes an entity. Each source has its own timeout inside one
overall deadline, failures degrade to partial results, and successful lookups are cached for 24 h.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import unicodedata
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import httpx

from phronesis.agents.entity_lexicon import (
    BIOMARKER_ACRONYMS,
    ENDPOINT_ACRONYMS,
    NON_GENE_ACRONYMS,
    coordinated_disease_terms,
    defined_acronyms,
    family_members,
    is_gene_mention,
)
from phronesis.agents.query_context import ResolvedEntity
from phronesis.utils.http_limits import TTLCache, get_limiter

logger = logging.getLogger(__name__)

OPENTARGETS_URL = "https://api.platform.opentargets.org/api/v4/graphql"
HGNC_URL = "https://rest.genenames.org/fetch"
EUTILS_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
NCBI_VARIATION_URL = "https://api.ncbi.nlm.nih.gov/variation/v0/hgvs"
CHEMBL_URL = "https://www.ebi.ac.uk/chembl/api/data/molecule.json"

MAPIDS_QUERY = (
    'query($t:[String!]!){ mapIds(queryTerms:$t, entityNames:["target","drug","disease"]) '
    "{ total mappings { term hits { id name entity score } } } }"
)

DEFAULT_TIMEOUTS: Dict[str, float] = {"opentargets": 3.0, "hgnc": 2.5, "dbsnp": 2.5, "ncbi_variation": 2.5, "chembl": 3.0}
DEFAULT_DEADLINE_S = 3.2

_CACHE = TTLCache(maxsize=2048, ttl_s=24 * 3600)

GENE_SHAPE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}(?:-[A-Z0-9]{1,4})?$")
CODE_SHAPE_RE = re.compile(r"^[A-Z]{1,5}-?\d{2,6}$")
_ABBREV_SHAPE_RE = re.compile(r"^(?=.*[A-Z])(?=.*[-\d])[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+$")
_TRADE_SHAPE_RE = re.compile(r"^[A-Z][a-z]{3,}$")
_ACRONYM_RE = re.compile(r"^[A-Z][A-Z0-9]{1,6}$")
_DISEASE_SUFFIX_RE = re.compile(
    r"(?:omas?|emias?|aemias?|itis|cancers?|syndromes?|diseases?|disorders?|carcinomas?|tumou?rs?|neoplasms?|"
    r"leuka?emias?|lymphomas?|sarcomas?|fibrosis|diabetes|infections?|deficiency)$",
    re.I,
)
# OpenTargets' disease index also holds GO processes, mouse phenotypes and EFO measurements ("dose", "age").
_NON_DISEASE_ID_RE = re.compile(r"^(?:GO|MP)_")
_NON_DISEASE_WORDS = frozenset(
    """
    dose dosing age time mortality survival death toxicity efficacy response exposure pregnancy genotype phenotype
    cognition puberty aging ageing growth sleep memory behavior behaviour menopause weight height risk
    disease diseases disorder disorders syndrome
    duration durations interval intervals parameter parameters simulation simulations second seconds minute minutes
    hour hours millisecond milliseconds unit units range limit point points value values concentration
    """.split()
)
# Trial endpoints and measurements that EFO lists as "diseases" ("overall survival", "response rate", "hazard ratio").
_ENDPOINT_PHRASE_RE = re.compile(
    r"^(?:(?:median|overall|progression[\s\-]free|disease[\s\-]free|event[\s\-]free|recurrence[\s\-]free|relapse[\s\-]free|"
    r"metastasis[\s\-]free|distant[\s\-]metastasis[\s\-]free|cancer[\s\-]specific|disease[\s\-]specific|\d+[\s\-]years?|"
    r"long[\s\-]term|short[\s\-]term|patient|graft|net|relative)\s+)+survival(?:\s+(?:rates?|times?|benefit|outcomes?|analysis))?$"
    r"|^survival(?:\s+(?:rates?|times?|benefit|outcomes?|analysis))?$"
    r"|^(?:overall|objective|complete|partial|pathologic(?:al)?\s+complete|major\s+pathologic(?:al)?|clinical|tumou?r|"
    r"treatment|durable|best\s+overall|radiographic|molecular|cytogenetic|hematologic)\s+responses?(?:\s+rates?)?$"
    r"|^responses?\s+rates?$|^(?:hazard|odds|risk|rate)\s+ratios?$|^relative\s+risks?$"
    r"|^(?:serious\s+|treatment[\s\-](?:related|emergent)\s+|grade\s+\d(?:\s*[\-\u2013]\s*\d)?\s+)?adverse\s+(?:events?|effects?|reactions?)$"
    r"|^(?:health[\s\-]related\s+)?quality\s+of\s+life$|^time\s+to\s+(?:\w+[\s\-]?){1,3}$|^duration\s+of\s+(?:response|remission)$"
    r"|^disease\s+control(?:\s+rates?)?$|^(?:mortality|death|remission|relapse|recurrence|toxicity|incidence|cure)\s+rates?$"
    r"|^dose[\s\-]limiting\s+toxicit(?:y|ies)$|^maximum\s+tolerated\s+doses?$|^disease\s+progression$|^progression[\s\-]free$",
    re.I,
)
# Acronyms OpenTargets maps to unrelated diseases ("OS" -> osteosarcoma, "HRD" -> HRD syndrome) in a trial context.
# Gene-family names written without the member number ("BRCA", "RAS", "CDK"): never a disease, although some are
# also cohort codes (TCGA "BRCA" = breast invasive carcinoma) that OpenTargets' disease index matches.
_GENE_FAMILY_ACRONYMS = {
    "BRCA", "RAS", "RAF", "CDK", "JAK", "PI3K", "HER", "ERBB", "FGFR", "IDH", "NTRK", "PARP", "MEK", "ERK", "AKT",
    "MTOR", "SRC", "TRK", "ROS", "RET", "KIT", "VEGFR", "PDGFR", "HDAC", "BET", "PD1", "PDL1",
}
_NON_DISEASE_ACRONYMS = ENDPOINT_ACRONYMS | (BIOMARKER_ACRONYMS - {"CIN"})

# Generic biochemicals OpenTargets lists as drugs (TYROSINE CHEMBL925, ZINC, GLUCOSE): never a drug mention here.
_GENERIC_BIOCHEMICALS = frozenset(
    """
    alanine arginine asparagine aspartate aspartic cysteine glutamine glutamate glutamic glycine histidine isoleucine
    leucine lysine methionine phenylalanine proline serine threonine tryptophan tyrosine valine selenocysteine
    ornithine citrulline taurine
    atp adp amp gtp gdp gmp ctp utp camp cgmp nad nadh nadp nadph fad fadh2 coa acetyl-coa
    adenine guanine cytosine thymine uracil guanosine cytidine uridine thymidine inosine
    calcium zinc iron magnesium sodium potassium chloride copper manganese phosphate bicarbonate selenium
    oxygen glucose fructose sucrose lactate pyruvate water cholesterol ethanol urea ammonia nitrogen hydrogen carbon
    """.split()
)
# Endogenous molecules that are also real drugs: rejected unless a therapeutic word sits next to the mention.
_CONTEXTUAL_BIOCHEMICALS = frozenset("adenosine folate retinol riboflavin thiamine niacin biotin".split())
_VITAMIN_RE = re.compile(r"^vitamin\s+[a-k]\d{0,2}$", re.I)
_THERAPEUTIC_WORDS = frozenset(
    """
    supplement supplements supplementation supplemented therapy therapies treatment treated treat dose doses dosing
    dosed administration administered infusion infused injection injected intravenous iv oral orally given mg
    regimen prescribed receiving received bolus drug
    """.split()
)
# A word right after the mention that makes it part of a protein, enzyme or process name ("tyrosine kinase").
_BIO_PHRASE_AFTER = frozenset(
    """
    kinase kinases phosphatase phosphatases deaminase deaminases synthase synthases synthetase transferase
    transferases dehydrogenase reductase oxidase hydroxylase hydrolase ligase isomerase mutase carboxylase
    decarboxylase protease peptidase receptor receptors transporter transporters channel channels residue residues
    phosphorylation biosynthesis triphosphate diphosphate monophosphate signaling signalling pathway pathways
    """.split()
)
_BIO_PHRASE_BEFORE = frozenset("receptor non-receptor protein".split())
_SOFT_PHRASE_AFTER = frozenset("metabolism metabolic level levels homeostasis deficiency uptake".split())


def _ot_key(term: str) -> str:
    """mapIds misses curly apostrophes and accented letters (U+2019, "Sjogren" with an umlaut); send an ASCII form."""
    folded = unicodedata.normalize("NFKD", term.replace("\u2019", "'")).encode("ascii", "ignore").decode()
    return folded or term


class _SourceFailure(Exception):
    def __init__(self, status: str, detail: str = "") -> None:
        super().__init__(detail or status)
        self.status = status


def _find_span(query: str, term: str) -> Tuple[int, int]:
    for flags in (0, re.I):
        m = re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", query, flags)
        if m:
            return m.start(), m.end()
    return (0, 0)


def _mention(query: str, term: str) -> Tuple[str, Tuple[int, int]]:
    span = _find_span(query, term)
    return (query[span[0]:span[1]] if span != (0, 0) else term), span


def _sentence_initial(query: str, start: int) -> bool:
    before = query[:start].rstrip()
    return not before or before[-1] in ".!?:;\n"


def _is_shaped_drug_term(query: str, term: str) -> bool:
    """Research code (AMG510, BI-3406), mixed abbreviation (T-DXd) or a capitalized trade name mid-sentence."""
    if CODE_SHAPE_RE.match(term) or _ABBREV_SHAPE_RE.match(term):
        return True
    if _TRADE_SHAPE_RE.match(term):
        start, end = _find_span(query, term)
        return (start, end) != (0, 0) and not _sentence_initial(query, start)
    return False


def _words(text: str) -> List[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z0-9]+", text)]


# KG-014: words that say what kind of malignancy something is, folded to one head word for matching.
_MALIGNANCY_HEADS = frozenset("cancer carcinoma neoplasm neoplasia tumor malignancy".split())
# Function words and staging qualifiers carry no identity of the disease ("metastatic pancreatic cancer" is still
# pancreatic cancer); acute / chronic / site / histology words do and are kept.
_DISEASE_FILLER_WORDS = frozenset(
    "of the and in with a an to for or by on metastatic advanced recurrent refractory relapsed unresectable "
    "locally stage".split()
)


def _disease_word(w: str) -> str:
    w = re.sub(r"ae|oe", "e", w)  # leukaemia / oesophageal / tumour spellings
    w = re.sub(r"our$", "or", w)
    if len(w) > 4 and w.endswith("s") and not w.endswith(("ss", "is", "us")):
        w = w[:-1]  # "cancers" ~ "cancer"
    return "cancer" if w in _MALIGNANCY_HEADS else w


def _disease_word_set(text: str) -> Set[str]:
    """The distinctive words of a disease name, spelling- and plural-folded, malignancy heads merged (KG-014)."""
    drop = _NON_DISEASE_WORDS | _DISEASE_FILLER_WORDS
    return {_disease_word(w) for w in _words(_ot_key(text))
            if w != "s" and w not in drop and not (w.endswith("s") and w[:-1] in drop)}  # "Alzheimer's"


def _occurrences(query: str, term: str) -> List[Tuple[int, int]]:
    pat = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", re.I)
    return [m.span() for m in pat.finditer(query)]


def _is_biochemical_mention(query: str, term: str) -> bool:
    """True when term names a generic biochemical, or every mention of it sits inside a biological phrase
    ("receptor tyrosine kinase", "insulin receptor", "adenosine signaling"), so it is not a drug mention."""
    key = " ".join(term.lower().split())
    if key in _GENERIC_BIOCHEMICALS:
        return True
    spans = _occurrences(query, term)
    if not spans:
        return False
    soft = key in _CONTEXTUAL_BIOCHEMICALS or bool(_VITAMIN_RE.match(key))

    def biological(start: int, end: int) -> bool:
        left = [w.lower() for w in re.findall(r"[A-Za-z][\w\-]*", query[:start])[-3:]]
        right = [w.lower() for w in re.findall(r"[A-Za-z][\w\-]*", query[end:])[:3]]
        if right and right[0] in _BIO_PHRASE_AFTER:
            return True
        if left and left[-1] in _BIO_PHRASE_BEFORE and not query[:start].rstrip().endswith(","):
            return True
        if soft:
            if right and right[0] in _SOFT_PHRASE_AFTER:
                return True
            return not any(w in _THERAPEUTIC_WORDS for w in left + right)
        return False

    return all(biological(s, e) for s, e in spans)


def _contains(outer: Tuple[int, int], inner: Tuple[int, int]) -> bool:
    return outer != inner and outer[0] <= inner[0] and inner[1] <= outer[1] and outer != (0, 0)


class EntityResolver:
    """Resolve candidate mentions of one question. resolve() never raises; it reports per-source status."""

    def __init__(
        self,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        cache: Optional[TTLCache] = None,
        timeouts: Optional[Dict[str, float]] = None,
        deadline_s: float = DEFAULT_DEADLINE_S,
        hgnc_concurrency: int = 8,
    ) -> None:
        self._transport = transport
        self._cache = cache if cache is not None else _CACHE
        self._timeouts = {**DEFAULT_TIMEOUTS, **(timeouts or {})}
        self._deadline_s = deadline_s
        self._hgnc_concurrency = max(1, hgnc_concurrency)

    # ------------------------------------------------------------------ http
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(4.0, connect=3.0),
            headers={"User-Agent": "phronesis-entity-resolver/1.0"},
            follow_redirects=True,
        )

    async def _timed(
        self, name: str, fn: Callable[[], Awaitable[Any]], deadline: float, status: Dict[str, str], timings: Dict[str, float]
    ) -> Any:
        t0 = time.perf_counter()
        budget = min(self._timeouts.get(name, 3.0), max(0.0, deadline - time.monotonic()))
        try:
            if budget <= 0:
                raise asyncio.TimeoutError()
            out = await asyncio.wait_for(fn(), timeout=budget)
            status[name] = "ok"
            return out
        except asyncio.TimeoutError:
            status[name] = "timeout"
        except _SourceFailure as exc:
            status[name] = exc.status
            logger.info("Entity source %s failed: %s", name, exc)
        except Exception as exc:  # noqa: BLE001
            status[name] = "error"
            logger.info("Entity source %s failed: %s", name, exc)
        finally:
            timings[name] = round((time.perf_counter() - t0) * 1000, 1)
        return None

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        if resp.status_code >= 500 or resp.status_code == 429:
            raise _SourceFailure("error", f"HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise _SourceFailure("error", "invalid JSON") from exc

    # --------------------------------------------------------------- sources
    async def _opentargets(self, client: httpx.AsyncClient, terms: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        missing = []
        for t in terms:
            cached = self._cache.get(("ot", t))
            if cached is None:
                missing.append(t)
            else:
                out[t] = cached
        if not missing:
            return out
        keys = {t: _ot_key(t) for t in missing}
        sent = list(dict.fromkeys(keys.values()))
        resp = await client.post(OPENTARGETS_URL, json={"query": MAPIDS_QUERY, "variables": {"t": sent}})
        body = self._json(resp)
        if resp.status_code >= 400 or not isinstance(body, dict) or body.get("errors"):
            raise _SourceFailure("error", str((body or {}).get("errors") if isinstance(body, dict) else resp.status_code)[:200])
        mappings = (((body.get("data") or {}).get("mapIds") or {}).get("mappings")) or []
        by_key = {m.get("term"): [h for h in (m.get("hits") or []) if isinstance(h, dict) and h.get("id")] for m in mappings}
        fresh: Dict[str, List[Dict[str, Any]]] = {t: by_key.get(keys[t], []) for t in missing}
        for t, hits in fresh.items():
            self._cache.set(("ot", t), hits)
        out.update(fresh)
        return out

    async def _hgnc_fetch(self, client: httpx.AsyncClient, sem: asyncio.Semaphore, field: str, term: str) -> List[Dict[str, Any]]:
        key = ("hgnc", field, term)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        async with sem:
            resp = await client.get(f"{HGNC_URL}/{field}/{urllib.parse.quote(term, safe='')}", headers={"Accept": "application/json"})
        body = self._json(resp)
        if resp.status_code >= 400 or not isinstance(body, dict):
            raise _SourceFailure("error", f"HTTP {resp.status_code}")
        docs = [d for d in ((body.get("response") or {}).get("docs") or []) if isinstance(d, dict) and d.get("symbol")]
        self._cache.set(key, docs)
        return docs

    async def _hgnc_token(self, client: httpx.AsyncClient, sem: asyncio.Semaphore, token: str) -> Dict[str, Any]:
        symbol_docs, alias_docs = await asyncio.gather(
            self._hgnc_fetch(client, sem, "symbol", token), self._hgnc_fetch(client, sem, "alias_symbol", token)
        )
        if symbol_docs:
            return {"doc": symbol_docs[0], "matched_on": "symbol"}
        docs, field = alias_docs, "alias_symbol"
        if not docs:
            docs, field = await self._hgnc_fetch(client, sem, "prev_symbol", token), "prev_symbol"
        if len(docs) == 1:
            return {"doc": docs[0], "matched_on": field}
        if len(docs) > 1:
            return {"ambiguous": [d["symbol"] for d in docs], "matched_on": field}
        return {}

    async def _hgnc(self, client: httpx.AsyncClient, tokens: Sequence[str], transcripts: Sequence[str]) -> Dict[str, Any]:
        sem = asyncio.Semaphore(self._hgnc_concurrency)
        gene_jobs = [self._hgnc_token(client, sem, t) for t in tokens]
        mane_jobs = [self._hgnc_fetch(client, sem, "mane_select", t) for t in transcripts]
        results = await asyncio.gather(*gene_jobs, *mane_jobs, return_exceptions=True)
        genes, manes, failures = {}, {}, 0
        for token, res in zip(tokens, results[: len(tokens)]):
            if isinstance(res, BaseException):
                failures += 1
                continue
            genes[token] = res
        for tx, res in zip(transcripts, results[len(tokens):]):
            if isinstance(res, BaseException):
                failures += 1
                continue
            manes[tx] = res[0] if len(res) == 1 else None
        if failures and not genes and not manes:
            raise _SourceFailure("error", "all HGNC lookups failed")
        return {"genes": genes, "manes": manes, "partial": bool(failures)}

    async def _dbsnp(self, client: httpx.AsyncClient, rsids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        missing = []
        for rs in rsids:
            cached = self._cache.get(("snp", rs))
            if cached is None:
                missing.append(rs)
            else:
                out[rs] = cached
        if not missing:
            return out
        params = {"db": "snp", "id": ",".join(rs[2:] for rs in missing), "retmode": "json", "tool": "phronesis"}
        if os.getenv("NCBI_API_KEY"):
            params["api_key"] = os.environ["NCBI_API_KEY"]
        async with get_limiter("ncbi_eutils", 0.34):
            resp = await client.get(EUTILS_URL, params=params)
        body = self._json(resp)
        if resp.status_code >= 400 or not isinstance(body, dict) or not isinstance(body.get("result"), dict):
            raise _SourceFailure("error", f"HTTP {resp.status_code}")
        result = body["result"]
        for rs in missing:
            doc = result.get(rs[2:])
            if not isinstance(doc, dict):
                continue
            entry = {
                "error": doc.get("error"),
                "genes": [g.get("name") for g in (doc.get("genes") or []) if isinstance(g, dict) and g.get("name")],
            }
            out[rs] = entry
            self._cache.set(("snp", rs), entry)
        return out

    async def _ncbi_variation(self, client: httpx.AsyncClient, hgvs_list: Sequence[str]) -> Dict[str, Optional[bool]]:
        async def one(hgvs: str) -> Optional[bool]:
            cached = self._cache.get(("hgvs", hgvs))
            if cached is not None:
                return cached
            async with get_limiter("ncbi_eutils", 0.34):
                resp = await client.get(f"{NCBI_VARIATION_URL}/{urllib.parse.quote(hgvs, safe='')}/contextuals")
            if resp.status_code >= 500 or resp.status_code == 429:
                raise _SourceFailure("error", f"HTTP {resp.status_code}")
            try:
                body = resp.json()
            except ValueError:
                return None
            validity = ((body.get("data") or body.get("error") or {}) if isinstance(body, dict) else {}).get("input_hgvs_validity")
            valid = True if validity == "valid" else False if validity == "invalid" else None
            if valid is not None:
                self._cache.set(("hgvs", hgvs), valid)
            return valid

        results = await asyncio.gather(*(one(h) for h in hgvs_list), return_exceptions=True)
        out = {h: (None if isinstance(r, BaseException) else r) for h, r in zip(hgvs_list, results)}
        if hgvs_list and all(isinstance(r, BaseException) for r in results):
            raise _SourceFailure("error", "all HGVS validations failed")
        return out

    async def _chembl(self, client: httpx.AsyncClient, codes: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
        async def one(code: str) -> List[Dict[str, Any]]:
            cached = self._cache.get(("chembl", code))
            if cached is not None:
                return cached
            resp = await client.get(
                CHEMBL_URL,
                params={
                    "molecule_synonyms__molecule_synonym__iexact": code,
                    "only": "molecule_chembl_id,pref_name,max_phase",
                    "limit": 3,
                },
                headers={"Accept": "application/json"},
            )
            body = self._json(resp)
            if resp.status_code >= 400 or not isinstance(body, dict):
                raise _SourceFailure("error", f"HTTP {resp.status_code}")
            mols = [m for m in (body.get("molecules") or []) if isinstance(m, dict) and m.get("molecule_chembl_id")]
            self._cache.set(("chembl", code), mols)
            return mols

        results = await asyncio.gather(*(one(c) for c in codes), return_exceptions=True)
        if codes and all(isinstance(r, BaseException) for r in results):
            raise _SourceFailure("error", "all ChEMBL lookups failed")
        return {c: r for c, r in zip(codes, results) if not isinstance(r, BaseException)}

    # ------------------------------------------------------------ acceptance
    @staticmethod
    def _gene_from_doc(query: str, token: str, info: Dict[str, Any], ot_target: Optional[Dict[str, Any]]) -> Optional[ResolvedEntity]:
        mention, span = _mention(query, token)
        members = family_members(token)
        if members:
            return ResolvedEntity(
                type="gene", mention=mention, span=span, label=token.upper(), source="HGNC",
                matched_on="family", ambiguous_with=list(members), validated=False,
            )
        if info.get("ambiguous"):
            return ResolvedEntity(
                type="gene", mention=mention, span=span, label=mention, source="HGNC",
                matched_on=info.get("matched_on"), ambiguous_with=list(info["ambiguous"])[:8], validated=False,
            )
        doc = info.get("doc")
        if not doc:
            return None
        ids = {"hgnc_id": str(doc.get("hgnc_id") or "")}
        ensembl = doc.get("ensembl_gene_id")
        if not ensembl and ot_target and str(ot_target.get("name", "")).upper() == str(doc["symbol"]).upper():
            ensembl = ot_target.get("id")  # only when OpenTargets named the same approved symbol
        if ensembl:
            ids["ensembl_id"] = str(ensembl)
        uniprot = doc.get("uniprot_ids") or []
        if uniprot:
            ids["uniprot_id"] = str(uniprot[0])
        aliases = [str(a) for a in (doc.get("alias_symbol") or []) + (doc.get("prev_symbol") or []) if a]
        return ResolvedEntity(
            type="gene", mention=mention, span=span, label=str(doc["symbol"]), ids={k: v for k, v in ids.items() if v},
            source="HGNC", matched_on=info.get("matched_on"), aliases=aliases[:12], validated=True,
        )

    @staticmethod
    def _accept_target(query: str, term: str, hits: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        targets = [h for h in hits if h.get("entity") == "target"]
        if not targets or not GENE_SHAPE_RE.match(term) or term.upper() in NON_GENE_ACRONYMS:
            return None
        span = _find_span(query, term)
        if span == (0, 0) or query[span[0]:span[1]] != term or not is_gene_mention(query, *span):
            return None
        defined = defined_acronyms(query).get(term.upper())
        if defined == "concept":
            return None  # "homologous recombination deficiency (HRD)" names a biomarker, not a gene
        exact = [h for h in targets if str(h.get("name", "")).upper() == term.upper()]
        if exact:
            return exact[0]
        if defined:
            return None  # an acronym the question spells out is not a fuzzy target hit
        return targets[0] if len(targets) == 1 else {"ambiguous": [str(h.get("name")) for h in targets]}

    @staticmethod
    def _accept_drug(query: str, term: str, hits: List[Dict[str, Any]]) -> Optional[Tuple[Dict[str, Any], str]]:
        drugs = [h for h in hits if h.get("entity") == "drug"]
        if not drugs or len(re.findall(r"[A-Za-z]", term)) < 3:
            return None
        if _is_biochemical_mention(query, term):
            return None  # "receptor tyrosine kinase" is not the drug TYROSINE
        exact = [h for h in drugs if str(h.get("name", "")).lower() == term.lower()]
        if exact:
            return exact[0], "name"
        if len(drugs) == 1 and _is_shaped_drug_term(query, term):
            name = str(drugs[0].get("name", "")).lower()
            if not name.startswith(term.lower()):
                return drugs[0], "synonym"
        return None

    @staticmethod
    def _accept_disease(term: str, hits: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        diseases = [h for h in hits if h.get("entity") == "disease" and not _NON_DISEASE_ID_RE.match(str(h.get("id", "")))]
        if not diseases:
            return None
        if _ENDPOINT_PHRASE_RE.match(" ".join(term.split())) or term.upper() in _NON_DISEASE_ACRONYMS:
            return None  # "overall survival", "hazard ratio", "OS": endpoints and measurements, not diseases
        exact = [h for h in diseases if str(h.get("name", "")).lower() == term.lower()]
        words = term.split()
        if len(words) == 1:
            if term.lower() in _NON_DISEASE_WORDS:
                return None
            if exact:
                return exact[0]
            if term.upper() in _GENE_FAMILY_ACRONYMS:
                return None  # "BRCA" in a PARP question means BRCA1/2, not TCGA's BRCA (breast carcinoma)
            if _ACRONYM_RE.match(term) or _DISEASE_SUFFIX_RE.search(term):
                return diseases[0]
            # "obesity" -> "obesity disorder", "hypertension" -> "hypertensive disorder", "Parkinson's" -> "Parkinson disease"
            base = re.sub(r"['\u2019]s$", "", term).lower()
            stem = re.escape(base[: max(5, len(base) - 3)])
            for h in diseases:
                if re.fullmatch(rf"{stem}\w*\s+(?:disorder|disease)", str(h.get("name", "")).lower()):
                    return h
            return None
        if exact:
            return exact[0]
        # KG-014: a multi-word term without an exact hit is accepted only when the hit keeps every distinctive word
        # of the term (site and histology; cancer ~ carcinoma ~ neoplasm ~ tumor as one head word), so "colon
        # cancer" never becomes "breast cancer" and "pancreatic cancer" never "pancreatic insufficiency". Among
        # qualifying hits the one adding the fewest words wins (the concept, not a subtype of it).
        term_words = _disease_word_set(term)
        if not term_words:
            return None
        best: Optional[Tuple[int, int, Dict[str, Any]]] = None
        for k, h in enumerate(diseases):
            hit_words = _disease_word_set(str(h.get("name", "")))
            if term_words <= hit_words:
                key = (len(hit_words - term_words), k)
                if best is None or key < best[:2]:
                    best = (key[0], key[1], h)
        return best[2] if best else None

    @classmethod
    def _accept_coordinated_disease(cls, term: str, hits: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """A disease term built from a coordinated phrase must match by name or keep its site word ("ovarian")."""
        hit = cls._accept_disease(term, hits)
        if hit is None:
            return None
        if str(hit.get("name", "")).lower() == term.lower():
            return hit
        site = _words(_ot_key(term))[0]
        for h in [hit, *hits]:
            if (
                h.get("entity") == "disease"
                and not _NON_DISEASE_ID_RE.match(str(h.get("id", "")))
                and site in _words(str(h.get("name", "")))
            ):
                return h
        return None

    # ---------------------------------------------------------------- public
    async def resolve(
        self,
        query: str,
        terms: Optional[Iterable[str]] = None,
        gene_tokens: Optional[Iterable[str]] = None,
        drug_codes: Optional[Iterable[str]] = None,
        rsids: Optional[Iterable[str]] = None,
        transcripts_hgvs: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        t_start = time.perf_counter()
        deadline = time.monotonic() + self._deadline_s
        terms_l = list(dict.fromkeys(t for t in (terms or []) if t and t.strip()))
        genes_l = list(dict.fromkeys(t for t in (gene_tokens or []) if t))
        codes_l = list(dict.fromkeys(t for t in (drug_codes or []) if t))
        rsids_l = list(dict.fromkeys(r.lower() for r in (rsids or []) if re.fullmatch(r"rs\d{3,12}", r, re.I)))
        hgvs_l = list(dict.fromkeys(h for h in (transcripts_hgvs or []) if h))
        transcripts = list(dict.fromkeys(h.split(":", 1)[0] for h in hgvs_l if re.match(r"^NM_\d+\.\d+:", h)))
        ot_terms = list(dict.fromkeys(terms_l + genes_l + codes_l))

        status: Dict[str, str] = {}
        timings: Dict[str, float] = {}
        async with self._client() as client:

            async def ot_then_chembl() -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
                ot_hits: Dict[str, List[Dict[str, Any]]] = {}
                if ot_terms:
                    ot_hits = await self._timed("opentargets", lambda: self._opentargets(client, ot_terms), deadline, status, timings) or {}
                # ChEMBL only for research codes OpenTargets did not resolve (all of them when it failed).
                missed = [
                    c for c in codes_l
                    if status.get("opentargets") != "ok" or not self._accept_drug(query, c, ot_hits.get(c, []))
                ]
                found: Dict[str, List[Dict[str, Any]]] = {}
                if missed:
                    found = await self._timed("chembl", lambda: self._chembl(client, missed), deadline, status, timings) or {}
                return ot_hits, found

            jobs: Dict[str, Awaitable[Any]] = {"opentargets": ot_then_chembl()}
            if genes_l or transcripts:
                jobs["hgnc"] = self._timed("hgnc", lambda: self._hgnc(client, genes_l, transcripts), deadline, status, timings)
            if rsids_l:
                jobs["dbsnp"] = self._timed("dbsnp", lambda: self._dbsnp(client, rsids_l), deadline, status, timings)
            if hgvs_l:
                jobs["ncbi_variation"] = self._timed(
                    "ncbi_variation", lambda: self._ncbi_variation(client, hgvs_l), deadline, status, timings
                )
            names = list(jobs)
            values = await asyncio.gather(*jobs.values())
            got: Dict[str, Any] = dict(zip(names, values))
        ot, chembl = got["opentargets"]

        hgnc = got.get("hgnc") or {}
        if hgnc.get("partial") and status.get("hgnc") == "ok":
            status["hgnc"] = "partial"
        entities = self._assemble(query, ot, hgnc, got.get("dbsnp") or {}, got.get("ncbi_variation") or {}, chembl,
                                  genes_l, codes_l, rsids_l, hgvs_l, status)
        timings["total"] = round((time.perf_counter() - t_start) * 1000, 1)
        return {
            "entities": [e.model_dump() for e in entities],
            "source_status": status,
            "timings_ms": timings,
        }

    def _assemble(
        self,
        query: str,
        ot: Dict[str, List[Dict[str, Any]]],
        hgnc: Dict[str, Any],
        snp: Dict[str, Dict[str, Any]],
        hgvs_valid: Dict[str, Optional[bool]],
        chembl: Dict[str, List[Dict[str, Any]]],
        gene_tokens: List[str],
        drug_codes: List[str],
        rsids: List[str],
        hgvs_list: List[str],
        status: Dict[str, str],
    ) -> List[ResolvedEntity]:
        genes: List[ResolvedEntity] = []
        seen_gene_terms = set()
        hgnc_genes = hgnc.get("genes") or {}
        hgnc_ok = status.get("hgnc") in ("ok", "partial")

        defined = defined_acronyms(query)
        for token in gene_tokens:
            span = _find_span(query, token)
            if span != (0, 0) and not is_gene_mention(query, *span):
                continue
            kind = defined.get(token.upper())
            if kind == "concept":
                continue  # defined in the question as a biomarker or endpoint ("... repair-deficient (HRD)")
            ot_target = self._accept_target(query, token, ot.get(token, [])) if token in ot else None
            info = hgnc_genes.get(token)
            if kind and info and (info.get("matched_on") in ("alias_symbol", "prev_symbol")):
                continue  # an acronym the question spells out is not an HGNC alias of some other gene
            ent: Optional[ResolvedEntity] = None
            if info is not None or family_members(token):
                ot_hit = ot_target if ot_target and "ambiguous" not in ot_target else None
                ent = self._gene_from_doc(query, token, info or {}, ot_hit)
            if ent is None and not (hgnc_ok and token in hgnc_genes) and ot_target:
                ent = self._ot_gene(query, token, ot_target)
            if ent is not None:
                genes.append(ent)
                seen_gene_terms.add(token)

        # Gene-shaped terms that were not sent to HGNC (over the token cap) may still resolve through OpenTargets.
        for term, hits in ot.items():
            if term in seen_gene_terms or term in gene_tokens:
                continue
            ot_target = self._accept_target(query, term, hits)
            if ot_target:
                ent = self._ot_gene(query, term, ot_target)
                if ent is not None:
                    genes.append(ent)
                    seen_gene_terms.add(term)

        drugs: List[ResolvedEntity] = []
        diseases: List[ResolvedEntity] = []
        # "ovarian and prostate cancers" was sent as "ovarian cancer" and "prostate cancer"; anchor them to the text.
        coordinated = {t: (mention, span) for t, mention, span in coordinated_disease_terms(query)}
        for term, hits in ot.items():
            if term in seen_gene_terms:
                continue
            if term in coordinated and _find_span(query, term) == (0, 0):
                disease = self._accept_coordinated_disease(term, hits)
                if disease:
                    mention, span = coordinated[term]
                    diseases.append(ResolvedEntity(
                        type="disease", mention=mention, span=span, label=str(disease.get("name") or term),
                        ids={"ontology_id": str(disease["id"])}, source="OpenTargets", matched_on="coordinated",
                        validated=True,
                    ))
                continue
            drug = self._accept_drug(query, term, hits)
            if drug:
                hit, matched_on = drug
                mention, span = _mention(query, term)
                drugs.append(ResolvedEntity(
                    type="drug", mention=mention, span=span, label=str(hit.get("name") or term),
                    ids={"chembl_id": str(hit["id"])}, source="OpenTargets", matched_on=matched_on, validated=True,
                ))
                continue
            disease = self._accept_disease(term, hits)
            if disease:
                mention, span = _mention(query, term)
                diseases.append(ResolvedEntity(
                    type="disease", mention=mention, span=span, label=str(disease.get("name") or term),
                    ids={"ontology_id": str(disease["id"])}, source="OpenTargets",
                    matched_on="name" if str(disease.get("name", "")).lower() == term.lower() else "term", validated=True,
                ))

        for code in drug_codes:
            mols = chembl.get(code) or []
            if len(mols) != 1 or any(code == d.mention or code.lower() == d.mention.lower() for d in drugs):
                continue
            mol = mols[0]
            mention, span = _mention(query, code)
            drugs.append(ResolvedEntity(
                type="drug", mention=mention, span=span, label=str(mol.get("pref_name") or code),
                ids={"chembl_id": str(mol["molecule_chembl_id"])}, source="ChEMBL", matched_on="synonym", validated=True,
            ))

        variants: List[ResolvedEntity] = []
        for rs in rsids:
            doc = snp.get(rs)
            if doc is None:
                continue
            mention, span = _mention(query, rs)
            ok = not doc.get("error")
            variants.append(ResolvedEntity(
                type="variant", mention=mention, span=span, label=rs, ids={"rsid": rs} if ok else {},
                source="dbSNP", matched_on="rsid", variant_kind="rsid",
                gene=(doc.get("genes") or [None])[0] if ok else None, validated=ok,
            ))
        manes = hgnc.get("manes") or {}
        for hgvs in hgvs_list:
            tx = hgvs.split(":", 1)[0]
            mane = manes.get(tx)
            valid = hgvs_valid.get(hgvs)
            if mane is None and valid is None:
                continue
            mention, span = _mention(query, hgvs)
            variants.append(ResolvedEntity(
                type="variant", mention=mention, span=span, label=hgvs,
                source="NCBI Variation" if valid else "HGNC", matched_on="mane_select" if mane else "hgvs",
                variant_kind="hgvs", gene=str(mane["symbol"]) if mane else None, validated=bool(valid),
            ))

        return self._dedupe(genes) + self._dedupe(drugs) + self._dedupe(diseases) + variants

    @staticmethod
    def _ot_gene(query: str, term: str, hit: Dict[str, Any]) -> Optional[ResolvedEntity]:
        mention, span = _mention(query, term)
        if "ambiguous" in hit:
            return ResolvedEntity(
                type="gene", mention=mention, span=span, label=mention, source="OpenTargets",
                matched_on="term", ambiguous_with=list(hit["ambiguous"])[:8], validated=False,
            )
        return ResolvedEntity(
            type="gene", mention=mention, span=span, label=str(hit.get("name") or term),
            ids={"ensembl_id": str(hit["id"])}, source="OpenTargets",
            matched_on="symbol" if str(hit.get("name", "")).upper() == term.upper() else "term", validated=True,
        )

    @staticmethod
    def _dedupe(items: List[ResolvedEntity]) -> List[ResolvedEntity]:
        """Within one entity type the longest mention wins ("lung adenocarcinoma" over "adenocarcinoma"); at the same
        span a coordinated expansion ("ovarian" -> ovarian cancer) wins over the bare word's own reading."""
        kept: List[ResolvedEntity] = []
        for e in sorted(items, key=lambda x: (-(x.span[1] - x.span[0]), x.span[0], x.matched_on != "coordinated")):
            if any(_contains(k.span, e.span) or (k.span == e.span and k.span != (0, 0)) for k in kept):
                continue
            if any(k.label == e.label and k.ids == e.ids for k in kept):
                continue
            kept.append(e)
        return sorted(kept, key=lambda x: x.span[0])


_DEFAULT_RESOLVER: Optional[EntityResolver] = None


def get_entity_resolver() -> EntityResolver:
    global _DEFAULT_RESOLVER
    if _DEFAULT_RESOLVER is None:
        _DEFAULT_RESOLVER = EntityResolver()
    return _DEFAULT_RESOLVER


__all__ = ["EntityResolver", "get_entity_resolver", "CODE_SHAPE_RE", "GENE_SHAPE_RE", "MAPIDS_QUERY"]
