"""
Markdown to typed blocks for the report renderers (owned by EXPORT, feature 9).

markdown-it-py with commonmark plus GFM tables and strikethrough; raw HTML is disabled, so HTML in an
answer is shown as text rather than interpreted.
"""

from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Set

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml


def _image_as_text(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    # fpdf2's write_html loads <img src> from local files and URLs; answers are client-supplied, so an
    # image is shown as its alt text and never fetched.
    alt = self.renderInlineAsText(tokens[idx].children or [], options, env) or "image"
    return f"<i>[{escapeHtml(alt)}]</i>"


@functools.lru_cache(maxsize=1)
def markdown_parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
    md.add_render_rule("image", _image_as_text)
    return md


def render_html(text: str) -> str:
    return markdown_parser().render(text or "")


@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    strike: bool = False
    link: Optional[str] = None


@dataclass
class Block:
    type: str  # heading | paragraph | table | code | hr
    level: int = 0
    runs: List[Run] = field(default_factory=list)
    list_kind: Optional[str] = None  # bullet | ordered (paragraph inside a list item)
    depth: int = 0  # list nesting depth, 1-based when list_kind is set
    first_in_item: bool = True
    quote: bool = False
    header: List[List[Run]] = field(default_factory=list)
    rows: List[List[List[Run]]] = field(default_factory=list)
    text: str = ""
    lang: str = ""


def inline_runs(token: Any) -> List[Run]:
    runs: List[Run] = []
    bold = italic = strike = False
    link: Optional[str] = None
    link_text_start = 0
    for ch in getattr(token, "children", None) or []:
        t = ch.type
        if t == "strong_open":
            bold = True
        elif t == "strong_close":
            bold = False
        elif t == "em_open":
            italic = True
        elif t == "em_close":
            italic = False
        elif t == "s_open":
            strike = True
        elif t == "s_close":
            strike = False
        elif t == "link_open":
            link = str(ch.attrs.get("href") or "") or None
            link_text_start = len(runs)
        elif t == "link_close":
            text = "".join(r.text for r in runs[link_text_start:])
            if link and text.strip() != link:
                runs.append(Run(text=f" ({link})", italic=italic, bold=bold))
            link = None
        elif t == "text":
            runs.append(Run(text=ch.content, bold=bold, italic=italic, strike=strike, link=link))
        elif t == "code_inline":
            runs.append(Run(text=ch.content, bold=bold, italic=italic, code=True, link=link))
        elif t in ("softbreak", "hardbreak"):
            runs.append(Run(text=" " if t == "softbreak" else "\n"))
        elif t == "image":
            alt = ch.content or "image"
            runs.append(Run(text=f"[{alt}]", italic=True))
    return runs


def parse_blocks(text: str) -> List[Block]:
    tokens = markdown_parser().parse(text or "")
    blocks: List[Block] = []
    lists: List[str] = []
    quote_depth = 0
    item_fresh: List[bool] = []
    table: Optional[Block] = None
    row: Optional[List[List[Run]]] = None
    in_head = False
    i = 0
    while i < len(tokens):
        t = tokens[i]
        tt = t.type
        if tt == "heading_open":
            blocks.append(Block(type="heading", level=int(t.tag[1]), runs=inline_runs(tokens[i + 1])))
            i += 3
            continue
        if tt in ("bullet_list_open", "ordered_list_open"):
            lists.append("ordered" if tt == "ordered_list_open" else "bullet")
        elif tt in ("bullet_list_close", "ordered_list_close"):
            if lists:
                lists.pop()
        elif tt == "list_item_open":
            item_fresh.append(True)
        elif tt == "list_item_close":
            if item_fresh:
                item_fresh.pop()
        elif tt == "blockquote_open":
            quote_depth += 1
        elif tt == "blockquote_close":
            quote_depth = max(0, quote_depth - 1)
        elif tt == "paragraph_open" and table is None:
            block = Block(type="paragraph", runs=inline_runs(tokens[i + 1]), quote=quote_depth > 0)
            if lists:
                block.list_kind = lists[-1]
                block.depth = len(lists)
                block.first_in_item = item_fresh[-1] if item_fresh else True
                if item_fresh:
                    item_fresh[-1] = False
            blocks.append(block)
            i += 3
            continue
        elif tt == "table_open":
            table = Block(type="table")
        elif tt == "thead_open":
            in_head = True
        elif tt == "thead_close":
            in_head = False
        elif tt == "tr_open":
            row = []
        elif tt == "tr_close" and table is not None and row is not None:
            if in_head and not table.header:
                table.header = row
            else:
                table.rows.append(row)
            row = None
        elif tt in ("th_open", "td_open") and row is not None:
            row.append(inline_runs(tokens[i + 1]))
            i += 3
            continue
        elif tt == "table_close" and table is not None:
            blocks.append(table)
            table = None
        elif tt in ("fence", "code_block"):
            blocks.append(Block(type="code", text=t.content.rstrip("\n"), lang=(t.info or "").strip()))
        elif tt == "hr":
            blocks.append(Block(type="hr"))
        i += 1
    return blocks


def runs_text(runs: Iterable[Run]) -> str:
    return "".join(r.text for r in runs)


# -----------------------------------------------------------------------------
# Glyph coverage for the PDF fonts
# -----------------------------------------------------------------------------

# Google Sans and Google Sans Code (SIL Open Font License 1.1, see fonts/OFL-*.txt), bundled so every export uses the
# same typeface as the web app on any server. DejaVu (shipped with matplotlib) is the fallback for the few glyphs
# Google Sans lacks.
REPORT_FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
REPORT_FONTS = {
    "regular": "GoogleSans-Regular.ttf",
    "medium": "GoogleSans-Medium.ttf",
    "bold": "GoogleSans-Bold.ttf",
    "italic": "GoogleSans-Italic.ttf",
    "bold_italic": "GoogleSans-BoldItalic.ttf",
    "mono": "GoogleSansCode-Regular.ttf",
    "mono_bold": "GoogleSansCode-Bold.ttf",
}


def report_font_path(key: str) -> Optional[str]:
    path = os.path.join(REPORT_FONT_DIR, REPORT_FONTS[key])
    return path if os.path.isfile(path) else None


def report_cmap() -> Optional[Set[int]]:
    """Code points the report fonts can draw: Google Sans plus the DejaVu fallback."""
    covered: Set[int] = set()
    regular = report_font_path("regular")
    try:
        if regular:
            covered |= font_cmap(regular)
    except Exception:  # noqa: BLE001
        pass
    covered |= default_cmap() or set()
    return covered or None


def dejavu_font_dir() -> Optional[str]:
    try:
        import matplotlib
    except ImportError:
        return None
    path = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf")
    return path if os.path.isfile(os.path.join(path, "DejaVuSans.ttf")) else None


@functools.lru_cache(maxsize=4)
def font_cmap(font_path: str) -> frozenset:
    from fontTools.ttLib import TTFont

    font = TTFont(font_path, lazy=True)
    try:
        return frozenset((font.getBestCmap() or {}).keys())
    finally:
        font.close()


def default_cmap() -> Optional[Set[int]]:
    folder = dejavu_font_dir()
    if not folder:
        return None
    try:
        return set(font_cmap(os.path.join(folder, "DejaVuSans.ttf")))
    except Exception:  # noqa: BLE001
        return None


_SPACE_BEFORE_CLOSE = re.compile(r"([(\[]) +| +([)\]])")


def strip_unsupported_glyphs(text: str, cmap: Optional[Iterable[int]] = None) -> str:
    """Drop characters the font cannot draw (astral emoji, variation selectors); keeps ⚠ ≥ ± µ × → and Greek."""
    covered = set(cmap) if cmap is not None else default_cmap()
    out = []
    for ch in text or "":
        cp = ord(ch)
        if cp < 128 or ch in "\n\t":
            out.append(ch)
        elif cp > 0xFFFF or cp in (0xFE0F, 0xFE0E, 0x200D):
            continue
        elif covered is None or cp in covered:
            out.append(ch)
    cleaned = "".join(out)
    return _SPACE_BEFORE_CLOSE.sub(lambda m: m.group(1) or m.group(2), cleaned)

