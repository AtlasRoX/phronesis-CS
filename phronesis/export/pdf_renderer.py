"""
PDF renderer for the investigation report (owned by EXPORT, feature 9).

Google Sans and Google Sans Code (bundled under fonts/, SIL OFL) with DejaVu as the per-glyph fallback, so the PDF
uses the web app's typeface. The title block, section headings and rules are drawn directly; body content is
fpdf2's write_html on markdown-it HTML with explicit line heights and paragraph spacing. Characters no font can
draw, such as astral-plane emoji, are stripped first.
"""

from __future__ import annotations

import html
import io
import logging
import os
import re
from typing import Any, List, Optional, Tuple

from .markdown_blocks import dejavu_font_dir, render_html, report_cmap, report_font_path, strip_unsupported_glyphs
from .report_model import (
    ReportCitation, ReportModel, citation_parts, fmt_number, question_is_title, report_headline, report_kind,
    task_lines, verification_lines,
)

_ASSETS = os.path.join(os.path.dirname(__file__), "assets")
BRAND_MARK = os.path.join(_ASSETS, "phronesis-mark.png")
BRAND_WORDMARK = os.path.join(_ASSETS, "phronesis-wordmark.png")

# fpdf2 subsets fonts through fontTools, which logs every glyph table at INFO.
logging.getLogger("fontTools").setLevel(logging.WARNING)

_SANS = "GoogleSans"
_SANS_MEDIUM = "GoogleSansMedium"
_MONO = "GoogleSansCode"
_FALLBACK = "DejaVu"

# Palette: ink for text, navy for headings, muted greys for labels and rules, a tint for table headers.
_INK = (32, 33, 36)
_NAVY = (24, 30, 72)
_MUTED = (95, 99, 104)
_FAINT = (128, 134, 139)
_RULE = (218, 220, 224)
_TINT = "#F1F3F8"
_AMBER = (146, 84, 0)

_BODY_PT = 10
_LINE = 1.4  # body line height, as a multiple of the font size
_MARGIN_X, _MARGIN_TOP, _MARGIN_BOTTOM = 20, 18, 20


class FontsUnavailable(ImportError):
    pass


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _table_html(
    header: List[str], rows: List[List[str]], widths: Optional[List[int]] = None, mono: Tuple[int, ...] = ()
) -> str:
    """mono: columns of identifiers and JSON, set in Google Sans Code so they stay compact and break cleanly.
    Text is left-aligned (fpdf2 justifies cells by default); columns of numbers are right-aligned."""
    width_attr = lambda i: f' width="{widths[i]}%"' if widths and i < len(widths) else ""  # noqa: E731
    numeric = {
        i for i in range(len(header))
        if i not in mono and _numeric_column([r[i] if i < len(r) else "" for r in rows])
    }

    def cell(i: int, value: str) -> str:
        text = _esc(value) if str(value or "").strip() else "—"
        align = "right" if i in numeric else "left"
        if i in mono:
            return f'<td align="{align}"><font face="{_MONO}" size="7">{text}</font></td>'
        return f'<td align="{align}">{text}</td>'

    head = "".join(
        f'<th align="{"right" if i in numeric else "left"}"{width_attr(i)}>{_esc(h)}</th>' for i, h in enumerate(header)
    )
    body = "".join("<tr>" + "".join(cell(i, c) for i, c in enumerate(row)) + "</tr>" for row in rows)
    return f'{_TABLE_OPEN}<thead><tr bgcolor="{_TINT}">{head}</tr></thead><tbody>{body}</tbody></table>'


_TABLE_OPEN = '<table border="0" cellpadding="1.2" width="100%">'
_NUMBER = re.compile(r"^[<>≤≥~]?\s*[-+]?(\d+(\.\d+)?|\.\d+)([eE][-+]?\d+)?\s*%?$")


def _numeric_column(values: List[str]) -> bool:
    """Every filled cell is a number (an all-empty column aligns with the numbers beside it)."""
    filled = [v.strip() for v in values if v and v.strip() and v.strip() != "—"]
    return all(_NUMBER.match(v) for v in filled)
# fpdf2 gives tables no top margin; a short spacer keeps them off the paragraph or list above.
_TABLE_SPACER = '<p line-height="0.6">&nbsp;</p>'


def _spaced(markup: str) -> str:
    """Body line height on paragraphs and lists (fpdf2 reads a line-height attribute; markdown-it emits none), and
    the report's table look on tables from the answer's markdown."""
    markup = re.sub(r"<p>", f'<p line-height="{_LINE}">', markup)
    markup = re.sub(r"<(ul|ol)(\s[^>]*)?>", lambda m: f'<{m.group(1)}{m.group(2) or ""} line-height="{_LINE}">', markup)
    markup = markup.replace("<table>", _TABLE_SPACER + _TABLE_OPEN)
    markup = re.sub(r"<thead>(\s*)<tr>", lambda m: f'<thead>{m.group(1)}<tr bgcolor="{_TINT}">', markup)
    return re.sub(r"<th(?=[\s>])(?![^>]*\balign=)", '<th align="left"', markup)


def _items(lines: List[str]) -> str:
    """A bullet list; lines indented by two spaces nest under the line before them."""
    out, nested = [f'<ul line-height="{_LINE}">'], False
    for raw in lines:
        child = raw.startswith("  ")
        text = raw.strip()
        text = text[2:] if text.startswith("- ") else text
        if child and not nested:
            out[-1] = out[-1].removesuffix("</li>")
            out.append(f'<ul line-height="{_LINE}">')
            nested = True
        elif not child and nested:
            out.append("</ul></li>")
            nested = False
        out.append(f"<li>{_esc(text)}</li>")
    if nested:
        out.append("</ul></li>")
    out.append("</ul>")
    return "".join(out)


# A part is ("heading", title), ("html", markup), ("table", markup) or ("plot", None).
Part = Tuple[str, Any]


def _parts(report: ReportModel, *, question_in_title: bool) -> List[Part]:
    parts: List[Part] = []
    for notice in report.notices:
        parts.append(("html", f"<blockquote><b>Notice.</b> {_esc(notice)}</blockquote>"))
    if not question_in_title:
        parts.append(("heading", "Question"))
        parts.append(("html", _spaced(f"<p>{_esc(report.question or 'No question was supplied.')}</p>")))
    if report.task:
        parts.append(("heading", "Task status"))
        parts.append(("html", _items(task_lines(report.task))))
    parts.append(("heading", "Answer"))
    parts.append(("html", _spaced(render_html(_demote_headings(report.answer_markdown or "_No answer text._")))))

    sim = report.simulation
    if sim and (sim.rows or report.plot_png):
        parts.append(("heading", "Simulation"))
        label = " ".join(x for x in (sim.model_id, f"({sim.model_name})" if sim.model_name else "") if x)
        line = f"Model: {label}" if label else ""
        if sim.duration is not None:
            line += f", duration {fmt_number(sim.duration)} {sim.time_unit}".rstrip()
        if line:
            parts.append(("html", _spaced(f"<p>{_esc(line)}</p>")))
        if report.plot_png:
            parts.append(("plot", None))
        if sim.rows:
            unit = f" ({sim.quantity_unit})" if sim.quantity_unit else ""
            has_ss = any(r.steady_state is not None for r in sim.rows)
            header = ["Species", f"Initial{unit}", f"Final{unit}"] + ([f"Steady state{unit}"] if has_ss else [])
            rows = [
                [r.species, fmt_number(r.initial), fmt_number(r.final)] + ([fmt_number(r.steady_state)] if has_ss else [])
                for r in sim.rows
            ]
            parts.append(("table", _table_html(header, rows)))

    for table in report.data_tables:
        parts.append(("heading", table.title))
        parts.append(("table", _table_html(table.header, table.rows)))
        if table.note:
            parts.append(("html", _spaced(f"<p><i>{_esc(table.note)}</i></p>")))

    parts.append(("heading", "Sources and citations"))
    if report.citations:
        items = "".join(f"<li>{_citation_html(c)}</li>" for c in report.citations)
        parts.append(("html", f'<ol line-height="{_LINE}">{items}</ol>'))
    else:
        parts.append(("html", _spaced("<p>No structured evidence records accompany this answer.</p>")))

    parts.append(("heading", "Verification"))
    parts.append(("html", _items(verification_lines(report.verification))))
    parts.append(("heading", "Evidence limitations"))
    parts.append(("html", _items(report.limitations or ["No limitations were recorded."])))
    parts.append(("heading", "Run metadata"))
    parts.append(("table", _table_html(["Field", "Value"], report.run_metadata, [30, 70])))
    parts.append(("heading", "Reproducibility"))
    if report.tool_calls:
        parts.append((
            "table",
            _table_html(
                ["#", "Tool", "Inputs", "Status", "Output sha256"],
                [[str(t.seq), t.tool, t.inputs, t.status, t.output_sha256[:12]] for t in report.tool_calls],
                [4, 31, 45, 8, 12],
                mono=(1, 2, 4),
            ),
        ))
    else:
        parts.append(("html", _spaced("<p>No tool calls were recorded for this answer.</p>")))
    if report.reproducibility:
        parts.append(("table", _table_html(["Source", "Version or date"], report.reproducibility, [45, 55])))
    parts.append(("html", f'<p line-height="{_LINE}"><i>{_esc(report.footer)}</i></p>'))
    return parts


def _citation_html(c: ReportCitation) -> str:
    """Bold title, then authors, italic venue, identifiers, a muted link and the grade or integrity flags."""
    p = citation_parts(c)
    pieces = []
    if p["title"]:
        pieces.append(f"<b>{_esc(p['title'])}</b>.")
    if p["authors"]:
        pieces.append(f"{_esc(p['authors'])}" + ("" if p["authors"].endswith(".") else "."))
    if p["venue"]:
        pieces.append(f"<i>{_esc(p['venue'])}</i>.")
    if p["ids"]:
        pieces.append(f"{_esc(p['ids'])}.")
    if p["url"]:
        pieces.append(f'<font color="#80868B">{_esc(p["url"])}</font>')
    if p["flags"]:
        pieces.append(f'<font color="#181E48">[{_esc("; ".join(p["flags"]))}]</font>')
    return " ".join(pieces) or "Untitled source"


def _demote_headings(markdown: str) -> str:
    """Answer headings sit under the report's own section headings."""
    out = []
    in_fence = False
    for line in (markdown or "").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        if not in_fence and line.startswith("#"):
            hashes = len(line) - len(line.lstrip("#"))
            if 1 <= hashes <= 6 and line[hashes:hashes + 1] == " ":
                line = "#" * min(hashes + 2, 6) + line[hashes:]
        out.append(line)
    return "\n".join(out)


def render_pdf(report: ReportModel) -> bytes:
    from fpdf import FPDF
    from fpdf.fonts import FontFace, TextStyle

    fallback_dir = dejavu_font_dir()
    if not report_font_path("regular") and not fallback_dir:
        raise FontsUnavailable("Neither the bundled Google Sans fonts nor DejaVu (matplotlib) were found")
    cmap = report_cmap()
    run_label = f"Run {report.run_id}" if report.run_id else "No stored run"
    kind_label = report_kind(report)
    clean = lambda text: strip_unsupported_glyphs(text, cmap)  # noqa: E731

    class _ReportPDF(FPDF):
        def header(self) -> None:  # noqa: D401
            if self.page_no() == 1:
                return
            top = self.get_y()
            if os.path.isfile(BRAND_MARK):
                self.image(BRAND_MARK, x=self.l_margin, y=top, h=4.6)
                self.set_x(self.l_margin + 6)
            self.set_font(_SANS_MEDIUM, "", 8.5)
            self.set_text_color(*_NAVY)
            self.cell(18, 5, "Phronesis")
            self.set_font(_SANS, "", 8)
            self.set_text_color(*_FAINT)
            self.cell(self.epw / 2 - 24, 5, kind_label, align="L")
            self.cell(0, 5, run_label, align="R", new_x="LMARGIN", new_y="NEXT")
            self.set_draw_color(*_RULE)
            self.set_line_width(0.2)
            self.line(self.l_margin, self.get_y() + 1, self.w - self.r_margin, self.get_y() + 1)
            self.ln(6)
            self.set_text_color(*_INK)

        def footer(self) -> None:
            self.set_y(-12)
            self.set_font(_SANS, "", 8)
            self.set_text_color(*_FAINT)
            self.cell(self.epw / 2, 6, f"Phronesis · {run_label}", align="L")
            self.cell(self.epw / 2, 6, f"Page {self.page_no()} of {{nb}}", align="R")
            self.set_text_color(*_INK)

    pdf = _ReportPDF(format="A4")
    pdf.set_title(report.title)
    pdf.set_author("Phronesis")
    pdf.set_subject(clean((report.question or "")[:200]))

    def font(family: str, style: str, key: str, fallback: str) -> None:
        path = report_font_path(key) or (os.path.join(fallback_dir, fallback) if fallback_dir else None)
        if path:
            pdf.add_font(family, style, path)

    font(_SANS, "", "regular", "DejaVuSans.ttf")
    font(_SANS, "B", "bold", "DejaVuSans-Bold.ttf")
    font(_SANS, "I", "italic", "DejaVuSans-Oblique.ttf")
    font(_SANS, "BI", "bold_italic", "DejaVuSans-BoldOblique.ttf")
    font(_SANS_MEDIUM, "", "medium", "DejaVuSans-Bold.ttf")
    font(_MONO, "", "mono", "DejaVuSansMono.ttf")
    font(_MONO, "B", "mono_bold", "DejaVuSansMono-Bold.ttf")
    font(_MONO, "I", "mono", "DejaVuSansMono-Oblique.ttf")
    font(_MONO, "BI", "mono_bold", "DejaVuSansMono-BoldOblique.ttf")
    if fallback_dir and report_font_path("regular"):
        # Glyphs Google Sans lacks (superscript minus, some arrows) come from DejaVu instead of being dropped.
        pdf.add_font(_FALLBACK, "", os.path.join(fallback_dir, "DejaVuSans.ttf"))
        pdf.add_font(_FALLBACK, "B", os.path.join(fallback_dir, "DejaVuSans-Bold.ttf"))
        pdf.set_fallback_fonts([_FALLBACK], exact_match=False)

    pdf.alias_nb_pages()
    pdf.set_margins(_MARGIN_X, _MARGIN_TOP, _MARGIN_X)
    pdf.set_auto_page_break(True, _MARGIN_BOTTOM)
    pdf.add_page()

    # -- title block ---------------------------------------------------------------------------------------------
    question = (report.question or "").strip()
    question_in_title = question_is_title(report)
    top = pdf.get_y()
    if os.path.isfile(BRAND_MARK) and os.path.isfile(BRAND_WORDMARK):
        pdf.image(BRAND_MARK, x=pdf.l_margin, y=top, h=9)
        pdf.image(BRAND_WORDMARK, x=pdf.l_margin + 10.5, y=top + 2.2, h=4.6)
    else:
        pdf.set_font(_SANS_MEDIUM, "", 13)
        pdf.set_text_color(*_NAVY)
        pdf.cell(40, 9, "Phronesis")
    pdf.set_xy(pdf.l_margin, top + 2.4)
    pdf.set_font(_SANS_MEDIUM, "", 7.5)
    pdf.set_text_color(*_FAINT)
    pdf.set_char_spacing(0.6)
    pdf.cell(0, 4, kind_label.upper(), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_char_spacing(0)
    pdf.set_y(top + 9)
    pdf.set_draw_color(*_NAVY)
    pdf.set_line_width(0.6)
    pdf.line(pdf.l_margin, pdf.get_y() + 2, pdf.l_margin + 14, pdf.get_y() + 2)
    pdf.ln(7)
    pdf.set_font(_SANS_MEDIUM, "", 17)
    pdf.set_text_color(*_NAVY)
    title = question if question_in_title else report.title
    pdf.multi_cell(0, 17 * 0.3528 * 1.3, clean(title[:1].upper() + title[1:]), align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    cells = [(k, v) for k, v in report_headline(report)]
    cols = 3
    col_w = pdf.epw / cols
    for start in range(0, len(cells), cols):
        row = cells[start:start + cols]
        y = pdf.get_y()
        for i, (label, _value) in enumerate(row):
            pdf.set_xy(pdf.l_margin + i * col_w, y)
            pdf.set_font(_SANS, "", 7.5)
            pdf.set_text_color(*_FAINT)
            pdf.cell(col_w - 4, 4, label.upper())
        for i, (_label, value) in enumerate(row):
            pdf.set_xy(pdf.l_margin + i * col_w, y + 4.2)
            pdf.set_font(_SANS, "", 9.5)
            pdf.set_text_color(*_INK)
            text = clean(value)
            while text and pdf.get_string_width(text) > col_w - 4:
                text = text[:-2].rstrip() + "…"
            pdf.cell(col_w - 4, 5, text)
        pdf.set_xy(pdf.l_margin, y + 11)
    pdf.ln(1)
    pdf.set_draw_color(*_RULE)
    pdf.set_line_width(0.3)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(4)

    # -- body ----------------------------------------------------------------------------------------------------
    tag_styles = {
        "p": TextStyle(t_margin=0, b_margin=2.4),
        "li": TextStyle(t_margin=1.2, l_margin=5),
        "ul": TextStyle(t_margin=0.5, b_margin=2.4),
        "ol": TextStyle(t_margin=0.5, b_margin=2.4),
        "code": FontFace(family=_MONO, size_pt=8.5, color=_NAVY),
        "pre": TextStyle(font_family=_MONO, font_size_pt=8.5, t_margin=2, b_margin=3),
        "a": FontFace(color=_NAVY),
        "h1": TextStyle(font_family=_SANS_MEDIUM, font_size_pt=13, color=_NAVY, t_margin=5, b_margin=1.5),
        "h2": TextStyle(font_family=_SANS_MEDIUM, font_size_pt=12.5, color=_NAVY, t_margin=5, b_margin=1.5),
        "h3": TextStyle(font_family=_SANS_MEDIUM, font_size_pt=11.5, color=_NAVY, t_margin=4.5, b_margin=1.5),
        "h4": TextStyle(font_family=_SANS_MEDIUM, font_size_pt=10.5, color=_NAVY, t_margin=4, b_margin=1),
        "h5": TextStyle(font_family=_SANS_MEDIUM, font_size_pt=10.5, color=_NAVY, t_margin=3.5, b_margin=0.5),
        "h6": TextStyle(font_family=_SANS_MEDIUM, font_size_pt=10, color=_MUTED, t_margin=3.5, b_margin=0.5),
        "blockquote": TextStyle(font_family=_SANS, color=_AMBER, t_margin=1, b_margin=3, l_margin=4),
    }

    def write(markup: str, size: float = _BODY_PT) -> None:
        pdf.set_font(_SANS, "", size)
        pdf.set_text_color(*_INK)
        pdf.write_html(
            clean(markup),
            font_family=_SANS,
            tag_styles=tag_styles,
            table_line_separators=True,
            ul_bullet_char="•",
            li_prefix_color=_FAINT,
        )

    def heading(text: str) -> None:
        if pdf.get_y() > pdf.page_break_trigger - 24:  # keep a heading with at least a few lines of its section
            pdf.add_page()
        pdf.ln(4)
        pdf.set_font(_SANS_MEDIUM, "", 12.5)
        pdf.set_text_color(*_NAVY)
        pdf.multi_cell(0, 6.5, clean(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*_RULE)
        pdf.set_line_width(0.2)
        pdf.line(pdf.l_margin, pdf.get_y() + 0.8, pdf.w - pdf.r_margin, pdf.get_y() + 0.8)
        pdf.ln(1.6)
        pdf.set_text_color(*_INK)

    parts = _parts(report, question_in_title=question_in_title)
    for idx, (kind, value) in enumerate(parts):
        if kind == "heading":
            # A heading never ends a page: a following table or plot needs a block of room, not just a line.
            follows = parts[idx + 1][0] if idx + 1 < len(parts) else ""
            if follows in ("table", "plot") and pdf.get_y() > pdf.page_break_trigger - 45:
                pdf.add_page()
            heading(value)
        elif kind == "plot":
            try:
                pdf.image(io.BytesIO(report.plot_png or b""), w=pdf.epw)
            except Exception:  # noqa: BLE001
                write("<p><i>The simulation plot could not be embedded.</i></p>")
            pdf.ln(3)
        elif kind == "table":
            pdf.ln(1.5)
            write(value, size=8.5)
            pdf.ln(2)
        else:
            write(value)
    return bytes(pdf.output())
