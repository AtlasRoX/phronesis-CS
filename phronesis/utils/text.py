"""Small text helpers shared by the verifier and the orchestrator.

`sentence_spans` is the one sentence splitter (the verifier re-exports it); local splitters that differ on purpose
say why where they are defined.

`fold_typography` is the one table both sides use, so a sentence the verifier quotes (from folded text) can always be
found again in the answer the orchestrator holds (claim removal).
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# Unicode hyphens and minus signs the model writes ("Mos‑P", "on‑target", "−0.4") become "-"; the soft hyphen is
# dropped; typographic spaces (no-break, narrow no-break, thin, figure, hair) become a plain space. The en dash
# (U+2013) and em dash (U+2014) are kept: "0.23–0.41" is a range and means something different from "0.23-0.41".
TYPOGRAPHY_FOLD = str.maketrans({
    "‐": "-",  # hyphen
    "‑": "-",  # non-breaking hyphen
    "‒": "-",  # figure dash
    "−": "-",  # minus sign
    "­": "",   # soft hyphen
    " ": " ",  # no-break space
    " ": " ",  # narrow no-break space
    " ": " ",  # thin space
    " ": " ",  # figure space
    " ": " ",  # hair space
})


# A middle dot between digits is a decimal point in Lancet-style typesetting ("10·2 months"); elsewhere it separates
# items ("8 passages · 4k tokens") and is kept. One character for one, so fold_typography_with_map stays aligned.
_DECIMAL_DOTS = "·∙"
_DECIMAL_DOT_RE = re.compile(rf"(?<=\d)[{_DECIMAL_DOTS}](?=\d)")


def fold_typography(text: Optional[str]) -> str:
    """`text` with typographic hyphens and spaces folded to ASCII (see TYPOGRAPHY_FOLD) and middle-dot decimals
    ("10·2") read as "10.2". None or "" gives ""."""
    return _DECIMAL_DOT_RE.sub(".", text.translate(TYPOGRAPHY_FOLD)) if text else (text or "")


def fold_typography_with_map(text: str) -> Tuple[str, List[int]]:
    """(folded text, offsets): offsets[i] is the index in `text` of folded[i]; offsets[len(folded)] == len(text).

    The fold only replaces one character by one or drops a character, so every folded position maps back."""
    out: List[str] = []
    offsets: List[int] = []
    src = text or ""
    for i, ch in enumerate(src):
        rep = ch.translate(TYPOGRAPHY_FOLD)
        if ch in _DECIMAL_DOTS and 0 < i < len(src) - 1 and src[i - 1].isdigit() and src[i + 1].isdigit():
            rep = "."
        if not rep:
            continue
        out.append(rep)
        offsets.append(i)
    offsets.append(len(text or ""))
    return "".join(out), offsets


def find_folded(haystack: str, needle: str, start: int = 0) -> Tuple[int, int]:
    """(start, end) in `haystack` of the first match of `needle` at or after `start`, comparing both texts after
    fold_typography; (-1, -1) when absent. An exact match is tried first."""
    idx = haystack.find(needle, start)
    if idx >= 0:
        return idx, idx + len(needle)
    folded_needle = fold_typography(needle)
    if not folded_needle:
        return -1, -1
    folded, offsets = fold_typography_with_map(haystack)
    fstart = 0
    while fstart < len(offsets) - 1 and offsets[fstart] < start:
        fstart += 1
    fidx = folded.find(folded_needle, fstart)
    if fidx < 0:
        return -1, -1
    fend = fidx + len(folded_needle)
    # The original span ends just after the last matched character (dropped soft hyphens inside the span are kept
    # inside it; trailing ones are left outside).
    return offsets[fidx], offsets[fend - 1] + 1


# Abbreviations whose period never ends a sentence, and ones that do not when a number follows ("No. 5", "Fig. 2").
_ABBREVIATIONS = ("et al.", "e.g.", "i.e.", "vs.", "approx.", "cf.", "ca.", "resp.", "incl.", "viz.")
_NUMBER_ABBREVIATIONS = ("no.", "nos.", "fig.", "figs.", "ref.", "refs.", "vol.", "eq.", "tab.")
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_TABLE_LINE_RE = re.compile(r"^\s*\|")


def _is_abbreviation_break(text: str, pos: int, end: int) -> bool:
    """The period before `pos` belongs to an abbreviation, so the whitespace at pos..end is no sentence break."""
    tail = text[max(0, pos - 10):pos].lower()
    nxt = text[end:end + 1]
    for abbr in _ABBREVIATIONS + _NUMBER_ABBREVIATIONS:
        if not tail.endswith(abbr):
            continue
        before = tail[: len(tail) - len(abbr)]
        if before and before[-1].isalnum():
            continue
        if abbr in _NUMBER_ABBREVIATIONS and not nxt.isdigit():
            continue
        return True
    return False


def sentence_spans(text: str) -> List[Tuple[int, int]]:
    """(start, end) of each sentence: a break after . ! ? (not after an abbreviation such as "et al." or "e.g.")
    or at a newline. A markdown table row is always one span. ClinicalVerifier._split_sentences and the citation
    map both use this, so the sentences they compare line up. This is the project's one sentence splitter."""
    spans: List[Tuple[int, int]] = []
    start = 0
    line_start = 0
    for m in _SENTENCE_BREAK_RE.finditer(text):
        if "\n" in m.group(0):
            spans.append((start, m.start()))
            start = line_start = m.end()
            continue
        if _TABLE_LINE_RE.match(text, line_start) or _is_abbreviation_break(text, m.start(), m.end()):
            continue
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(text)))
    return [(s, e) for s, e in spans if text[s:e].strip()]
