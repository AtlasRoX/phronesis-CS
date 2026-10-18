"""Report export (owned by EXPORT, feature 9). Renderers import python-docx and fpdf2 lazily."""

from __future__ import annotations

from .report_model import ExportMessage, ExportRequest, ReportModel, build_report_model, report_markdown


def render_markdown(report: ReportModel) -> bytes:
    return report_markdown(report).encode("utf-8")


def render_docx(report: ReportModel) -> bytes:
    from .docx_renderer import render_docx as _render

    return _render(report)


def render_pdf(report: ReportModel) -> bytes:
    from .pdf_renderer import render_pdf as _render

    return _render(report)


def render_bundle(report: ReportModel) -> bytes:
    from .bundle import render_bundle as _render

    return _render(report)


__all__ = [
    "ExportMessage",
    "ExportRequest",
    "ReportModel",
    "build_report_model",
    "render_bundle",
    "render_docx",
    "render_markdown",
    "render_pdf",
]
