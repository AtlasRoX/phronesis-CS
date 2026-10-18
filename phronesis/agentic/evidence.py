"""
Evidence XML for results that agentic phases added to tool_results (keys 'agent.<phase>.<n>.<tool>').

Literature payloads (ranked_passages) render as citable <document> elements like the pipeline's own
literature section; web results and other free text are spotlighted as untrusted data; everything else is
compact JSON. Phases that render their own section set render=False on the result label.
"""

from __future__ import annotations

import json
from html import escape
from typing import Any, Dict, List

from .guardrails import spotlight

_MAX_JSON_CHARS = 3000
_MAX_PASSAGES = 5
_PASSAGE_CHARS = 700


def _attr(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _compact(data: Any) -> str:
    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if not str(k).endswith(("_base64_png", "_png", "base64"))}
        if isinstance(value, list):
            return [strip(v) for v in value[:40]]
        return value

    try:
        text = json.dumps(strip(data), default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(data)
    if len(text) > _MAX_JSON_CHARS:
        text = text[:_MAX_JSON_CHARS] + " ...(truncated)"
    return text


def render_result(key: str, result: Any, label: Dict[str, str]) -> str:
    data = getattr(result, "data", None)
    tool = getattr(result, "tool_name", "")
    head = (
        f'<tool_output key="{_attr(key)}" tool="{_attr(tool)}" phase="{_attr(label.get("phase"))}" '
        f'purpose="{_attr(label.get("purpose"))}">'
    )
    body: List[str] = []
    if isinstance(data, dict) and isinstance(data.get("ranked_passages"), list):
        # Exact-identifier lookups and correction linkage (lithoscribe limitations): what was and was not retrieved.
        for note in (data.get("limitations") or [])[:6]:
            if isinstance(note, str) and note.strip():
                body.append(f"<search_note>{escape(note.strip()[:400])}</search_note>")
        relaxed = (data.get("retrieval") or {}).get("anchor_relaxed") or []
        if relaxed:
            body.append(
                f"<search_note>No retrieved record names {escape(', '.join(map(str, relaxed)))}; these passages are "
                "about the broader subject.</search_note>"
            )
        for i, p in enumerate(data["ranked_passages"][:_MAX_PASSAGES], 1):
            if not isinstance(p, dict):
                continue
            text = str(p.get("passage_text", ""))
            if len(text) > _PASSAGE_CHARS:
                text = text[:_PASSAGE_CHARS].rsplit(" ", 1)[0] + " [...]"
            from ..agents.prompts import level_attr  # the same "evidence_level" label the synthesis evidence carries

            level = level_attr(p).strip()
            pmid = str(p.get("pmid") or "")
            # The paper's own title and record URL: an answer asked for "exact title" and "source URL" needs them.
            url = str(p.get("url") or "") or (f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid.isdigit() else "")
            body.append(
                f'<document index="{i}" pmid="{_attr(pmid)}" doi="{_attr(p.get("doi") or "")}" '
                f'title="{_attr(p.get("title") or "")}" url="{_attr(url)}" '
                f'citation="{_attr(p.get("citation") or p.get("citation_label") or "")}" '
                f'setting="{_attr(p.get("grade_label") or p.get("experimental_setting") or "unspecified")}"'
                f'{" " + level if level else ""}>'
                f"{escape(text)}</document>"
            )
    elif isinstance(data, dict) and isinstance(data.get("results"), list) and tool.startswith("websurfer"):
        from ..utils.source_authority import record_url, source_authority

        for r in data["results"][:4]:
            if isinstance(r, dict):
                body.append(
                    f'<result url="{_attr(record_url(r))}" title="{_attr(r.get("title"))}" '
                    f'authority="{source_authority(record_url(r))}">'
                    + spotlight(str(r.get("snippet") or r.get("content") or ""), str(r.get("url") or "web"), 500)
                    + "</result>"
                )
    else:
        body.append(f"<data>{escape(_compact(data), quote=False)}</data>")
    return "\n".join([head, *body, "</tool_output>"])


def render_agent_evidence(tool_results: Dict[str, Any], labels: Dict[str, Dict[str, str]], sections: List[str]) -> List[str]:
    """XML sections for agentic results that succeeded, plus the phases' own sections."""
    rendered: List[str] = []
    for key, label in labels.items():
        res = tool_results.get(key)
        if res is None or not getattr(res, "success", False) or not label.get("render"):
            continue
        rendered.append(render_result(key, res, label))
    out: List[str] = []
    if rendered:
        out.append('<agent_evidence note="gathered by follow-up agent steps; the same grounding and citation rules apply">')
        out.extend(rendered)
        out.append("</agent_evidence>")
    out.extend(s for s in sections if s and s.strip())
    return out
