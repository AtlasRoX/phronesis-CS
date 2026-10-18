"""Who published a source, from the record's own URL fields: one policy for prompts, citations, the verifier and exports.

The publisher is read from a URL the retrieval recorded for the document itself (its final URL after redirects, else
its canonical or requested URL), never from text inside the document. A third-party page that links an fda.gov URL is
not an FDA record; neither is a lookalike host (``www.fda.gov.example.test``) or a URL whose userinfo names FDA
(``https://fda.gov@evil.test``). Origin only says who published a page; whether it supports a claim is checked
separately.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit

REGULATORY_HOSTS = (
    "fda.gov", "ema.europa.eu", "dailymed.nlm.nih.gov", "mhra.gov.uk", "pmda.go.jp", "tga.gov.au",
    "swissmedic.ch",
)
FDA_HOSTS = ("fda.gov",)  # www.fda.gov, accessdata.fda.gov, ...
REGISTRY_HOSTS = ("clinicaltrials.gov", "clinicaltrialsregister.eu", "euclinicaltrials.eu", "isrctn.com",
                  "trialsearch.who.int", "anzctr.org.au", "chictr.org.cn")
MANUFACTURER_HOSTS = (
    "prnewswire.com", "businesswire.com", "globenewswire.com", "accesswire.com", "amgen.com", "bms.com",
    "mirati.com", "pfizer.com", "novartis.com", "roche.com", "gene.com", "astrazeneca.com", "lilly.com",
    "merck.com", "msd.com", "gsk.com", "sanofi.com", "abbvie.com", "jnj.com", "bayer.com", "takeda.com",
    "boehringer-ingelheim.com", "regeneron.com", "gilead.com", "biogen.com", "daiichisankyo.com", "astellas.com",
    "eisai.com", "novonordisk.com", "servier.com", "ipsen.com", "beigene.com", "beonemed.com", "revmed.com",
    "jacobiopharma.com", "innocarepharma.com", "hengrui.com", "boehringer-ingelheim.us",
)
LITERATURE_HOSTS = (
    "ncbi.nlm.nih.gov", "europepmc.org", "doi.org", "nejm.org", "thelancet.com", "nature.com", "sciencedirect.com",
    "springer.com", "wiley.com", "ascopubs.org", "aacrjournals.org", "jamanetwork.com", "bmj.com", "cell.com",
    "biorxiv.org", "medrxiv.org", "academic.oup.com", "frontiersin.org", "mdpi.com", "tandfonline.com",
    "annalsofoncology.org", "jto.org", "esmoopen.com", "sagepub.com", "karger.com", "plos.org",
)
SOURCE_AUTHORITY_LABELS = {
    "regulatory": "regulator (official approval or label record)",
    "trial_registry": "trial registry record",
    "manufacturer": "manufacturer or press-wire announcement (not a regulator statement)",
    "literature": "journal or literature database",
    "web": "other web page",
}

# Record fields that hold the document's own address, most authoritative first: where the fetch ended after
# redirects, then the canonical URL the page declared, then the URL that was requested or listed by a search engine.
URL_FIELDS = ("final_url", "canonical_url", "url", "link", "source_url")
_HOST_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def host_of(url: Any) -> Optional[str]:
    """The validated, lowercased DNS host of an http(s) URL, or None. Rejects userinfo, IP literals, empty or
    malformed labels and non-web schemes; a trailing root dot is dropped."""
    text = str(url or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    if "@" in (parts.netloc or ""):
        return None  # "https://fda.gov@evil.test": the host is evil.test, and the URL is built to mislead
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2 or not all(_HOST_LABEL_RE.match(label) for label in labels):
        return None
    return host


def _on(host: Optional[str], domains: Iterable[str]) -> bool:
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)  # type: ignore[union-attr]


def source_authority(url: Any) -> str:
    """'regulatory' | 'trial_registry' | 'manufacturer' | 'literature' | 'web' for a page URL, by its host."""
    host = host_of(url)
    if _on(host, REGULATORY_HOSTS):
        return "regulatory"
    if _on(host, REGISTRY_HOSTS):
        return "trial_registry"
    if _on(host, MANUFACTURER_HOSTS):
        return "manufacturer"
    if _on(host, LITERATURE_HOSTS):
        return "literature"
    return "web"


def is_fda_url(url: Any) -> bool:
    """fda.gov itself or one of its subdomains (www.fda.gov, accessdata.fda.gov)."""
    return _on(host_of(url), FDA_HOSTS)


def is_regulatory_url(url: Any) -> bool:
    return _on(host_of(url), REGULATORY_HOSTS)


def record_url(record: Any) -> Optional[str]:
    """The document's own URL from a retrieval record: final URL after redirects, else canonical, else requested.

    A record whose redirects were recorded (``redirects``: the hops in order) is judged by its last hop. Text fields
    (snippets, page content) are never read: a URL mentioned inside a page is a citation, not the page's origin."""
    if not isinstance(record, Mapping):
        return None
    hops = record.get("redirects")
    if isinstance(hops, (list, tuple)) and hops and isinstance(hops[-1], str) and hops[-1].strip():
        return hops[-1].strip()
    for field in URL_FIELDS:
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


__all__ = [
    "FDA_HOSTS",
    "LITERATURE_HOSTS",
    "MANUFACTURER_HOSTS",
    "REGISTRY_HOSTS",
    "REGULATORY_HOSTS",
    "SOURCE_AUTHORITY_LABELS",
    "URL_FIELDS",
    "host_of",
    "is_fda_url",
    "is_regulatory_url",
    "record_url",
    "source_authority",
]
