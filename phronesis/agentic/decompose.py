"""
Question decomposition (feature 3, stage before_evidence): split a compound research question into at most
five ordered sub-questions that the planner (feature 1), the gap loop (feature 2) and claim confidence
(feature 9) work through.

- Adaptive-RAG (Jeong et al., NAACL 2024, arXiv:2403.14403): a complexity check first. A simple single-topic
  question is not decomposed, and no model call is spent on it; only a question with complexity signals
  (several questions, an enumeration, conjoined question clauses, a comparison, a multi-hop chain, several
  entity types or intents, length) goes on.
- Self-Ask (Press et al., Findings EMNLP 2023, arXiv:2210.03350): the model is asked "Are follow-up
  questions needed here?" and writes explicit follow-up questions, each of which can be routed to search.
- Least-to-Most (Zhou et al., ICLR 2023, arXiv:2205.10625): sub-questions are ordered simplest first and a
  later one may depend on earlier ones (depends_on), so their answers feed it.
- Decomposed Prompting (Khot et al., ICLR 2023, arXiv:2210.02406): each sub-question gets a `focus` that
  routes it to a specialist handler (literature, target, simulation, single_cell, clinical, drug, variant,
  structure, omics, general).

The model call uses the prompt marker [phase:decompose] and returns
{"complex": bool, "sub_questions": [{"id", "text", "focus", "depends_on"}]}. When the model is unavailable
(or its reply is unusable) a deterministic splitter is used: question marks, enumerations, ';' and
conjunctions that join distinct question clauses. PHRONESIS_AGENT_DECOMPOSE=off|auto|on (on also asks the
model about questions without complexity signals).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .budget import BudgetExhausted
from .config import env_flag
from .llm import AgentLLMUnavailable
from .runtime import AgentPhase, PhaseOutcome, register_phase
from .state import SubQuestion

logger = logging.getLogger(__name__)

MAX_SUB_QUESTIONS = 5
FOCI = ("literature", "target", "simulation", "single_cell", "clinical", "drug", "variant", "structure", "omics", "general")

_FOCUS_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("simulation", re.compile(r"\b(simulat\w*|kinetic\w*|ode|time[- ]course|steady[- ]state|parameter scan|biomodel\w*|sbml|dynamics)\b", re.I)),
    ("single_cell", re.compile(r"\b(single[- ]cell|scrna\w*|cell types?|clusters?|marker genes?|h5ad)\b", re.I)),
    ("structure", re.compile(r"\b(structur\w*|alphafold|pdb|docking|dock|binding (?:site|pocket)|plddt|fold)\b", re.I)),
    ("variant", re.compile(r"\b(mutation\w*|variant\w*|pathogenic\w*|rs\d+|clinvar|[A-Z]\d{1,4}[A-Z])\b")),
    ("clinical", re.compile(r"\b(trials?|clinical\w*|patients?|phase [1-4i]+|recruit\w*|nct\d+)\b", re.I)),
    ("drug", re.compile(r"\b(drugs?|inhibitors?|compounds?|mechanisms?(?: of action)?|potency|ic50|side effects?|adverse|safety|toxicity|dosing)\b", re.I)),
    ("omics", re.compile(r"\b(gwas|expression|tissues?|gtex|interaction\w*|network|string|reactome|eqtl)\b", re.I)),
    ("target", re.compile(r"\b(targets?|associat\w*|genes?|pathways?|druggab\w*|tractab\w*|signaling)\b", re.I)),
    ("literature", re.compile(r"\b(papers?|literature|publications?|studies|study|evidence|reported|review)\b", re.I)),
]

_QWORD = r"(?:what|how|which|whether|is|are|does|do|did|can|could|why|where|when|who|should|will)"
_CONJ_SPLIT = re.compile(r"\s*(?:,\s*(?:and\s+)?|\s+and\s+(?:also\s+)?|\s+also\s+)(?=" + _QWORD + r"\b)", re.I)
_ENUM_SPLIT = re.compile(r"(?:^|\s)(?:\(?\d{1,2}[.)]|\([a-hA-H]\)|[a-h]\))\s+")
_ANAPHORA = re.compile(r"\b(it|its|they|their|them|this|these|that|those|such|the same)\b", re.I)
_COMPARE = re.compile(r"\b(compare\w*|comparison|versus|vs\.?|differ\w*|difference)\b", re.I)
_CHAIN = re.compile(r"\b(and then|then|which in turn|downstream|leading to|as a result|consequently|and how (?:does|do) (?:that|this|it))\b", re.I)
_LEAD_JUNK = re.compile(r"^(?:and|also|then|so|plus|additionally|finally)\b[\s,]*", re.I)

# Output instructions ("Formatting: use Markdown tables, cite every claim, keep under 1500 words, state today's
# date") shape the final answer; they are not research questions and never become sub-questions.
_FORMAT_HEAD = re.compile(
    r"(?:^|\n|(?<=[.!?])\s)\s*(?:output\s+)?(?:format(?:ting)?|style|presentation|answer format|"
    r"formatting requirements|response requirements|output requirements|instructions)\s*:", re.I)
_FORMAT_CUE = re.compile(
    r"\bmarkdown\b|\bbullet(?:ed)? (?:points?|list)|\bnumbered list\b|\bheadings?\b|\bbold\b|"
    r"\b(?:under|within|at most|no more than|max(?:imum)?(?: of)?)\s+\d[\d,]*\s+words\b|\bword (?:limit|count)\b|"
    r"\bcite (?:every|each|all)\b|\bcitations? (?:for|after) (?:every|each)\b|\breferences? (?:section|list)\b|"
    r"\b(?:state|give|include|put|write|show)\b[^.;\n]{0,30}\b(?:current|today'?s|utc|retrieval) (?:utc )?date\b|"
    r"\bdate (?:at the top|of retrieval)\b|\bexecutive summary\b|\bplain language\b",
    re.I)
# Sub-questions that only ask for the date or for the answer's format.
_META_SUBQ = re.compile(
    r"\b(?:current|today'?s|utc|retrieval)\s+(?:utc\s+)?date\b|\bwhat(?: is|'s)? (?:the )?date\b|\bwhat day is\b|"
    r"\bword (?:limit|count)\b|\bformat(?:ting)?\b|\bmarkdown\b|\bcitation style\b",
    re.I)

DECOMPOSE_SYSTEM = """[phase:decompose]
You decompose biomedical research questions for a research agent. Today's date is given in the request; never write a sub-question about the date or about the answer's format, length or citation style.
First decide: are follow-up questions needed here? A simple, single-topic question (one entity and one thing asked about it) is NOT complex: answer {"complex": false, "sub_questions": []}.
If the question is compound or multi-hop, write 2 to 5 sub-questions:
- order them from the simplest to the most dependent (least-to-most); a later sub-question may depend on earlier ones (depends_on lists their ids);
- each must be answerable on its own by one kind of search, keep the user's entities verbatim, and together they must cover the whole question;
- give each a focus from: literature, target, simulation, single_cell, clinical, drug, variant, structure, omics, general;
- ANAPHORA & ENTITY INTEGRITY: Resolve anaphoric phrases ('this cascade', 'this pathway', 'this target', 'it', 'they') strictly to the antecedent named in the user question itself (e.g. resolve 'this cascade' to 'the MAPK/ERK pathway'). NEVER invent, substitute, or guess an unmentioned drug, gene, or compound (such as 'osimertinib', 'sotorasib', etc.) not present in the user question!
Reply with only JSON: {"complex": true|false, "sub_questions": [{"id": "q1", "text": "...", "focus": "...", "depends_on": []}]}"""


def infer_focus(text: str) -> str:
    for focus, pat in _FOCUS_PATTERNS:
        if pat.search(text or ""):
            return focus
    return "general"


def strip_output_instructions(query: str) -> Tuple[str, List[str]]:
    """(the research question, the output instructions removed from it). A "Formatting:" (or "Style:",
    "Instructions:") block is removed to the end of its line, then each remaining sentence that is only an output
    instruction (Markdown, word limits, "cite every claim", "state today's date") and names no research subject."""
    text = query or ""
    removed: List[str] = []
    while True:
        m = _FORMAT_HEAD.search(text)
        if not m:
            break
        end = text.find("\n", m.end())
        end = len(text) if end < 0 else end
        removed.append(text[m.start():end].strip())
        text = (text[:m.start()] + text[end:]).strip()
    kept: List[str] = []
    for line in text.split("\n"):
        out = []
        for sent in re.split(r"(?<=[.!?])\s+", line):
            if sent.strip() and _FORMAT_CUE.search(sent) and infer_focus(_FORMAT_CUE.sub(" ", sent)) == "general":
                removed.append(sent.strip())
                continue
            out.append(sent)
        kept.append(" ".join(out))
    cleaned = re.sub(r"[ \t]+", " ", "\n".join(kept)).strip()
    return (cleaned or (query or "").strip()), [r for r in removed if r]


def is_meta_sub_question(text: str) -> bool:
    """A sub-question that asks for the date or the answer's format, which no search answers."""
    return bool(_META_SUBQ.search(text or "")) and infer_focus(_META_SUBQ.sub(" ", text or "")) == "general"


def _entity_tokens(text: str) -> List[str]:
    """Upper-case names a sub-question uses (KRAS, SHP2), without trailing hyphens ("FDA-") or pure numbers."""
    out: List[str] = []
    for raw in re.findall(r"\b[A-Z][A-Z0-9\-]{3,}\b", text or ""):
        tok = raw.strip("-")
        if len(tok) >= 4 and not tok.isdigit() and tok not in out:
            out.append(tok)
    return out


def _word_set(query: str) -> set:
    """The question's words in lower case, with each hyphenated word's parts ("fda-approved" -> fda, approved)."""
    words = set(re.findall(r"[a-z0-9\-]+", (query or "").lower()))
    for w in list(words):
        words.update(p for p in w.split("-") if p)
        words.add(w.strip("-"))
    return words


def complexity_signals(query: str, ctx: Any = None) -> List[str]:
    """Adaptive-RAG-style complexity cues; an empty list means a simple question."""
    q = (query or "").strip()
    signals: List[str] = []
    questions = [p for p in re.split(r"\?", q) if len(p.split()) >= 2]
    if len(questions) >= 2:
        signals.append("several questions")
    if len(_ENUM_SPLIT.findall(" " + q)) >= 2 or q.count(";") >= 1:
        signals.append("enumeration")
    if _CONJ_SPLIT.search(q):
        signals.append("conjoined question clauses")
    if _COMPARE.search(q):
        signals.append("comparison")
    if _CHAIN.search(q):
        signals.append("multi-hop chain")
    if ctx is not None:
        entities = list(getattr(ctx, "entities", []) or [])
        types = {getattr(e, "type", "") for e in entities}
        if len(entities) >= 3 and len(types) >= 2:
            signals.append("several entity types")
        if len(getattr(ctx, "intents", set()) or set()) >= 3:
            signals.append("several intents")
    if len(q.split()) > 35:
        signals.append("long question")
    return signals


def _is_title(head: str) -> bool:
    """A short lead-in that states a subject rather than asking something ("Research brief: KRAS in CRC.")."""
    h = head.strip()
    return bool(h) and len(h.split()) <= 14 and not h.endswith("?") and not re.match(_QWORD + r"\b", h, re.I)


def _with_subject(item: str, subject: str) -> str:
    item = item.strip().rstrip(".;")
    if not subject or any(t.lower() in item.lower() for t in _entity_tokens(subject) or [subject]):
        return item
    if item.endswith("?"):
        return f"{item[:-1].rstrip()} ({subject})?"
    return f"{item} ({subject})"


def _titled_enumeration(text: str, max_sub_questions: int) -> Optional[List[str]]:
    """Sub-questions of "Title: subject. 1) ... 2) ... 3) ..." (or a bare "1) ... 2) ..."), the items split across
    question marks too; None when the text is not such an enumeration."""
    spaced = " " + text
    markers = list(_ENUM_SPLIT.finditer(spaced))
    if len(markers) < 2:
        return None
    head = spaced[: markers[0].start()].strip()
    if head and (not _is_title(head) or (len(head.split()) <= 6 and head.endswith(":"))):
        return None  # a question or a "Tell me:" lead-in; handled clause by clause
    subject = head.rstrip(" .:").split(":", 1)[-1].strip() if head else ""
    out: List[str] = []
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(spaced)
        item = spaced[m.end():end].strip()
        for sub in heuristic_split(item, max_sub_questions=max_sub_questions) or ([item] if len(item.split()) >= 3 else []):
            sub = _with_subject(sub, subject)
            if sub.lower() not in {o.lower() for o in out}:
                out.append(sub)
    return out[:max_sub_questions]


def heuristic_split(query: str, max_sub_questions: int = MAX_SUB_QUESTIONS) -> List[str]:
    """Deterministic decomposition: question marks, ';', enumerations, and conjunctions joining question clauses."""
    text = re.sub(r"\s+", " ", (query or "").strip())
    if not text:
        return []
    titled = _titled_enumeration(text, max_sub_questions)
    if titled is not None:
        return titled
    pieces: List[Tuple[str, bool]] = []
    for m in re.finditer(r"[^?;]+[?;]?", text):
        chunk = m.group(0).strip()
        if chunk:
            pieces.append((chunk.rstrip("?;").strip(), chunk.endswith("?")))
    parts: List[Tuple[str, bool]] = []
    for chunk, is_q in pieces:
        enum = [p for p in _ENUM_SPLIT.split(" " + chunk) if p and p.strip()]
        if len(enum) >= 2:
            head = enum[0].strip()
            items = enum[1:]
            # "Tell me 1) X 2) Y": the lead-in is context for each item, not a sub-question of its own.
            if len(head.split()) <= 6 and head.endswith(":"):
                head = head.rstrip(":").strip()
                parts.extend(((f"{head} {it.strip()}" if head else it.strip()), is_q) for it in items)
            else:
                parts.extend((p.strip(), is_q) for p in enum)
            continue
        clauses = [c for c in _CONJ_SPLIT.split(chunk) if c and c.strip()]
        parts.extend((c.strip(), is_q or len(clauses) > 1) for c in clauses)
    out: List[str] = []
    seen = set()
    for part, is_q in parts:
        part = _LEAD_JUNK.sub("", part).strip(" ,.:")
        if len(part.split()) < 3:
            continue
        part = part[0].upper() + part[1:]
        if is_q or re.match(_QWORD + r"\b", part, re.I):
            part = part.rstrip(".") + "?"
        key = part.lower()
        if key not in seen:
            seen.add(key)
            out.append(part)
    return out[:max_sub_questions]


def _from_texts(texts: List[str], max_sub_questions: int = MAX_SUB_QUESTIONS) -> List[SubQuestion]:
    subs: List[SubQuestion] = []
    for i, t in enumerate(texts[:max_sub_questions], 1):
        deps = [subs[-1].id] if subs and _ANAPHORA.search(t) else []
        subs.append(SubQuestion(id=f"q{i}", text=t[:400], focus=infer_focus(t), depends_on=deps))
    return subs


def parse_decomposition(value: Any, query: Optional[str] = None, max_sub_questions: int = MAX_SUB_QUESTIONS) -> Tuple[Optional[bool], List[SubQuestion]]:
    """(complex, sub-questions) from the model reply; ids are renumbered q1.. and depends_on only points back."""
    if not isinstance(value, dict):
        return None, []
    flag = value.get("complex")
    complex_flag = flag if isinstance(flag, bool) else None
    raw = value.get("sub_questions") or value.get("subquestions") or []
    if not isinstance(raw, list):
        return complex_flag, []
    subs: List[SubQuestion] = []
    id_map: Dict[str, str] = {}
    seen = set()
    query_words = _word_set(query) if query else set()
    for item in raw:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = re.sub(r"\s+", " ", str(item.get("text") or item.get("question") or "")).strip()
        if len(text.split()) < 2 or text.lower() in seen or is_meta_sub_question(text):
            continue
        # Anti-hallucination entity check: a sub-question naming an entity the question does not (a substituted
        # drug or gene) is dropped, not replaced by a clause of the question picked by position, which could be a
        # different part of the question.
        if query_words:
            from ..drugintel.names import extract_drug_mentions

            named = _entity_tokens(text) + extract_drug_mentions(text)
            hallucinated = [w for w in named if w.lower() not in query_words]
            if hallucinated:
                logger.info("Decomposition sub-question names unmentioned entity %s; dropped: %s", hallucinated, text)
                continue
        seen.add(text.lower())
        new_id = f"q{len(subs) + 1}"
        if item.get("id") is not None:
            id_map[str(item["id"]).strip()] = new_id
        focus = str(item.get("focus") or "").strip().lower().replace("-", "_").replace(" ", "_")
        if focus not in FOCI:
            focus = infer_focus(text)
        deps_raw = item.get("depends_on") or []
        if isinstance(deps_raw, (str, int)):
            deps_raw = [deps_raw]
        deps = []
        for d in deps_raw if isinstance(deps_raw, list) else []:
            mapped = id_map.get(str(d).strip())
            if mapped and mapped != new_id and mapped not in deps:
                deps.append(mapped)  # only earlier sub-questions: least-to-most order, no cycles
        subs.append(SubQuestion(id=new_id, text=text[:400], focus=focus, depends_on=deps))
        if len(subs) >= max_sub_questions:
            break
    return complex_flag, subs


class DecomposePhase(AgentPhase):
    key = "decompose"
    stage = "before_evidence"
    order = 30
    frameworks = None  # every agentic framework
    needs_llm = False  # the deterministic splitter runs without a model

    def applies(self, state: Any) -> bool:
        if env_flag("PHRONESIS_AGENT_DECOMPOSE", "auto") == "off":
            return False
        if state.sub_questions:
            return False
        return getattr(state.ctx, "scope", "research") == "research"

    async def run(self, state: Any) -> Optional[PhaseOutcome]:
        query, instructions = strip_output_instructions(state.clean_query or state.query)
        signals = complexity_signals(query, state.ctx)
        info: Dict[str, Any] = {"signals": signals, "method": "none", "complex": False}
        if instructions:
            info["output_instructions"] = [i[:200] for i in instructions[:6]]  # for the answer, never sub-questions
        mode = env_flag("PHRONESIS_AGENT_DECOMPOSE", "auto")
        if not signals and mode != "on":
            info["method"] = "complexity_check"
            state.bag["decomposition"] = info
            return None

        from .config import get_effort_profile
        effort_level = getattr(state, "effort_level", None) or getattr(state.ctx, "effort_level", "medium")
        profile = get_effort_profile(effort_level)
        max_sub = int(profile.get("max_subqueries", MAX_SUB_QUESTIONS))

        subs: List[SubQuestion] = []
        complex_flag: Optional[bool] = None
        used_model = False
        if state.llm.available:
            from ..agents.prompts import utc_date_line

            user = (f"{utc_date_line()}\n<question>{query}</question>\n"
                    f"<complexity_signals>{', '.join(signals) or 'none'}</complexity_signals>")
            system = DECOMPOSE_SYSTEM.replace("write 2 to 5 sub-questions", f"write 2 to {max(2, max_sub)} sub-questions")
            try:
                reply = await state.llm.json(system, user, phase="decompose")
                complex_flag, subs = parse_decomposition(reply, query=query, max_sub_questions=max_sub)
                used_model = complex_flag is not None or bool(subs)
            except (ValueError, BudgetExhausted, AgentLLMUnavailable) as exc:
                info["model_error"] = str(exc)[:200]
            except Exception as exc:  # noqa: BLE001
                logger.warning("Decomposition model call failed: %s", exc)
                info["model_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"

        if used_model:
            info["method"] = "llm"
            if complex_flag is False or len(subs) < 2:
                subs = []
        else:
            info["method"] = "heuristic"
            texts = [t for t in heuristic_split(query, max_sub_questions=max_sub + 2) if not is_meta_sub_question(t)]
            subs = _from_texts(texts, max_sub_questions=max_sub)
            if len(subs) < 2:
                subs = []
        info["complex"] = bool(subs)
        state.sub_questions = subs
        state.bag["decomposition"] = info
        if subs:
            await state.step(
                "agent_decompose",
                f"Split the question into {len(subs)} sub-questions",
                "\n".join(f"{q.id}. {q.text}" + (f" (after {', '.join(q.depends_on)})" if q.depends_on else "") for q in subs),
                status="done",
                agent="Planner",
                method=info["method"],
                sub_questions=[q.to_dict() for q in subs],
            )
        return None


register_phase(DecomposePhase())

__all__ = [
    "DECOMPOSE_SYSTEM",
    "DecomposePhase",
    "FOCI",
    "MAX_SUB_QUESTIONS",
    "complexity_signals",
    "heuristic_split",
    "infer_focus",
    "is_meta_sub_question",
    "parse_decomposition",
    "strip_output_instructions",
]
