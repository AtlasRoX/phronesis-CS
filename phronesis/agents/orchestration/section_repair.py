"""Targeted section repair: rewrite only the answer sections a critique, a gap fill or a failed claim touches.

A whole-answer rewrite regenerates every section to change a few sentences; even with "copy the rest verbatim"
it is a full generation (time, tokens) and can drift untouched text. This module splits the Markdown answer into
sections, decides which ones the feedback affects, asks the model to rewrite only those (one bounded call per
section), validates each replacement and splices it back. Anything it cannot do safely returns a "whole" decision
so the caller keeps the whole-answer rewrite.

The model call is injected (`generate(system, user, max_tokens) -> text`) so the caller can wrap it with budget
admission and liveness watching. AnswerVersion keeps the answer, evidence and verification versions consistent:
a repair bumps the answer version and names the sections (and their claims) that need re-verification.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

GenerateFn = Callable[[str, str, int], Awaitable[str]]

# More than this share of sections affected: a targeted repair saves little, so the caller rewrites the whole answer.
WHOLE_FRACTION = 0.5
# A replacement must stay within this factor of the original length (unless a reason asks for removal).
LENGTH_TOLERANCE = 0.6
DEFAULT_SECTION_MAX_TOKENS = 1200

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_BOLD_HEADING_RE = re.compile(r"^\*\*([^*\n]{2,80})\*\*\s*:?\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SUMMARY_TITLE_RE = re.compile(r"\b(bottom\s+line|summary|key\s+(?:findings|points|takeaways)|tl;?dr|conclusions?|overview|answer)\b", re.I)

# Citation markers in answer text, normalised to evidence-key form ("PMID:123", "NCT:NCT01234567", ...).
_CITE_PATTERNS: Sequence[Tuple[str, re.Pattern]] = (
    ("PMID", re.compile(r"\bPMID[:\s]*?(\d{4,9})\b", re.I)),
    ("PMCID", re.compile(r"\b(PMC\d{4,9})\b")),
    ("DOI", re.compile(r"\b(10\.\d{4,9}/[^\s\)\]\(;,]+[^\s\)\]\(;,.])", re.I)),
    ("NCT", re.compile(r"\b(NCT\d{8})\b", re.I)),
    ("CHEMBL", re.compile(r"\b(CHEMBL\d+)\b", re.I)),
    ("URL", re.compile(r"\]\((https?://[^\s\)]+)\)")),
)


def citation_keys(text: str) -> Set[str]:
    """Evidence keys cited in a piece of answer text."""
    out: Set[str] = set()
    for kind, rx in _CITE_PATTERNS:
        for m in rx.finditer(text or ""):
            val = m.group(1)
            if kind in ("NCT", "CHEMBL", "PMCID"):
                val = val.upper()
            elif kind == "DOI":
                val = val.lower()
            out.add(f"{kind}:{val}")
    return out


def normalize_key(key: str) -> str:
    """An evidence key in the same form citation_keys() returns ("PMID: 123" -> "PMID:123", "nct:nct0.." upper)."""
    k = (key or "").strip()
    if ":" not in k:
        return k
    kind, _, val = k.partition(":")
    kind, val = kind.strip().upper(), val.strip()
    if kind in ("NCT", "CHEMBL", "PMCID"):
        val = val.upper()
    elif kind == "DOI":
        val = val.lower()
    return f"{kind}:{val}"


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`>#|]", " ", text or "")).strip().lower()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "section"


# Sections -----------------------------------------------------------------------------------------------------
@dataclass
class Section:
    id: str
    index: int
    heading: str  # the heading line as written ("" for the preamble)
    title: str  # heading text without markup ("" for the preamble)
    level: int  # 1-6 for "#" headings, 7 for a bold-line heading, 0 for the preamble
    text: str  # the whole section, heading included, exactly as in the answer (trailing newlines kept)

    @property
    def body(self) -> str:
        return self.text[len(self.heading):] if self.heading and self.text.startswith(self.heading) else self.text

    @property
    def is_summary(self) -> bool:
        if self.level == 0:
            return True  # the opening block before the first heading carries the bottom line
        return bool(_SUMMARY_TITLE_RE.search(self.title))

    @property
    def hash(self) -> str:
        return _hash(self.text)

    def cites(self) -> Set[str]:
        return citation_keys(self.text)


def _heading(line: str) -> Optional[Tuple[int, str]]:
    m = _HEADING_RE.match(line)
    if m:
        return len(m.group(1)), m.group(2).strip()
    m = _BOLD_HEADING_RE.match(line.strip())
    if m and not line.startswith(("|", "-", "*  ")):
        return 7, m.group(1).strip()
    return None


def split_sections(markdown: str) -> List[Section]:
    """Split an answer into sections at headings ("#".."######" or a line that is only bold text). The text
    before the first heading is a preamble section. Lines inside code fences or tables never start a section,
    and joining every section's text reproduces the input exactly."""
    text = markdown or ""
    lines = text.splitlines(keepends=True)
    starts: List[Tuple[int, str, str, int]] = []  # (line index, heading line, title, level)
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith("|"):
            continue
        h = _heading(line.rstrip("\r\n"))
        if h is not None:
            starts.append((i, line.rstrip("\r\n"), h[1], h[0]))
    bounds: List[Tuple[int, int, str, str, int]] = []
    if not starts or starts[0][0] > 0:
        bounds.append((0, starts[0][0] if starts else len(lines), "", "", 0))
    for n, (i, line, title, level) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        bounds.append((i, end, line, title, level))
    sections: List[Section] = []
    seen: Dict[str, int] = {}
    for idx, (a, b, line, title, level) in enumerate(bounds):
        chunk = "".join(lines[a:b])
        if level == 0 and not chunk.strip() and len(bounds) > 1:
            # Leading blank lines: keep them attached to the next section so the join still reproduces the text.
            continue
        base = "preamble" if level == 0 else _slug(title)
        seen[base] = seen.get(base, 0) + 1
        sid = base if seen[base] == 1 else f"{base}-{seen[base]}"
        sections.append(Section(id=sid, index=len(sections), heading=line, title=title, level=level, text=chunk))
    # Re-attach skipped leading whitespace so "".join(...) == markdown.
    joined = "".join(s.text for s in sections)
    if sections and joined != text and text.endswith(joined):
        lead = text[: len(text) - len(joined)]
        first = sections[0]
        sections[0] = Section(first.id, first.index, first.heading, first.title, first.level, lead + first.text)
    return sections


def join_sections(sections: Iterable[Section]) -> str:
    return "".join(s.text for s in sections)


# Which sections a repair touches --------------------------------------------------------------------------------
@dataclass
class RepairDecision:
    mode: str  # "targeted" | "whole" | "none"
    sections: Dict[str, List[str]] = field(default_factory=dict)  # section id -> reasons
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "sections": dict(self.sections), "reason": self.reason}


_QUOTE_RE = re.compile(r"[\"“”]([^\"“”]{12,400})[\"“”]")


def _quoted_fragments(item: str) -> List[str]:
    return [m.group(1).strip() for m in _QUOTE_RE.finditer(item or "") if len(m.group(1).split()) >= 3]


def _feedback_keys(item: str) -> Set[str]:
    keys = citation_keys(item)
    for m in re.finditer(r"\(evidence:\s*([^;)]+)", item or ""):
        keys.add(normalize_key(m.group(1)))
    return keys


def _fragment_in(fragment: str, section_text: str) -> bool:
    frag = _norm(fragment)
    if not frag:
        return False
    sec = _norm(section_text)
    if frag in sec:
        return True
    # A critic paraphrases the start of the sentence it quotes: 6 consecutive words are enough.
    words = frag.split()
    if len(words) >= 6:
        for i in range(0, len(words) - 5):
            if " ".join(words[i:i + 6]) in sec:
                return True
    return False


def affected_sections(
    sections: Sequence[Section],
    feedback_items: Sequence[str] = (),
    new_evidence_keys: Iterable[str] = (),
    unsupported_claims: Sequence[str] = (),
    *,
    whole_fraction: float = WHOLE_FRACTION,
) -> RepairDecision:
    """Which sections need repair and why.

    A section is affected when it contains a sentence a feedback item quotes, cites a key a feedback item names or
    whose evidence changed, or contains an unsupported/contradicted claim. The summary (preamble or "Bottom line")
    is added whenever another section changes, since it restates them. Feedback that names no section (for
    example "use the new gap-search documents") cannot be placed: the decision is "whole". So is a repair that
    touches more than whole_fraction of the sections, or an answer with fewer than two sections."""
    if len(sections) < 2:
        return RepairDecision("whole", reason="the answer has no section structure to repair in place")
    changed_keys = {normalize_key(k) for k in new_evidence_keys if k}
    picks: Dict[str, List[str]] = {}

    def add(sec: Section, why: str) -> None:
        reasons = picks.setdefault(sec.id, [])
        if why not in reasons:
            reasons.append(why)

    unplaced: List[str] = []
    for item in feedback_items:
        placed = False
        for frag in _quoted_fragments(item):
            for sec in sections:
                if _fragment_in(frag, sec.text):
                    add(sec, f"critique: {item[:300]}")
                    placed = True
        keys = _feedback_keys(item)
        if keys:
            for sec in sections:
                if sec.cites() & keys:
                    add(sec, f"critique on cited evidence: {item[:300]}")
                    placed = True
        if not placed:
            unplaced.append(item)
    for sec in sections:
        hit = sec.cites() & changed_keys
        if hit:
            add(sec, "evidence changed for " + ", ".join(sorted(hit)[:5]))
    for claim in unsupported_claims:
        for sec in sections:
            if _fragment_in(claim, sec.text):
                add(sec, f"unsupported claim: {claim[:300]}")
    if unplaced:
        return RepairDecision(
            "whole", reason=f"{len(unplaced)} feedback item(s) name no section of the answer: {unplaced[0][:200]}"
        )
    if not picks:
        return RepairDecision("none", reason="no section is affected")
    non_summary = [s for s in sections if s.id in picks and not s.is_summary]
    if non_summary:
        for sec in sections:
            if sec.is_summary and sec.id not in picks:
                add(sec, "summary restates a section that changed")
    if len(picks) > whole_fraction * len(sections):
        return RepairDecision(
            "whole", dict(picks), reason=f"{len(picks)} of {len(sections)} sections are affected; rewriting the whole answer"
        )
    ordered = {s.id: picks[s.id] for s in sections if s.id in picks}
    return RepairDecision("targeted", ordered, reason=f"{len(ordered)} of {len(sections)} section(s) need repair")


# Prompts ------------------------------------------------------------------------------------------------------
SECTION_SYSTEM = (
    "You repair one section of a cited biomedical research answer. Rewrite only the section given in "
    "<section>, changing only what the listed reasons require; keep every other sentence, table row and citation "
    "as written. Keep the heading line exactly. Cite only identifiers that appear in <evidence> or already in the "
    "section, in the same format (for example \"(PMID: 12345678)\", NCT01234567, [Title](url)). If the evidence "
    "cannot support a statement, qualify or remove it rather than inventing support. Return only the rewritten "
    "section in Markdown: no preamble, no other sections, no code fences."
)


def build_section_prompt(
    section: Section, reasons: Sequence[str], evidence_block_excerpt: str = "", neighbors_summary: str = ""
) -> Tuple[str, str]:
    """(system, user) for rewriting one section."""
    parts = ["<reasons>"] + [f"- {r}" for r in reasons] + ["</reasons>"]
    if neighbors_summary:
        parts += ["<other_sections note=\"context only; do not rewrite\">", neighbors_summary.strip(), "</other_sections>"]
    if evidence_block_excerpt:
        parts += ["<evidence>", evidence_block_excerpt.strip(), "</evidence>"]
    parts += ["<section>", section.text.strip("\n"), "</section>"]
    if section.heading:
        parts.append(f"Return the section starting with this exact heading line: {section.heading}")
    else:
        parts.append("This is the opening block of the answer (no heading); return it without adding a heading.")
    return SECTION_SYSTEM, "\n".join(parts)


def neighbors_summary(sections: Sequence[Section], skip_id: str, limit: int = 1200) -> str:
    """Headings and first sentences of the other sections, so a section rewrite stays consistent."""
    lines: List[str] = []
    for s in sections:
        if s.id == skip_id:
            continue
        first = re.split(r"(?<=[.!?])\s", _norm_spaces(s.body).strip(), maxsplit=1)[0][:200]
        lines.append(f"- {s.title or 'Opening'}: {first}")
    out = "\n".join(lines)
    return out[:limit]


def _norm_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "")


# Splicing -----------------------------------------------------------------------------------------------------
@dataclass
class SpliceResult:
    markdown: str
    applied: List[str] = field(default_factory=list)
    rejected: Dict[str, str] = field(default_factory=dict)  # section id -> why the original was kept


_FENCE_WRAP_RE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n```\s*$", re.S)


def _clean_reply(text: str) -> str:
    t = (text or "").strip("﻿")
    m = _FENCE_WRAP_RE.match(t)
    if m:
        t = m.group(1)
    t = re.sub(r"^\s*</?section>\s*$", "", t, flags=re.M)
    return t.strip("\n")


def validate_replacement(
    original: Section,
    replacement: str,
    sections: Sequence[Section],
    allowed_keys: Optional[Iterable[str]] = None,
    reasons: Sequence[str] = (),
) -> Tuple[Optional[str], str]:
    """(text to splice, "") or (None, why it was rejected)."""
    rep = _clean_reply(replacement)
    if not rep.strip():
        return None, "empty replacement"
    first_line = rep.splitlines()[0].rstrip()
    if original.heading:
        if first_line.strip() != original.heading.strip():
            # Accept a model that dropped the heading line; restore it. Reject one that changed it.
            if _heading(first_line) is not None:
                return None, "the heading was changed"
            rep = original.heading + "\n" + rep
    elif _heading(first_line) is not None:
        return None, "a heading was added to the opening block"
    others = {_norm(s.title) for s in sections if s.id != original.id and s.title}
    for line in rep.splitlines()[1 if original.heading else 0:]:
        h = _heading(line.rstrip())
        # No duplicate of another section's title, and nothing at this section's level or above (a sub-heading
        # inside a "##" section is fine); the opening block gets no heading at all.
        ceiling = 6 if original.level in (0, 7) else original.level
        if h is not None and (_norm(h[1]) in others or h[0] <= ceiling):
            return None, f"the replacement adds another section heading ({h[1][:60]})"
    body_len = len(_norm(original.body)) or 1
    new_body = rep[len(original.heading):] if original.heading and rep.startswith(original.heading) else rep
    new_len = len(_norm(new_body))
    removal = any(r.startswith("unsupported claim") for r in reasons)
    lo = 0.0 if removal else (1 - LENGTH_TOLERANCE) * body_len
    hi = (1 + LENGTH_TOLERANCE) * body_len + 200  # short sections may gain a qualifying sentence
    if new_len < lo or new_len > hi:
        return None, f"length changed too much ({new_len} vs {body_len} characters)"
    if allowed_keys is not None:
        allowed = {normalize_key(k) for k in allowed_keys} | original.cites()
        extra = citation_keys(rep) - allowed
        if extra:
            return None, "cites identifiers outside the evidence: " + ", ".join(sorted(extra)[:5])
    # Keep the original's trailing whitespace so the section boundary (blank line before the next heading) holds.
    trail = original.text[len(original.text.rstrip("\n")):]
    return rep.rstrip("\n") + (trail or "\n"), ""


def splice(
    sections: Sequence[Section],
    replacements: Dict[str, str],
    allowed_keys: Optional[Iterable[str]] = None,
    reasons: Optional[Mapping[str, Sequence[str]]] = None,
) -> SpliceResult:
    """The answer with each valid replacement spliced in; invalid ones keep the original section."""
    allowed = list(allowed_keys) if allowed_keys is not None else None
    out: List[str] = []
    result = SpliceResult(markdown="")
    for sec in sections:
        if sec.id not in replacements:
            out.append(sec.text)
            continue
        text, why = validate_replacement(sec, replacements[sec.id], sections, allowed, (reasons or {}).get(sec.id, ()))
        if text is None:
            result.rejected[sec.id] = why
            out.append(sec.text)
        else:
            result.applied.append(sec.id)
            out.append(text)
    result.markdown = "".join(out)
    return result


# Versions -----------------------------------------------------------------------------------------------------
def evidence_version_of(keys: Iterable[str]) -> str:
    """A stable version id for an evidence set (order-free hash of its keys)."""
    return _hash("\n".join(sorted({normalize_key(k) for k in keys if k})))


@dataclass
class AnswerVersion:
    """Answer, evidence and verification versions for one turn. Every text change bumps answer_version; the
    verification it was checked against is stale until verified_version catches up."""

    answer_version: int = 1
    evidence_version: str = ""
    verified_version: int = 0
    section_hashes: Dict[str, str] = field(default_factory=dict)
    log: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def start(cls, markdown: str, evidence_keys: Iterable[str] = ()) -> "AnswerVersion":
        v = cls(evidence_version=evidence_version_of(evidence_keys))
        v.section_hashes = {s.id: s.hash for s in split_sections(markdown)}
        return v

    def bump_evidence(self, evidence_keys: Iterable[str]) -> bool:
        """Record a new evidence set; True when it changed (the answer then needs re-verification)."""
        new = evidence_version_of(evidence_keys)
        changed = new != self.evidence_version
        self.evidence_version = new
        return changed

    def record_repair(self, before: str, after: str, mode: str, reasons: Dict[str, List[str]]) -> Dict[str, Any]:
        """Log a repair. Returns the log entry, including which sections changed and need re-verification."""
        old_hashes = dict(self.section_hashes)
        new_sections = split_sections(after)
        new_hashes = {s.id: s.hash for s in new_sections}
        changed = [sid for sid, h in new_hashes.items() if old_hashes.get(sid) != h]
        removed = [sid for sid in old_hashes if sid not in new_hashes]
        entry = {
            "from_version": self.answer_version,
            "to_version": self.answer_version + (1 if before != after else 0),
            "mode": mode,
            "evidence_version": self.evidence_version,
            "sections_changed": changed,
            "sections_removed": removed,
            "reasons": {k: list(v) for k, v in reasons.items()},
            "before_hash": _hash(before),
            "after_hash": _hash(after),
        }
        if before != after:
            self.answer_version += 1
        self.section_hashes = new_hashes
        self.log.append(entry)
        return entry

    def mark_verified(self) -> None:
        self.verified_version = self.answer_version

    @property
    def verification_stale(self) -> bool:
        return self.verified_version != self.answer_version

    def to_dict(self) -> Dict[str, Any]:
        return {
            "answer_version": self.answer_version,
            "evidence_version": self.evidence_version,
            "verified_version": self.verified_version,
            "verification_stale": self.verification_stale,
            "repairs": list(self.log),
        }


def claims_to_reverify(markdown: str, section_ids: Iterable[str], claims: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Claim records (verifier dicts with claim_text) that sit in the given sections of the answer."""
    wanted = set(section_ids)
    texts = [s.text for s in split_sections(markdown) if s.id in wanted]
    return [c for c in claims if any(_fragment_in(str(c.get("claim_text") or ""), t) for t in texts)]


# Orchestration ------------------------------------------------------------------------------------------------
@dataclass
class RepairReport:
    mode: str  # "targeted" | "whole" | "none"
    reason: str = ""
    sections: Dict[str, List[str]] = field(default_factory=dict)
    applied: List[str] = field(default_factory=list)
    rejected: Dict[str, str] = field(default_factory=dict)
    failed: Dict[str, str] = field(default_factory=dict)  # section id -> generation error
    calls: int = 0
    version: Optional[Dict[str, Any]] = None

    @property
    def changed(self) -> bool:
        return bool(self.applied)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode, "reason": self.reason, "sections": dict(self.sections), "applied": list(self.applied),
            "rejected": dict(self.rejected), "failed": dict(self.failed), "calls": self.calls, "version": self.version,
        }


async def repair_sections(
    markdown: str,
    feedback: Sequence[str],
    generate: GenerateFn,
    *,
    new_evidence_keys: Iterable[str] = (),
    unsupported_claims: Sequence[str] = (),
    allowed_keys: Optional[Iterable[str]] = None,
    evidence_excerpt: str = "",
    max_parallel: int = 2,
    per_section_max_tokens: int = DEFAULT_SECTION_MAX_TOKENS,
    whole_fraction: float = WHOLE_FRACTION,
    version: Optional[AnswerVersion] = None,
) -> Tuple[str, RepairReport]:
    """Repair the affected sections of an answer in place.

    Returns (markdown, report). report.mode is "whole" when the caller should fall back to its whole-answer
    rewrite (markdown is then returned unchanged), "none" when nothing needs repair, else "targeted". A section
    whose generation fails or whose replacement is invalid keeps its original text (report.failed / rejected).
    generate exceptions other than asyncio.CancelledError are caught per section; cancellation propagates."""
    sections = split_sections(markdown)
    decision = affected_sections(
        sections, feedback, new_evidence_keys, unsupported_claims, whole_fraction=whole_fraction
    )
    report = RepairReport(mode=decision.mode, reason=decision.reason, sections=dict(decision.sections))
    if decision.mode != "targeted":
        return markdown, report
    by_id = {s.id: s for s in sections}
    sem = asyncio.Semaphore(max(1, int(max_parallel)))
    replies: Dict[str, str] = {}

    async def one(sid: str, reasons: List[str]) -> None:
        sec = by_id[sid]
        system, user = build_section_prompt(sec, reasons, evidence_excerpt, neighbors_summary(sections, sid))
        async with sem:
            report.calls += 1
            try:
                replies[sid] = await generate(system, user, int(per_section_max_tokens))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one failed section keeps its text, the others still repair
                report.failed[sid] = f"{type(exc).__name__}: {str(exc)[:200]}"

    # Repair the non-summary sections first, then the summary with their new text as context.
    order = list(decision.sections.items())
    body = [(sid, r) for sid, r in order if not by_id[sid].is_summary]
    summary = [(sid, r) for sid, r in order if by_id[sid].is_summary]
    await asyncio.gather(*(one(sid, r) for sid, r in body))
    if summary:
        interim = splice(sections, replies, allowed_keys, decision.sections)
        sections_after = split_sections(interim.markdown)
        sections_by_id = {s.id: s for s in sections_after}
        for sid, reasons in summary:
            sec = sections_by_id.get(sid) or by_id[sid]
            system, user = build_section_prompt(sec, reasons, evidence_excerpt, neighbors_summary(sections_after, sid))
            async with sem:
                report.calls += 1
                try:
                    replies[sid] = await generate(system, user, int(per_section_max_tokens))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    report.failed[sid] = f"{type(exc).__name__}: {str(exc)[:200]}"
    result = splice(sections, replies, allowed_keys, decision.sections)
    report.applied, report.rejected = result.applied, result.rejected
    if version is not None:
        report.version = version.record_repair(markdown, result.markdown, "targeted", decision.sections)
    return result.markdown, report


__all__ = [
    "AnswerVersion",
    "DEFAULT_SECTION_MAX_TOKENS",
    "RepairDecision",
    "RepairReport",
    "Section",
    "SpliceResult",
    "affected_sections",
    "build_section_prompt",
    "citation_keys",
    "claims_to_reverify",
    "evidence_version_of",
    "join_sections",
    "neighbors_summary",
    "normalize_key",
    "repair_sections",
    "splice",
    "split_sections",
    "validate_replacement",
]
