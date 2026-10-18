"""
Where evaluation cases come from.

- Built-in suites ship with the package (phronesis/evals/data/<name>.jsonl): "clinical" (curated clinical and
  research questions with verified gold PMIDs, required facts and reference answers), "behavior" (routing,
  scope, abstention, injection, simulation and single-cell behaviour), "golden" (long live questions, one per
  orchestration framework, with structured golden facts: identifiers, dates, hazard ratios with CIs) and "audit"
  (the former scripts/audit_evaluation_runner.py cases, with assertions). Cases tagged "live" need the live
  system (models, network); a suite whose every case is live is listed as live only.
- Any JSONL file of EvalCase objects: pass its path as the suite.
- Public benchmarks, downloaded on first use into the cache (PHRONESIS_EVAL_CACHE, default ~/.cache/phronesis/evals)
  and never committed: "pubmedqa" (PQA-L, 1,000 expert-labelled yes/no/maybe questions, MIT licence), "litqa2"
  (LAB-Bench LitQA2, multiple choice with source DOIs, CC BY-SA; carries a training-data canary), and "bioasq"
  (BioASQ task B; registration is required, so pass the downloaded JSON with --bioasq-file).
"""

from __future__ import annotations

import ast
import json
import os
import random
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .schema import AnswerExpect, EvalCase, RetrievalExpect

DATA_DIR = Path(__file__).parent / "data"
PUBMEDQA_URL = "https://raw.githubusercontent.com/pubmedqa/pubmedqa/master/data/ori_pqal.json"
LITQA2_ROWS_URL = "https://datasets-server.huggingface.co/rows"
EXTERNAL = ("pubmedqa", "litqa2", "bioasq")


def cache_dir() -> Path:
    path = Path(os.getenv("PHRONESIS_EVAL_CACHE") or Path.home() / ".cache" / "phronesis" / "evals")
    path.mkdir(parents=True, exist_ok=True)
    return path


def builtin_suites() -> List[str]:
    return sorted(p.stem for p in DATA_DIR.glob("*.jsonl"))


def is_live_suite(cases: Iterable[EvalCase]) -> bool:
    """Every case is tagged live: it has nothing to score offline (no plan block that holds without the system)."""
    cases = list(cases)
    return bool(cases) and all("live" in c.tags for c in cases)


def read_jsonl(path: Path) -> List[EvalCase]:
    cases: List[EvalCase] = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if line.strip() and not line.lstrip().startswith("//"):
                try:
                    cases.append(EvalCase.model_validate_json(line))
                except Exception as exc:  # noqa: BLE001
                    raise ValueError(f"{path}:{n}: invalid case: {exc}") from exc
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"{path}: duplicate case ids {sorted(dupes)}")
    return cases


def _download(url: str, dest: Path, refresh: bool = False, params: Optional[Dict[str, Any]] = None) -> Path:
    if dest.exists() and not refresh:
        return dest
    import httpx

    resp = httpx.get(url, params=params, timeout=120, follow_redirects=True)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


# ---------------------------------------------------------------------------------------------------------------
# PubMedQA (PQA-L): open-domain here, so retrieving the source abstract is measured as well as the label
# ---------------------------------------------------------------------------------------------------------------

def load_pubmedqa(refresh: bool = False) -> List[EvalCase]:
    raw = json.loads(_download(PUBMEDQA_URL, cache_dir() / "pubmedqa_ori_pqal.json", refresh).read_text(encoding="utf-8"))
    cases = []
    for pmid, item in raw.items():
        cases.append(EvalCase(
            id=f"pubmedqa-{pmid}", suite="pubmedqa", category="yesno", query=str(item["QUESTION"]).strip(),
            retrieval=RetrievalExpect(gold_pmids=[str(pmid)], required=False),
            answer=AnswerExpect(answer_type="yesno", label=str(item["final_decision"]).lower(),
                                options=["yes", "no", "maybe"], verified=None),
            reference=str(item.get("LONG_ANSWER") or ""), source="PubMedQA PQA-L (Jin et al. 2019)",
            tags=["benchmark", f"year:{item.get('YEAR')}"],
        ))
    return cases


# ---------------------------------------------------------------------------------------------------------------
# LitQA2 (LAB-Bench): multiple choice with an "insufficient information" escape; graded from the free-text answer
# ---------------------------------------------------------------------------------------------------------------

INSUFFICIENT = "Insufficient information to answer the question"


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            return [str(v) for v in ast.literal_eval(value)]
        except (ValueError, SyntaxError):
            return [value]
    return [str(value)] if value else []


def load_litqa2(refresh: bool = False) -> List[EvalCase]:
    dest = cache_dir() / "litqa2_rows.json"
    if not dest.exists() or refresh:
        import httpx

        rows: List[Dict[str, Any]] = []
        offset = 0
        while True:
            resp = httpx.get(LITQA2_ROWS_URL, timeout=120, params={
                "dataset": "futurehouse/lab-bench", "config": "LitQA2", "split": "train", "offset": offset, "length": 100,
            })
            resp.raise_for_status()
            page = resp.json()
            rows.extend(r["row"] for r in page.get("rows", []))
            offset += 100
            if offset >= int(page.get("num_rows_total") or 0):
                break
        dest.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    cases = []
    for row in json.loads(dest.read_text(encoding="utf-8")):
        ideal = str(row["ideal"])
        dois = [re.sub(r"^https?://(dx\.)?doi\.org/", "", s).lower() for s in _as_list(row.get("sources"))]
        cases.append(EvalCase(
            id=f"litqa2-{row['id']}", suite="litqa2", category="choice", query=str(row["question"]).strip(),
            retrieval=RetrievalExpect(gold_dois=dois, required=False),
            answer=AnswerExpect(answer_type="choice", label=ideal, options=[ideal, *_as_list(row.get("distractors")), INSUFFICIENT],
                                verified=None),
            reference=str(row.get("key-passage") or ""), source="LAB-Bench LitQA2 (FutureHouse 2024)", tags=["benchmark"],
        ))
    return cases


# ---------------------------------------------------------------------------------------------------------------
# BioASQ task B (training or test JSON with golden answers)
# ---------------------------------------------------------------------------------------------------------------

def _pmid_from_url(url: str) -> Optional[str]:
    """The PMID at the end of a PubMed URL ("http://www.ncbi.nlm.nih.gov/pubmed/555"); PMIDs can be short."""
    m = re.search(r"(?<!\d)(\d{1,9})/?$", str(url))
    return m.group(1) if m else None


def _synonym_groups(exact: Any) -> List[List[str]]:
    if isinstance(exact, str):
        return [[exact]]
    groups: List[List[str]] = []
    for item in exact or []:
        groups.append([str(s) for s in item] if isinstance(item, list) else [str(item)])
    return groups


def load_bioasq(path: Optional[str]) -> List[EvalCase]:
    if not path:
        raise ValueError("BioASQ needs --bioasq-file: download a task B JSON from participants-area.bioasq.org (free registration)")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = []
    for q in data.get("questions", []):
        qtype = str(q.get("type", "summary")).lower()
        gold = [p for p in (_pmid_from_url(u) for u in q.get("documents") or []) if p]
        ideal = q.get("ideal_answer")
        ideal = ideal[0] if isinstance(ideal, list) and ideal else ideal
        answer: Dict[str, Any] = {"verified": None}
        if qtype == "yesno":
            answer.update(answer_type="yesno", label=str(q.get("exact_answer", "")).lower(), options=["yes", "no"])
        elif qtype in ("factoid", "list"):
            answer.update(answer_type=qtype, exact=_synonym_groups(q.get("exact_answer")))
        else:
            answer.update(answer_type="summary")
        cases.append(EvalCase(
            id=f"bioasq-{q.get('id')}", suite="bioasq", category=qtype, query=str(q.get("body", "")).strip(),
            retrieval=RetrievalExpect(gold_pmids=gold, k=10, required=False) if gold else None,
            answer=AnswerExpect.model_validate(answer), reference=str(ideal or ""), source=f"BioASQ ({Path(path).name})", tags=["benchmark"],
        ))
    return cases


# ---------------------------------------------------------------------------------------------------------------

def load_suite(name: str, *, bioasq_file: Optional[str] = None, refresh: bool = False) -> List[EvalCase]:
    loaders: Dict[str, Callable[[], List[EvalCase]]] = {
        "pubmedqa": lambda: load_pubmedqa(refresh),
        "litqa2": lambda: load_litqa2(refresh),
        "bioasq": lambda: load_bioasq(bioasq_file),
    }
    if name in loaders:
        return loaders[name]()
    path = Path(name)
    if path.suffix == ".jsonl" and path.exists():
        return read_jsonl(path)
    builtin = DATA_DIR / f"{name}.jsonl"
    if builtin.exists():
        return read_jsonl(builtin)
    raise ValueError(f"Unknown suite '{name}'. Built-in: {', '.join(builtin_suites())}; external: {', '.join(EXTERNAL)}; or a .jsonl path")


def select(
    cases: Iterable[EvalCase],
    *,
    ids: Optional[List[str]] = None,
    categories: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    limit: Optional[int] = None,
    seed: Optional[int] = None,
) -> List[EvalCase]:
    """Filter by id (prefix match), category and tag; then sample `limit` cases (randomly when seed is given)."""
    out = list(cases)
    if ids:
        out = [c for c in out if any(c.id == i or c.id.startswith(i) for i in ids)]
    if categories:
        out = [c for c in out if c.category in categories]
    if tags:
        out = [c for c in out if set(tags) & set(c.tags)]
    if limit is not None and limit < len(out):
        out = random.Random(seed).sample(out, limit) if seed is not None else out[:limit]
    return out


__all__ = ["DATA_DIR", "EXTERNAL", "builtin_suites", "cache_dir", "is_live_suite", "load_suite", "read_jsonl", "select"]
