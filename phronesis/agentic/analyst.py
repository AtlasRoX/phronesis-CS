"""
Analyst phase (feature 10, stage augment, order 50): sandboxed code analysis of an attached dataset.

- CodeAct (Wang et al., ICML 2024, arXiv:2402.01030): Python code is the action. The model writes a cell, the
  sandbox runs it, and the output or traceback is the observation for the next cell, up to 4 iterations.
- Biomni (Huang et al. 2025, bioRxiv, doi:10.1101/2025.05.30.656746): a numbered plan first, then code.
- BixBench (Mitchener et al. 2025, arXiv:2503.00096): notebook-style analysis of a real dataset, where
  "insufficient information" is an acceptable answer.
- Data Interpreter (Hong et al. 2024, arXiv:2402.18679): each step's output is checked before the next one; the
  model sets done only once the observed result answers the question.
- Untrusted text (cell values, column names, stdout) reaches the model only spotlighted (Hines et al. 2024,
  arXiv:2403.14720), following the indirect-injection threat model of Greshake et al. 2023 (arXiv:2302.12173).
  Every cell passes the AST policy before it runs, and runs only through the registry tool `analysis_run_python`
  via state.call_tool, so it is budgeted and traced.

Runs only when PHRONESIS_SANDBOX=on, agent_options.enable_code_analysis is not false, a CSV/TSV/h5ad dataset is
attached, and the question asks for an analysis. Without a model, no model-written code runs: a fixed
descriptive-statistics script (describe, plus group summaries when a grouping column is obvious) is used instead.
Artifact `sandbox_analysis`.
"""

from __future__ import annotations

import json
import logging
import re
from html import escape
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .budget import BudgetExhausted
from .guardrails import SPOTLIGHT_RULE, neutralise, spotlight
from .llm import AgentLLMUnavailable
from .runtime import AgentPhase, PhaseOutcome, register_phase
from .sandbox import ANALYSABLE_KINDS, bound_scope, current_scope, resolve_inputs, sandbox_enabled, sandbox_timeout_s
from .sandbox.policy import ALLOWED_MODULES, check_code
from .state import AgentState

logger = logging.getLogger(__name__)

PHASE_KEY = "analyst"
TOOL_NAME = "analysis_run_python"
RESULT_KEY = "agent.analyst.sandbox"
MAX_ITERATIONS = 4

ANALYSIS_INTENT_RE = re.compile(
    r"\b(?:analy[sz](?:e|es|ed|ing|is)|compute|statistic|correlat|regress|differential|survival|plot|distribution|"
    r"compare groups|test whether)",
    re.I,
)
_PLOT_RE = re.compile(r"\b(?:plot|distribution|histogram|visuali[sz]e|figure|chart)", re.I)
_INSUFFICIENT_RE = re.compile(r"\binsufficient information\b", re.I)
GROUP_COLUMN_NAMES = (
    "group", "condition", "treatment", "cluster", "cell_type", "celltype", "sample", "batch", "arm", "status",
    "label", "class", "leiden", "louvain", "genotype", "tissue",
)


class AnalystStep(BaseModel):
    plan: List[str] = Field(default_factory=list)
    code: str = ""
    done: bool = False
    answer_summary: str = ""


def has_analysis_intent(text: str) -> bool:
    return bool(ANALYSIS_INTENT_RE.search(text or ""))


def analysable_attachment(ctx: Any) -> Optional[Any]:
    return next((a for a in (getattr(ctx, "attachments", None) or []) if getattr(a, "kind", "") in ANALYSABLE_KINDS), None)


# Dataset preview (in-process, bounded) -------------------------------------------------------------------------

def _preview(inp: Any) -> Dict[str, Any]:
    """Columns, dtypes and the first rows: pandas reads at most 20 rows; h5ad is opened backed (no X load)."""
    import pandas as pd

    if inp.kind == "table":
        df = pd.read_csv(inp.path, sep=inp.sep or ",", index_col=0, nrows=20)
        cols = [{"name": str(c), "dtype": str(t)} for c, t in list(df.dtypes.items())[:60]]
        with pd.option_context("display.max_columns", 12, "display.width", 160):
            head = df.head(5).to_string(max_cols=12, max_colwidth=24)
        return {"kind": "table", "n_columns": int(df.shape[1]), "columns": cols, "index_name": str(df.index.name or ""),
                "head": head, "note": "first column is the row index (index_col=0)"}
    import anndata as ad

    adata = ad.read_h5ad(inp.path, backed="r")
    try:
        obs = adata.obs
        cols = [{"name": str(c), "dtype": str(t)} for c, t in list(obs.dtypes.items())[:60]]
        with pd.option_context("display.max_columns", 12, "display.width", 160):
            head = obs.head(5).to_string(max_cols=12, max_colwidth=24) if obs.shape[1] else ""
        return {"kind": "h5ad", "n_obs": int(adata.n_obs), "n_vars": int(adata.n_vars), "obs_columns": cols,
                "var_names_head": [str(v) for v in list(adata.var_names[:20])], "head": head}
    finally:
        try:
            adata.file.close()
        except Exception:  # noqa: BLE001
            pass


def _preview_text(inp: Any, preview: Dict[str, Any]) -> str:
    lines = [f"file name in the working directory: {inp.name}", f"kind: {preview.get('kind')}"]
    if preview.get("kind") == "table":
        lines.append(f"separator: {'tab' if inp.sep == chr(9) else 'comma'}; {preview.get('note', '')}")
        lines.append(f"columns ({preview.get('n_columns')}): " + ", ".join(f"{c['name']} [{c['dtype']}]" for c in preview["columns"]))
    else:
        lines.append(f"n_obs={preview.get('n_obs')} n_vars={preview.get('n_vars')}")
        lines.append("obs columns: " + (", ".join(f"{c['name']} [{c['dtype']}]" for c in preview.get("obs_columns") or []) or "none"))
        lines.append("first var names: " + ", ".join(preview.get("var_names_head") or []))
    if preview.get("head"):
        lines.append("first rows:\n" + str(preview["head"]))
    return "\n".join(lines)


# Deterministic script (no model) -------------------------------------------------------------------------------

def deterministic_script(inp: Any, query: str) -> str:
    """A fixed, policy-clean descriptive-statistics cell for one input file."""
    plot = bool(_PLOT_RE.search(query or ""))
    names = repr(GROUP_COLUMN_NAMES)
    if inp.kind == "table":
        lines = [
            "import pandas as pd",
            f"df = pd.read_csv({inp.name!r}, sep={(inp.sep or ',')!r}, index_col=0)",
            'numeric = df.select_dtypes(include="number")',
            "desc = numeric.describe().T",
            "result = {",
            '    "n_rows": int(df.shape[0]),',
            '    "n_columns": int(df.shape[1]),',
            '    "n_numeric_columns": int(numeric.shape[1]),',
            '    "missing_values": int(df.isna().sum().sum()),',
            "}",
            "if numeric.shape[1]:",
            "    top = numeric.mean().sort_values(ascending=False).head(10)",
            '    result["highest_mean_columns"] = {str(k): round(float(v), 4) for k, v in top.items()}',
            "group_col = None",
            "for c in df.columns:",
            "    if not pd.api.types.is_numeric_dtype(df[c]) and 2 <= df[c].nunique() <= 10:",
            "        group_col = c",
            "        break",
            "if group_col is None:",
            "    for c in df.columns:",
            f"        if str(c).lower() in {names} and 2 <= df[c].nunique() <= 10:",
            "            group_col = c",
            "            break",
            'tables = {"describe": desc.head(50).round(4)}',
            "if group_col is not None:",
            '    result["group_column"] = str(group_col)',
            '    result["group_sizes"] = {str(k): int(v) for k, v in df[group_col].value_counts().items()}',
            "    values = numeric.drop(columns=[group_col], errors=\"ignore\")",
            "    if values.shape[1]:",
            '        tables["group_means"] = values.groupby(df[group_col]).mean().T.head(50).round(4)',
        ]
        if plot:
            lines += [
                "import matplotlib.pyplot as plt",
                "if numeric.shape[1]:",
                "    fig, ax = plt.subplots(figsize=(7, 4))",
                "    numeric.iloc[:, : min(numeric.shape[1], 12)].plot.box(ax=ax, rot=90)",
                '    ax.set_title("Distribution of the first numeric columns")',
                "    fig.tight_layout()",
            ]
        return "\n".join(lines) + "\n"
    lines = [
        "import numpy as np",
        "import pandas as pd",
        "import anndata as ad",
        f"adata = ad.read_h5ad({inp.name!r})",
        "X = adata.X",
        "means = np.asarray(X.mean(axis=0)).ravel()",
        "detected = np.asarray((X > 0).sum(axis=0)).ravel()",
        'genes = pd.DataFrame({"mean": means, "cells_detected": detected}, index=[str(v) for v in adata.var_names])',
        'top = genes.sort_values("mean", ascending=False).head(20)',
        "result = {",
        '    "n_obs": int(adata.n_obs),',
        '    "n_vars": int(adata.n_vars),',
        '    "obs_columns": [str(c) for c in adata.obs.columns][:30],',
        '    "top_mean_genes": {str(k): round(float(v), 4) for k, v in top["mean"].items()},',
        "}",
        'tables = {"top_genes": top.round(4)}',
        "group_col = None",
        "for c in adata.obs.columns:",
        "    n = adata.obs[c].nunique()",
        "    if not pd.api.types.is_numeric_dtype(adata.obs[c]) and 2 <= n <= 20:",
        "        group_col = c",
        "        break",
        "if group_col is not None:",
        "    labels = adata.obs[group_col].astype(str).values",
        '    result["group_column"] = str(group_col)',
        '    result["group_sizes"] = {str(k): int(v) for k, v in adata.obs[group_col].value_counts().items()}',
        "    chosen = list(top.index[:10])",
        "    sub = adata[:, chosen].X",
        '    dense = sub.toarray() if hasattr(sub, "toarray") else np.asarray(sub)',
        "    frame = pd.DataFrame(dense, columns=chosen)",
        "    frame['group'] = labels",
        "    tables[\"group_means\"] = frame.groupby('group').mean().T.round(4)",
    ]
    if plot:
        lines += [
            "import matplotlib.pyplot as plt",
            "fig, ax = plt.subplots(figsize=(6, 4))",
            "ax.hist(np.log1p(means), bins=40)",
            'ax.set_xlabel("log1p(mean expression per gene)")',
            'ax.set_ylabel("genes")',
            "fig.tight_layout()",
        ]
    return "\n".join(lines) + "\n"


# Prompting -------------------------------------------------------------------------------------------------------

def _system_prompt(inputs: List[Any]) -> str:
    names = ", ".join(i.name for i in inputs)
    return (
        "[phase:analyst] You are a computational biologist working in a Python notebook sandbox (CodeAct): you write "
        "one code cell at a time, it is executed, and you see its output before writing the next one.\n"
        "Rules:\n"
        "1. Start with a short numbered plan, then write the code for the next step only.\n"
        f"2. Allowed imports only: {', '.join(sorted(ALLOWED_MODULES))}.\n"
        f"3. The input files are in the working directory: {names}. Read them by these exact names. Uploaded tables "
        "store the row names in the first column (use index_col=0).\n"
        "4. Never write files, open network connections, use os/sys/subprocess/eval/exec, or touch dunder attributes; "
        "such code is rejected before it runs.\n"
        "5. Put the numbers that answer the question in a variable named `result` (a dict of JSON values). Put tables "
        "to show in `tables` (a dict of name -> DataFrame). Matplotlib figures left open are returned (at most 3).\n"
        "6. Check each output before continuing. Set done=true once the observed output answers the question.\n"
        "7. If the dataset cannot answer the question, reply with done=true, code=\"\" and an answer_summary starting "
        "with \"insufficient information:\" and the reason. Do not invent columns or values.\n"
        'Reply with JSON only: {"plan": ["..."], "code": "...", "done": false, "answer_summary": "..."}\n'
        + SPOTLIGHT_RULE
    )


def _observation(report: Dict[str, Any]) -> str:
    parts: List[str] = [f"status: {report.get('status')}"]
    if report.get("violations"):
        parts.append("policy violations: " + "; ".join(report["violations"][:8]))
    if report.get("error"):
        parts.append(f"error:\n{report['error']}")
    if report.get("stdout"):
        parts.append(f"stdout:\n{str(report['stdout'])[:2500]}")
    if report.get("result") is not None:
        parts.append("result: " + json.dumps(report["result"], default=str)[:2500])
    for t in (report.get("tables") or [])[:3]:
        parts.append(f"table {t.get('name')}: columns {t.get('columns')}; {t.get('n_rows')} rows; first rows {t.get('rows', [])[:5]}")
    if report.get("figures"):
        parts.append(f"figures: {len(report['figures'])} saved")
    return "\n".join(parts)


def _user_prompt(query: str, inp: Any, preview_text: str, attempts: List[Dict[str, Any]]) -> str:
    out = [
        "<question>",
        spotlight(query, "researcher_question", 2000),
        "</question>",
        f'<dataset id="{escape(inp.dataset_id)}" file="{escape(inp.name)}" kind="{escape(inp.kind)}">',
        spotlight(preview_text, f"dataset:{inp.name}", 3500),
        "</dataset>",
    ]
    for a in attempts[-3:]:
        out.append(f'<attempt n="{a["iteration"]}">')
        out.append(f"<code>\n{escape(a['code'][:4000], quote=False)}\n</code>")
        out.append("<observation>")
        out.append(spotlight(_observation(a["report"]), "sandbox_output", 4000))
        out.append("</observation>")
        out.append("</attempt>")
    out.append("Write the next step as JSON.")
    return "\n".join(out)


# Evidence --------------------------------------------------------------------------------------------------------

def _text(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    text = neutralise(text or "")
    return text[:limit] + (" ...(truncated)" if len(text) > limit else "")


def _evidence_xml(art: Dict[str, Any]) -> str:
    ds = art.get("dataset") or {}
    head = (
        f'<sandbox_analysis key="{RESULT_KEY}" dataset="{escape(str(ds.get("name") or ""), quote=True)}" '
        f'dataset_id="{escape(str(ds.get("id") or ""), quote=True)}" status="{escape(str(art.get("status")), quote=True)}" '
        f'iterations="{art.get("iterations")}" author="{escape(str(art.get("author")), quote=True)}" '
        'note="computed by sandboxed Python over the uploaded dataset; quote values exactly as given and name the '
        'dataset; exploratory analysis of user-provided data, not peer-reviewed evidence">'
    )
    body: List[str] = [head]
    if art.get("plan"):
        body.append("<plan>" + escape(_text(" | ".join(art["plan"]), 800), quote=False) + "</plan>")
    if art.get("code"):
        body.append("<code>" + escape(str(art["code"])[:4000], quote=False) + "</code>")
    if art.get("result") is not None:
        body.append("<result>" + escape(_text(art["result"], 3000), quote=False) + "</result>")
    for t in (art.get("tables") or [])[:3]:
        rows = ["\t".join(str(c) for c in t.get("columns") or [])]
        rows += ["\t".join("" if v is None else str(v) for v in r) for r in (t.get("rows") or [])[:20]]
        body.append(
            f'<table name="{escape(str(t.get("name")), quote=True)}" n_rows="{t.get("n_rows")}">'
            + escape(_text("\n".join(rows), 2500), quote=False)
            + "</table>"
        )
    if art.get("stdout"):
        body.append("<stdout>" + escape(_text(art["stdout"], 2000), quote=False) + "</stdout>")
    if art.get("error"):
        body.append("<error>" + escape(_text(art["error"], 800), quote=False) + "</error>")
    if art.get("answer_summary"):
        body.append("<analyst_summary>" + escape(_text(art["answer_summary"], 800), quote=False) + "</analyst_summary>")
    if art.get("figures"):
        body.append(f'<figures count="{len(art["figures"])}" note="shown to the researcher beside the answer"/>')
    body.append("</sandbox_analysis>")
    return "\n".join(body)


def _policy_block() -> Dict[str, Any]:
    return {
        "timeout_s": sandbox_timeout_s(),
        "network": "blocked",
        "isolation": "AST policy + isolated subprocess (python -I) in a temp work dir + scrubbed environment + "
        "network stub + resource limits (defence in depth, not a container)",
    }


# Phase ------------------------------------------------------------------------------------------------------------

class AnalystPhase(AgentPhase):
    key = PHASE_KEY
    stage = "augment"
    order = 50
    frameworks = None
    needs_llm = False

    def applies(self, state: AgentState) -> bool:
        if not sandbox_enabled():
            return False
        if state.options.get("enable_code_analysis", True) is False:
            return False
        if state.registry is None or state.registry.get_entry(TOOL_NAME) is None:
            return False
        if analysable_attachment(state.ctx) is None:
            return False
        return has_analysis_intent(state.clean_query or state.query)

    async def _execute(self, state: AgentState, code: str, inp: Any, purpose: str) -> Dict[str, Any]:
        with bound_scope(state.user_id, [a.id for a in state.ctx.attachments]):
            res = await state.call_tool(
                TOOL_NAME,
                phase=PHASE_KEY,
                purpose=purpose,
                store=False,
                code=code,
                dataset_ids=[inp.dataset_id],
                timeout_s=sandbox_timeout_s(),
            )
        report = getattr(res, "data", None)
        if not isinstance(report, dict) or "status" not in report:
            report = {"status": "error", "error": str(getattr(res, "error", "") or "the sandbox tool failed")[:400]}
        return report

    async def run(self, state: AgentState) -> Optional[PhaseOutcome]:
        att = analysable_attachment(state.ctx)
        with bound_scope(state.user_id, [a.id for a in state.ctx.attachments]):
            inputs, errors = resolve_inputs(current_scope(), [att.id])
        if not inputs:
            await state.step("agent_analyst", "Code analysis skipped", "; ".join(errors)[:300] or "dataset not available", status="error", agent="Analyst")
            return None
        inp = inputs[0]
        query = state.clean_query or state.query
        await state.step("agent_analyst", f"Analysing {inp.display_name} in the code sandbox", status="running", agent="Analyst")

        import asyncio

        try:
            preview = await asyncio.to_thread(_preview, inp)
            preview_text = _preview_text(inp, preview)
        except Exception as exc:  # noqa: BLE001
            logger.info("Dataset preview failed: %s", exc)
            preview_text = f"file name in the working directory: {inp.name}\nkind: {inp.kind}\n(preview unavailable)"

        attempts: List[Dict[str, Any]] = []
        plan: List[str] = []
        summary = ""
        author = "deterministic"
        steps_taken = 0
        if state.llm.available:
            author = "llm"
            system = _system_prompt(inputs)
            for i in range(1, MAX_ITERATIONS + 1):
                try:
                    step = await state.llm.json(system, _user_prompt(query, inp, preview_text, attempts), schema=AnalystStep, phase=PHASE_KEY)
                except (ValueError, AgentLLMUnavailable) as exc:
                    logger.info("Analyst model step failed: %s", exc)
                    break
                except BudgetExhausted:
                    break
                steps_taken = i
                plan = [str(p)[:300] for p in step.plan[:10]] or plan
                code = (step.code or "").strip()
                if step.answer_summary:
                    summary = step.answer_summary.strip()[:1200]
                if not code:
                    if step.done:
                        break
                    continue
                violations = check_code(code, [x.name for x in inputs])
                if violations:
                    report: Dict[str, Any] = {"status": "policy_violation", "error": "rejected by the sandbox policy", "violations": violations}
                else:
                    report = await self._execute(state, code, inp, f"analysis cell {i}: " + (plan[0] if plan else query)[:150])
                attempts.append({"iteration": i, "code": code, "report": report})
                await state.step(
                    f"agent_analyst_{i}",
                    f"Cell {i}: {report.get('status')}",
                    str(report.get("error") or "")[:240],
                    status="done" if report.get("status") == "ok" else "error",
                    agent="Analyst",
                )
                if report.get("status") == "ok" and step.done:
                    break
            if not attempts and not _INSUFFICIENT_RE.search(summary):
                author = "deterministic"  # the model gave no runnable step; fall back to the fixed script
        if author == "deterministic":
            code = deterministic_script(inp, query)
            plan = ["Load the attached dataset", "Descriptive statistics per numeric column",
                    "Group summaries when a grouping column is obvious"]
            report = await self._execute(state, code, inp, "descriptive statistics of the attached dataset")
            attempts.append({"iteration": 1, "code": code, "report": report})
            steps_taken = 1

        final = next((a for a in reversed(attempts) if a["report"].get("status") == "ok"), attempts[-1] if attempts else None)
        rep = final["report"] if final else {}
        insufficient = bool(_INSUFFICIENT_RE.search(summary)) and not any(a["report"].get("status") == "ok" for a in attempts)
        status = "ok" if rep.get("status") == "ok" else "error"
        art: Dict[str, Any] = {
            "status": status,
            "iterations": steps_taken,
            "author": author,
            "plan": plan,
            "code": final["code"] if final else "",
            "stdout": str(rep.get("stdout") or "")[:20000],
            "result": rep.get("result"),
            "error": rep.get("error") if status != "ok" else None,
            "dataset": {"id": inp.dataset_id, "name": inp.display_name, "kind": inp.kind, "file": inp.name},
            "figures": list(rep.get("figures") or [])[:3],
            "tables": list(rep.get("tables") or [])[:5],
            "answer_summary": summary,
            "insufficient": insufficient,
            "attempts": [
                {"iteration": a["iteration"], "status": a["report"].get("status"), "error": str(a["report"].get("error") or "")[:300],
                 "violations": list(a["report"].get("violations") or [])[:5]}
                for a in attempts
            ],
            "policy": _policy_block(),
        }
        if insufficient:
            art["error"] = summary
        state.artifacts["sandbox_analysis"] = art

        from ..tools.registry import ErrorType, ToolResult

        compact = {k: v for k, v in art.items() if k not in ("figures", "attempts", "policy")}
        state.add_result(
            RESULT_KEY,
            ToolResult(
                success=status == "ok",
                tool_name=TOOL_NAME,
                data=compact,
                error=None if status == "ok" else str(art.get("error") or "analysis failed")[:300],
                error_type=None if status == "ok" else ErrorType.INTERNAL_ERROR,
                metadata={"sandbox": "subprocess", "author": author},
            ),
            phase=PHASE_KEY,
            purpose=f"sandboxed analysis of {inp.display_name}",
            render=False,
        )
        if status == "ok" or insufficient:
            state.evidence_sections.append(_evidence_xml(art))
        if status == "ok":
            state.brief_notes.append(
                f"A sandboxed code analysis of the attached dataset {inp.display_name} ran (see <sandbox_analysis>). "
                "Report its computed values exactly as given, say they were computed from the uploaded data, and say "
                "what the analysis could not determine."
            )
        elif insufficient:
            state.brief_notes.append(
                f"The sandboxed analysis judged the attached dataset {inp.display_name} insufficient for this question "
                "(see <sandbox_analysis>); say so instead of estimating values."
            )
        else:
            state.scope_notes.append(f"The sandboxed analysis of {inp.display_name} did not complete; do not report values from it.")
        title = (
            f"Analysed {inp.display_name}: {len(art['tables'])} table(s), {len(art['figures'])} figure(s)"
            if status == "ok"
            else ("The data were judged insufficient for this question" if insufficient else "Code analysis failed")
        )
        await state.step("agent_analyst", title, str(art.get("error") or "")[:240], status="done" if status == "ok" or insufficient else "error", agent="Analyst")
        return PhaseOutcome(artifacts={"sandbox_analysis": art})


register_phase(AnalystPhase())


__all__ = ["AnalystPhase", "AnalystStep", "analysable_attachment", "deterministic_script", "has_analysis_intent"]
