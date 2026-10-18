"""
Clinical Memory Engine for Phronesis, inspired by Mem0 (2025).
Provides durable, isolated, and evidence-backed persistent memory:
- Explicit separation of memory types: User Instructions, Query History, Hypotheses, Retrieved Evidence,
  Computed Simulation Results, and Reflexion Failure Lessons
- Verification status tracking: VERIFIED_EMPIRICAL, PROVISIONAL, REFUTED, UNVERIFIED_GENERATED
- True BM25 probabilistic retrieval with length normalization and IDF term weighting
- Sentence-level contradiction detection, audit logging, and resolution
- Strict user and investigation/thread isolation: every read, delete and clear is scoped to one user
- Scoped user instructions (current_turn | investigation | profile); past questions are history, not directives
- Selective consolidation that NEVER promotes raw unverified model answers into empirical evidence
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import sqlite3
import time
import uuid
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple
from pydantic import BaseModel, Field

from ...extensions.hooks import apply_serialization_filters
from ...extensions.id_families import extract_family_ids, family_names

logger = logging.getLogger(__name__)

_LEGACY_ID_KEYS = ("biomodels", "pmids", "dois", "ensembl", "uniprot")

DEFAULT_USER_ID = "default_user"
INSTRUCTION_SCOPES = ("current_turn", "investigation", "profile")
SUBJECT_KEYS = ("gene", "drug", "disease", "biomodel_id")

# Recall caps: the injected memory block stays bounded however long a thread grows.
MAX_INSTRUCTIONS = 10
MAX_QUERY_HISTORY = 5
MAX_EVIDENCE = 4
MAX_COMPUTED = 3
MAX_LESSONS = 3
MAX_HYPOTHESES = 4

_BUSY_TIMEOUT_S = 5.0
_SUMMARY_MAX = 480
_ASSERTION_MAX = 2000

# Canonical biomedical entity detection patterns. Case-sensitive: "met", "ret", "atm" and "apc" are English words.
KNOWN_TARGET_PATTERN = re.compile(
    r"\b(KRAS|EGFR|HER2|ERBB2|BRAF|TP53|PIK3CA|MYC|BRCA1|BRCA2|ALK|ROS1|RET|MET|PTEN|APC|CDK4|CDK6|ATM|ATR|IDH1|IDH2|FGFR1|FGFR2|FGFR3|PDCD1|CD274|CTLA4|AXL|VIM|CDH1|CDH2|MAP2K1|RAF1|SOS1|SHP2)\b"
)
KNOWN_BIOMODEL_PATTERN = re.compile(r"\b(BIOMD\d{10}|MODEL\d{10})\b")
KNOWN_DISEASE_PATTERN = re.compile(
    r"\b(pancreatic ductal adenocarcinoma|pancreatic cancer|non-small cell lung cancer|nsclc|colorectal cancer|crc|melanoma|breast cancer|glioblastoma|leukemia|crohn(?:'s)? disease|ulcerative colitis|ibd|noonan syndrome)\b",
    re.IGNORECASE,
)

# Instruction scope: only explicitly scoped instructions outlive the turn that gave them.
_PROFILE_SCOPE_RE = re.compile(
    r"\b(?:always (?:use|include|cite|report|give|show|answer|respond|exclude|avoid|list|provide|search|run)"
    r"|in (?:all|every) (?:of my )?(?:future |later )?(?:answers?|responses?|investigations?|questions?|threads?|sessions?)"
    r"|for all (?:of )?(?:my )?(?:future )?(?:answers?|questions?|investigations?|threads?|sessions?))\b",
    re.IGNORECASE,
)
_INVESTIGATION_SCOPE_RE = re.compile(
    r"\b(?:from now on|going forward|for the rest of (?:the|this) (?:investigation|thread|session|conversation)"
    r"|for this (?:whole |entire )?(?:investigation|thread|session|conversation)"
    r"|throughout this (?:investigation|thread|session|conversation)"
    r"|keep (?:using )?(?:these|those|the same|the earlier|earlier|my earlier|the previous|previous) "
    r"(?:settings|parameters|constraints|instructions|filters))\b",
    re.IGNORECASE,
)
_DIRECTIVE_RE = re.compile(
    r"\b(?:only|do not|don't|never|without|avoid|exclude|must|use only|restrict|limit (?:to|the))\b", re.IGNORECASE
)
# Not utils.text.sentence_spans on purpose: ';' also splits, so "only use human data; from now on exclude preprints" is
# two instructions with their own scopes, and each clause is its own context for contradiction checks.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_LEGACY_GOAL_PREFIX = "Research Goal: "

# Contradiction detection: opposite polarity about the same entity, in sentences that share their context.
_OPPOSING_TERMS: Tuple[Tuple[str, str], ...] = (
    ("sensitive", "resistant"),
    ("responsive", "resistant"),
    ("curative", "refractory"),
    ("inhibits", "fails to inhibit"),
    ("effective", "ineffective"),
)
_POLARITY_WORDS = {w for pair in _OPPOSING_TERMS for term in pair for w in term.split()}
_NEGATION_RE = r"(?:\bnot|\bno longer|n't)\s+(?:\w+\s+)?"
_STOPWORDS = {
    "the", "and", "for", "with", "through", "from", "that", "this", "was", "were", "are", "has", "have",
    "into", "via", "its", "their", "than", "then", "but", "not", "also", "after", "before", "under",
}
_CONTEXT_OVERLAP_MIN = 0.6
_SKIP_SUMMARY_KEY_RE = re.compile(r"(plot|base64|png|svg|html|path|raw)", re.IGNORECASE)


def _require_user(user_id: Optional[str]) -> str:
    """Every memory read, delete and clear is scoped to one user; there is no global fallback."""
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id is required for memory access")
    return user_id


class MemoryType(str, Enum):
    USER_INSTRUCTION = "USER_INSTRUCTION"        # Explicit user directives, stored with a scope
    QUERY_HISTORY = "QUERY_HISTORY"              # Questions asked earlier; history, never directives
    WORKING_HYPOTHESIS = "WORKING_HYPOTHESIS"    # Investigative hypotheses undergoing testing
    RETRIEVED_EVIDENCE = "RETRIEVED_EVIDENCE"    # Verified external literature, OpenTargets scores
    COMPUTED_RESULT = "COMPUTED_RESULT"          # COPASI ODE flux, Scanpy cluster marker outputs
    FAILURE_LESSON = "FAILURE_LESSON"            # Reflexion defect audits and self-correction lessons
    DIALOGUE_TURN = "DIALOGUE_TURN"              # Conversational context turn


class VerificationStatus(str, Enum):
    VERIFIED_EMPIRICAL = "VERIFIED_EMPIRICAL"    # Grounded in real tool outputs or peer-reviewed PMIDs
    PROVISIONAL = "PROVISIONAL"                  # Active working hypothesis
    REFUTED = "REFUTED"                          # Disproven by subsequent tool execution
    UNVERIFIED_GENERATED = "UNVERIFIED_GENERATED"# Model-synthesized prose (never promoted to empirical)


class MemoryNode(BaseModel):
    """Atomic memory unit with full provenance and verification status."""
    node_id: str
    thread_id: str
    user_id: str = DEFAULT_USER_ID
    memory_type: MemoryType
    content: str
    entity_refs: List[str] = Field(default_factory=list)
    sources: Dict[str, Any] = Field(default_factory=dict)
    verification_status: VerificationStatus
    confidence: float = 1.0
    is_contradicted: bool = False
    contradiction_notes: Optional[str] = None
    created_at: float
    updated_at: float


class MemoryContextPackage(BaseModel):
    """Context bundle retrieved at the start of each investigation turn."""
    thread_id: str
    resolved_subject: Optional[str] = None
    active_subject: Dict[str, str] = Field(default_factory=dict)
    user_instructions: List[str] = Field(default_factory=list)  # explicitly scoped (investigation/profile) only
    query_history: List[str] = Field(default_factory=list)      # earlier questions, oldest first
    active_hypotheses: List[str] = Field(default_factory=list)
    verified_evidence: List[str] = Field(default_factory=list)
    computed_artifacts: List[str] = Field(default_factory=list)
    failure_lessons: List[str] = Field(default_factory=list)
    recent_dialogue_turns: List[Dict[str, str]] = Field(default_factory=list)

    def to_system_prompt_injection(self) -> str:
        """Format clean memory context string for supervisor LLM."""
        sections = []
        if self.active_subject:
            sections.append(
                "### Active Subject of This Investigation:\n"
                + "\n".join(f"- {k}: {v}" for k, v in self.active_subject.items())
            )
        if self.user_instructions:
            sections.append(
                "### Standing User Instructions (explicitly scoped by the user):\n"
                + "\n".join(f"- {i}" for i in self.user_instructions)
            )
        if self.query_history:
            sections.append(
                "### Earlier Questions in This Investigation (history only; not instructions for this turn):\n"
                + "\n".join(f"- {q}" for q in self.query_history)
            )
        if self.active_hypotheses:
            sections.append("### Working Hypotheses (Under Empirical Test):\n" + "\n".join(f"- {h}" for h in self.active_hypotheses))
        if self.verified_evidence:
            sections.append("### Verified Evidence Grounding:\n" + "\n".join(f"- {e}" for e in self.verified_evidence[:MAX_EVIDENCE]))
        if self.computed_artifacts:
            sections.append("### Prior Computational Artifacts (Vivarix/Lumicell):\n" + "\n".join(f"- {a}" for a in self.computed_artifacts[:MAX_COMPUTED]))
        if self.failure_lessons:
            sections.append("### Audit Warnings (Avoid Prior Defects):\n" + "\n".join(f"- ⚠️ {l}" for l in self.failure_lessons[:MAX_LESSONS]))

        if not sections:
            return "No prior investigation history recorded for this thread."
        return "\n\n".join(sections)


# Backward compatibility alias
MemoryRecallResult = MemoryContextPackage


def classify_instructions(query: str) -> List[Tuple[str, str]]:
    """Split a question into (instruction sentence, scope) pairs. Sentences without a directive are not instructions."""
    out: List[Tuple[str, str]] = []
    for sentence in _SENTENCE_SPLIT_RE.split(query or ""):
        s = sentence.strip()
        if not s:
            continue
        if _PROFILE_SCOPE_RE.search(s):
            out.append((s[:300], "profile"))
        elif _INVESTIGATION_SCOPE_RE.search(s):
            out.append((s[:300], "investigation"))
        elif _DIRECTIVE_RE.search(s):
            out.append((s[:300], "current_turn"))
    return out


def _fmt_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, str):
        return " ".join(value.split())[:160]
    return str(value)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (int, float, bool)) or (isinstance(value, str) and len(value) <= 160)


def _tool_text(data: Any) -> str:
    if isinstance(data, str):
        return data
    try:
        text = json.dumps(data, default=str)
    except (TypeError, ValueError):
        text = str(data)
    return re.sub(r'"plot_base64_png":\s*"[^"]*"', "", text)


def summarize_tool_data(data: Any, tool_key: str = "") -> str:
    """A structured, bounded summary of a tool output: scalar fields, small mappings and identifiers."""
    if isinstance(data, str):
        return " ".join(data.split())[:_SUMMARY_MAX]
    if not isinstance(data, dict):
        return " ".join(str(data).split())[:_SUMMARY_MAX]
    parts: List[str] = []
    for key, value in data.items():
        if _SKIP_SUMMARY_KEY_RE.search(str(key)) or value in (None, "", [], {}):
            continue
        if _is_scalar(value):
            parts.append(f"{key}={_fmt_value(value)}")
        elif isinstance(value, dict):
            inner = [
                f"{ik}={_fmt_value(iv)}"
                for ik, iv in value.items()
                if _is_scalar(iv) and not _SKIP_SUMMARY_KEY_RE.search(str(ik))
            ][:6]
            if inner:
                parts.append(f"{key}{{{', '.join(inner)}}}")
        elif isinstance(value, list):
            titles = [
                _fmt_value(item.get("title") or item.get("name"))[:80]
                for item in value[:2]
                if isinstance(item, dict) and isinstance(item.get("title") or item.get("name"), str)
            ]
            parts.append(f"{key}[{len(value)}" + (f": {'; '.join(titles)}" if titles else "") + "]")
    text = "; ".join(parts)
    try:
        ids = ClinicalMemoryEngine.collect_identifiers(data, tool_key)
    except Exception:  # noqa: BLE001 - a summary must never fail consolidation
        ids = {}
    id_bits = [f"{family}:{value}" for family, values in ids.items() for value in values][:8]
    if id_bits:
        text = f"{text} | ids: {', '.join(id_bits)}" if text else f"ids: {', '.join(id_bits)}"
    return text[:_SUMMARY_MAX]


def _has_term(sentence: str, term: str) -> bool:
    """The polarity term occurs and is not itself negated ("not sensitive")."""
    for m in re.finditer(rf"\b{re.escape(term)}\b", sentence, re.IGNORECASE):
        if not re.search(_NEGATION_RE + r"$", sentence[: m.start()], re.IGNORECASE):
            return True
    return False


def _context_tokens(sentence: str) -> Set[str]:
    return {
        w.lower()
        for w in re.findall(r"[A-Za-z0-9]+", sentence)
        if len(w) > 2 and w.lower() not in _STOPWORDS and w.lower() not in _POLARITY_WORDS
    }


def _sentences_about(text: str, entity: str) -> List[str]:
    pattern = re.compile(rf"\b{re.escape(entity)}\b")
    return [s for s in _SENTENCE_SPLIT_RE.split(text or "") if pattern.search(s)]


def assertions_conflict(prior: str, new: str, entity: str) -> bool:
    """Two statements about `entity` take opposite polarity in sentences that share most of their context."""
    for p in _sentences_about(prior, entity):
        for n in _sentences_about(new, entity):
            for a, b in _OPPOSING_TERMS:
                flipped = (_has_term(p, a) and _has_term(n, b) and not _has_term(p, b) and not _has_term(n, a)) or (
                    _has_term(p, b) and _has_term(n, a) and not _has_term(p, a) and not _has_term(n, b)
                )
                if not flipped:
                    continue
                pt, nt = _context_tokens(p), _context_tokens(n)
                shared = pt & nt
                if len(shared) >= 2 and len(shared) / max(1, min(len(pt), len(nt))) >= _CONTEXT_OVERLAP_MIN:
                    return True
    return False


def _strip_label(content: str) -> str:
    """Stored evidence is '<label>: <body>'; contradiction checks read the body only."""
    _, sep, body = (content or "").partition(": ")
    return body if sep else content or ""


def _clean_subject(subject: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not isinstance(subject, dict):
        return {}
    return {
        k: " ".join(str(subject[k]).split())[:120]
        for k in SUBJECT_KEYS
        if isinstance(subject.get(k), str) and subject[k].strip()
    }


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, (list, tuple, set)):
        return len(value)
    return 0


def unresolved_claim_count(verification: Optional[Dict[str, Any]]) -> Optional[int]:
    """Unsupported + contradicted + unchecked claims in a verification report (or its counts); None when unknown."""
    if not isinstance(verification, dict):
        return None
    keys = ("unsupported_claims", "contradicted_claims", "unchecked_claims")
    if not any(k in verification for k in keys):
        return None
    return sum(_count(verification.get(k)) for k in keys)


def turn_fully_verified(is_turn_verified: bool, verification: Optional[Dict[str, Any]] = None) -> bool:
    """Whether a turn's tool evidence may be stored as verified: the turn passed AND, when its report is given, it
    left no unsupported, contradicted or unchecked claims (a report's ``passed`` alone tolerates unsupported claims)."""
    if not is_turn_verified:
        return False
    if not isinstance(verification, dict):
        return True
    if verification.get("passed") is False or verification.get("fully_verified") is False:
        return False
    unresolved = unresolved_claim_count(verification)
    return not unresolved


def _sources_of(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        value = json.loads(row["sources_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class ClinicalMemoryEngine:
    """
    Durable, isolated, Mem0-grade memory vault for Phronesis.

    The synchronous methods are safe to call from worker threads (one connection per call); the `a*`
    coroutines run them via asyncio.to_thread so event-loop callers do not block on SQLite.
    """

    def __init__(self, storage_dir: Optional[str] = None) -> None:
        if storage_dir is None:
            storage_dir = os.getenv("PHRONESIS_MEMORY_DIR") or None
        if storage_dir is None:
            from phronesis.utils.paths import data_dir

            root = data_dir("memory")
            storage_dir = str(root) if root is not None else None
        if storage_dir is None:
            workspace_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
            storage_dir = os.path.join(workspace_root, ".phronesis_memory")

        self.storage_dir = storage_dir
        os.makedirs(self.storage_dir, exist_ok=True)
        self.db_path = os.path.join(self.storage_dir, "clinical_memory_v2.db")
        self._init_sqlite_db()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One connection per operation: committed on success, rolled back on error, always closed."""
        with contextlib.closing(sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_S, check_same_thread=False)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {int(_BUSY_TIMEOUT_S * 1000)}")
            with conn:
                yield conn

    def _init_sqlite_db(self) -> None:
        """Create structured Mem0 schema tables."""
        with self._connect() as conn:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError as exc:  # e.g. a filesystem without shared-memory support
                logger.info("SQLite WAL mode unavailable for memory store: %s", exc)
            cursor = conn.cursor()

            # 1. Structured Memory Nodes
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_nodes (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    entity_refs_json TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    verification_status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    is_contradicted INTEGER DEFAULT 0,
                    contradiction_notes TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mem_thread_type ON memory_nodes(thread_id, memory_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mem_user_thread ON memory_nodes(user_id, thread_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mem_status ON memory_nodes(verification_status)")

            # 2. Contradiction Tracking & Provenance Log
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS contradictions (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    entity TEXT NOT NULL,
                    prior_assertion TEXT NOT NULL,
                    new_assertion TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

            # 3. Active subject per user and thread, for resolving follow-ups ("What about its safety?")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS active_subjects (
                    user_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    subject_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (user_id, thread_id)
                )
                """
            )

    # -------------------------------------------------------------------------
    # ENTITY EXTRACTION
    # -------------------------------------------------------------------------
    @staticmethod
    def extract_entities(text: str) -> Dict[str, List[str]]:
        entities: Dict[str, List[str]] = {
            "targets": [],
            "biomodels": [],
            "diseases": [],
        }
        for m in KNOWN_TARGET_PATTERN.finditer(text):
            val = m.group(1)
            if val not in entities["targets"]:
                entities["targets"].append(val)
        for m in KNOWN_BIOMODEL_PATTERN.finditer(text):
            val = m.group(1).upper()
            if val not in entities["biomodels"]:
                entities["biomodels"].append(val)
        for m in KNOWN_DISEASE_PATTERN.finditer(text):
            val = m.group(1).strip().title()
            if val not in entities["diseases"]:
                entities["diseases"].append(val)
        return entities

    # -------------------------------------------------------------------------
    # PROBABILISTIC BM25 EVALUATION RETRIEVAL (No Keyword Counting Mock)
    # -------------------------------------------------------------------------
    def _bm25_score(self, query_terms: List[str], doc_tokens: List[str], df_counts: Dict[str, int], total_docs: int, avg_doc_len: float) -> float:
        """
        True Okapi BM25 scoring with document length normalization and inverse document frequency.
        """
        k1 = 1.2
        b = 0.75
        doc_len = len(doc_tokens)
        score = 0.0

        for term in query_terms:
            if term not in doc_tokens:
                continue
            tf = doc_tokens.count(term)
            df = df_counts.get(term, 1)
            # Standard Lucene/BM25 IDF
            idf = math.log(1.0 + (total_docs - df + 0.5) / (df + 0.5))
            term_score = idf * ((tf * (k1 + 1.0)) / (tf + k1 * (1.0 - b + b * (doc_len / max(1.0, avg_doc_len)))))
            score += term_score

        return score

    @staticmethod
    def _query_terms(query: str) -> List[str]:
        return [w.lower() for w in re.split(r"\W+", query or "") if len(w) > 2]

    def _rank_rows(self, rows: Sequence[sqlite3.Row], query_terms: List[str]) -> Dict[str, float]:
        """BM25 score (with entity and verification boosts) for each row id."""
        total_docs = len(rows)
        if not total_docs or not query_terms:
            return {}
        doc_token_map: Dict[str, List[str]] = {}
        df_counts: Dict[str, int] = {}
        total_len = 0
        for r in rows:
            tokens = [t.lower() for t in re.split(r"\W+", r["content"]) if len(t) > 2]
            doc_token_map[r["id"]] = tokens
            total_len += len(tokens)
            for unique_t in set(tokens):
                df_counts[unique_t] = df_counts.get(unique_t, 0) + 1
        avg_doc_len = total_len / max(1, total_docs)
        lowered_terms = {q.lower() for q in query_terms}

        scores: Dict[str, float] = {}
        for r in rows:
            score = self._bm25_score(query_terms, doc_token_map[r["id"]], df_counts, total_docs, avg_doc_len)
            try:
                ent_refs = json.loads(r["entity_refs_json"]) if r["entity_refs_json"] else []
            except (TypeError, ValueError):
                ent_refs = []
            if any(str(e).lower() in lowered_terms for e in ent_refs):
                score += 2.5
            if r["verification_status"] == VerificationStatus.VERIFIED_EMPIRICAL.value:
                score *= 1.25
            scores[r["id"]] = score
        return scores

    def search_memory(
        self,
        query: str,
        thread_id: Optional[str] = None,
        user_id: str = DEFAULT_USER_ID,
        memory_types: Optional[List[MemoryType]] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Search one user's memory nodes using Okapi BM25 ranking, optionally within one thread.
        """
        user_id = _require_user(user_id)
        query_terms = self._query_terms(query)
        if not query_terms:
            return []
        top_k = max(1, min(int(top_k or 5), 50))

        query_parts = ["is_contradicted = 0", "user_id = ?"]
        params: List[Any] = [user_id]
        if thread_id:
            query_parts.append("thread_id = ?")
            params.append(thread_id)
        if memory_types:
            type_placeholders = ",".join("?" for _ in memory_types)
            query_parts.append(f"memory_type IN ({type_placeholders})")
            params.extend([t.value for t in memory_types])

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, thread_id, user_id, memory_type, content, entity_refs_json, sources_json, "
                f"verification_status, confidence, created_at FROM memory_nodes WHERE {' AND '.join(query_parts)}",
                params,
            ).fetchall()

        scores = self._rank_rows(rows, query_terms)
        scored_results = [
            {
                "node_id": r["id"],
                "thread_id": r["thread_id"],
                "user_id": r["user_id"],
                "memory_type": r["memory_type"],
                "content": r["content"],
                "verification_status": r["verification_status"],
                "score": round(scores.get(r["id"], 0.0), 3),
                "sources": _sources_of(r),
            }
            for r in rows
            if scores.get(r["id"], 0.0) > 0.05
        ]
        scored_results.sort(key=lambda x: x["score"], reverse=True)
        return scored_results[:top_k]

    # -------------------------------------------------------------------------
    # CONTRADICTION DETECTION & RESOLUTION
    # -------------------------------------------------------------------------
    def _detect_and_handle_contradictions(
        self,
        thread_id: str,
        new_entity: str,
        new_assertion: str,
        conn: sqlite3.Connection,
        user_id: str = DEFAULT_USER_ID,
    ) -> Optional[str]:
        """
        Audit this user's prior evidence about the same entity in this thread. A prior node is demoted only when
        a sentence about the entity takes the opposite polarity in a sentence that shares its context.
        """
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, content, verification_status FROM memory_nodes
            WHERE thread_id = ? AND user_id = ? AND entity_refs_json LIKE ? AND is_contradicted = 0
            AND memory_type IN (?, ?)
            """,
            (
                thread_id,
                user_id,
                f'%"{new_entity}"%',
                MemoryType.RETRIEVED_EVIDENCE.value,
                MemoryType.COMPUTED_RESULT.value,
            ),
        )
        for ex in cursor.fetchall():
            ex_id = ex["id"]
            ex_content = ex["content"]
            if not assertions_conflict(_strip_label(ex_content), new_assertion, new_entity):
                continue
            contra_id = f"contra_{uuid.uuid4().hex[:8]}"
            cursor.execute(
                """
                INSERT INTO contradictions (id, thread_id, node_id, entity, prior_assertion, new_assertion, resolution, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contra_id,
                    thread_id,
                    ex_id,
                    new_entity,
                    ex_content,
                    new_assertion[:_SUMMARY_MAX],
                    "Demoted prior assertion; updated with empirical tool observation",
                    time.time(),
                ),
            )
            cursor.execute(
                """
                UPDATE memory_nodes
                SET is_contradicted = 1, verification_status = ?, contradiction_notes = ?
                WHERE id = ?
                """,
                (
                    VerificationStatus.REFUTED.value,
                    f"Refuted by subsequent observation: {new_assertion[:100]}",
                    ex_id,
                ),
            )
            logger.info("Contradiction detected & resolved for entity %s in thread %s", new_entity, thread_id)
            return contra_id
        return None

    # -------------------------------------------------------------------------
    # MEM0 TURN CONSOLIDATION
    # -------------------------------------------------------------------------
    @staticmethod
    def _insert_node(
        cursor: sqlite3.Cursor,
        prefix: str,
        thread_id: str,
        user_id: str,
        memory_type: MemoryType,
        content: str,
        entity_refs: List[str],
        sources: Dict[str, Any],
        status: VerificationStatus,
        confidence: float,
        now: float,
    ) -> str:
        node_id = f"{prefix}_{uuid.uuid4().hex[:8]}"
        cursor.execute(
            """
            INSERT INTO memory_nodes (id, thread_id, user_id, memory_type, content, entity_refs_json, sources_json, verification_status, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                thread_id,
                user_id,
                memory_type.value,
                content,
                json.dumps(entity_refs),
                json.dumps(sources, default=str),
                status.value,
                confidence,
                now,
                now,
            ),
        )
        return node_id

    def consolidate_turn_sync(
        self,
        thread_id: str,
        query: str,
        response: str,
        tool_results: Dict[str, Any],
        failure_lesson: Optional[str] = None,
        user_id: str = DEFAULT_USER_ID,
        is_turn_verified: bool = False,
        active_subject: Optional[Dict[str, Any]] = None,
        verification: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Selectively consolidate investigation turn into structured memory nodes.
        Never promotes unverified model markdown text or unvalidated tool runs to verified empirical evidence.

        verification (the turn's verification report or its counts) narrows is_turn_verified: a turn that left
        unsupported, contradicted or unchecked claims stores its tool evidence as provisional, never as verified.
        """
        user_id = _require_user(user_id)
        turn_verified = turn_fully_verified(is_turn_verified, verification)
        now = time.time()
        nodes_created = 0
        entities_updated: List[str] = []
        turn_id = uuid.uuid4().hex[:12]

        with self._connect() as conn:
            cursor = conn.cursor()

            # 1. The question itself is history, never a directive for later turns.
            self._insert_node(
                cursor, "mem_qry", thread_id, user_id, MemoryType.QUERY_HISTORY, (query or "")[:1000], [],
                {"origin": "user_prompt", "turn_id": turn_id}, VerificationStatus.PROVISIONAL, 1.0, now,
            )
            nodes_created += 1

            # 2. Instructions keep their scope; current_turn ones are stored for audit but never recalled.
            for text, scope in classify_instructions(query):
                self._insert_node(
                    cursor, "mem_ins", thread_id, user_id, MemoryType.USER_INSTRUCTION, text, [],
                    {"origin": "user_instruction", "scope": scope, "turn_id": turn_id},
                    VerificationStatus.PROVISIONAL, 1.0, now,
                )
                nodes_created += 1

            # 3. Record Empirical Evidence from Specialist Tools
            for t_name, t_data in tool_results.items():
                if not t_data:
                    continue
                t_data = apply_serialization_filters(t_name, t_data)

                ents = self.extract_entities(_tool_text(t_data))
                all_ents = ents["targets"] + ents["biomodels"] + ents["diseases"]
                body = summarize_tool_data(t_data, t_name)
                assertion = " ".join(t_data.split())[:_ASSERTION_MAX] if isinstance(t_data, str) else body

                # Determine Memory Type & Verification Status
                if "vivarix" in t_name.lower():
                    # A kinetic model's output describes the model, not patients: it stays provisional model output
                    # however well the turn verified, so it is never recalled as verified empirical evidence.
                    m_type = MemoryType.COMPUTED_RESULT
                    status = VerificationStatus.PROVISIONAL
                    confidence_val = 0.70 if turn_verified else 0.50
                    label = "Vivarix ODE Kinetic Model (model output, not clinical evidence)"
                elif "lumicell" in t_name.lower():
                    m_type = MemoryType.COMPUTED_RESULT
                    has_real_dataset = isinstance(t_data, dict) and bool(t_data.get("dataset_path"))
                    status = (VerificationStatus.VERIFIED_EMPIRICAL if (turn_verified and has_real_dataset) else VerificationStatus.PROVISIONAL)
                    confidence_val = 0.90 if (turn_verified and has_real_dataset) else 0.50
                    label = "Lumicell Single-Cell Transcriptomics"
                elif "lithoscribe" in t_name.lower():
                    m_type = MemoryType.RETRIEVED_EVIDENCE
                    status = VerificationStatus.VERIFIED_EMPIRICAL if turn_verified else VerificationStatus.PROVISIONAL
                    confidence_val = 0.95 if turn_verified else 0.60
                    label = "Lithoscribe Peer-Reviewed Passage Evidence"
                elif "synapta" in t_name.lower():
                    m_type = MemoryType.RETRIEVED_EVIDENCE
                    status = VerificationStatus.VERIFIED_EMPIRICAL if turn_verified else VerificationStatus.PROVISIONAL
                    confidence_val = 0.95 if turn_verified else 0.60
                    label = "Synapta Knowledge Graph Association"
                else:
                    m_type = MemoryType.RETRIEVED_EVIDENCE
                    status = VerificationStatus.PROVISIONAL
                    confidence_val = 0.50
                    label = f"Tool Evidence ({t_name})"

                # Check for contradictions against existing facts
                for e in all_ents:
                    self._detect_and_handle_contradictions(thread_id, e, assertion, conn, user_id=user_id)

                self._insert_node(
                    cursor, "mem_ev", thread_id, user_id, m_type, f"{label}: {body}", all_ents,
                    {"tool": t_name, "timestamp": now, "turn_id": turn_id, "identifiers": self.collect_identifiers(t_data, t_name),
                     "turn_verified": turn_verified, "unresolved_claims": unresolved_claim_count(verification)},
                    status, confidence_val, now,
                )
                nodes_created += 1
                entities_updated.extend(all_ents)

            # 4. Record Failure Lesson if generated by Reflexion Verifier
            if failure_lesson:
                self._insert_node(
                    cursor, "mem_les", thread_id, user_id, MemoryType.FAILURE_LESSON, failure_lesson[:1000], [],
                    {"origin": "reflexion_verifier", "turn_id": turn_id}, VerificationStatus.VERIFIED_EMPIRICAL, 1.0, now,
                )
                nodes_created += 1

            # 5. Record Dialogue Turn (as UNVERIFIED_GENERATED for the assistant response)
            self._insert_node(
                cursor, "mem_trn", thread_id, user_id, MemoryType.DIALOGUE_TURN, f"Synthesized Response: {(response or '')[:350]}...",
                [], {"origin": "clinical_supervisor_synthesis", "turn_id": turn_id}, VerificationStatus.UNVERIFIED_GENERATED, 0.70, now,
            )
            nodes_created += 1

            subject = _clean_subject(active_subject)
            if subject:
                self._write_active_subject(conn, user_id, thread_id, subject, now)

        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "nodes_created": nodes_created,
            "entities_updated": list(set(entities_updated)),
        }

    async def consolidate_turn(
        self,
        thread_id: str,
        query: str,
        response: str,
        tool_results: Dict[str, Any],
        failure_lesson: Optional[str] = None,
        user_id: str = DEFAULT_USER_ID,
        is_turn_verified: bool = False,
        active_subject: Optional[Dict[str, Any]] = None,
        verification: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Async consolidation: SQLite work runs in a worker thread so the event loop is not blocked."""
        _require_user(user_id)
        try:
            from ...extensions import load_extensions

            load_extensions()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Extensions unavailable during memory consolidation: %s", exc)
        return await asyncio.to_thread(
            self.consolidate_turn_sync,
            thread_id,
            query,
            response,
            tool_results,
            failure_lesson,
            user_id,
            is_turn_verified,
            active_subject,
            verification,
        )

    # -------------------------------------------------------------------------
    # ACTIVE SUBJECT (follow-up resolution)
    # -------------------------------------------------------------------------
    @staticmethod
    def _write_active_subject(conn: sqlite3.Connection, user_id: str, thread_id: str, subject: Dict[str, str], now: float) -> None:
        # The latest turn's subject replaces the previous one: a switch from KRAS to BRAF does not keep KRAS.
        conn.execute(
            """
            INSERT INTO active_subjects (user_id, thread_id, subject_json, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, thread_id) DO UPDATE SET subject_json = excluded.subject_json, updated_at = excluded.updated_at
            """,
            (user_id, thread_id, json.dumps(subject), now),
        )

    @staticmethod
    def _read_active_subject(conn: sqlite3.Connection, user_id: str, thread_id: str) -> Optional[Dict[str, str]]:
        row = conn.execute(
            "SELECT subject_json FROM active_subjects WHERE user_id = ? AND thread_id = ?", (user_id, thread_id)
        ).fetchone()
        if row is None:
            return None
        try:
            subject = _clean_subject(json.loads(row["subject_json"]))
        except (TypeError, ValueError):
            return None
        return subject or None

    def record_active_subject(self, user_id: str, thread_id: str, subject: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        """Store the subject (gene/drug/disease/biomodel_id) of the latest turn. Empty subjects are ignored."""
        user_id = _require_user(user_id)
        clean = _clean_subject(subject)
        if not clean:
            return None
        with self._connect() as conn:
            self._write_active_subject(conn, user_id, thread_id, clean, time.time())
        return clean

    def get_active_subject(self, user_id: str, thread_id: str) -> Optional[Dict[str, str]]:
        """The subject a follow-up such as "What about its safety?" refers to, or None."""
        user_id = _require_user(user_id)
        with self._connect() as conn:
            return self._read_active_subject(conn, user_id, thread_id)

    async def aget_active_subject(self, user_id: str, thread_id: str) -> Optional[Dict[str, str]]:
        return await asyncio.to_thread(self.get_active_subject, user_id, thread_id)

    # -------------------------------------------------------------------------
    # CONTEXT EXTRACTION & CLEANUP
    # -------------------------------------------------------------------------
    def get_context_package(
        self, thread_id: str, user_id: str = DEFAULT_USER_ID, query: Optional[str] = None
    ) -> MemoryContextPackage:
        """
        Extract a bounded memory package for one user and thread. With `query`, evidence is ranked by BM25
        relevance; otherwise the most recent items come first.
        """
        user_id = _require_user(user_id)
        pkg = MemoryContextPackage(thread_id=thread_id)

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, memory_type, content, entity_refs_json, sources_json, verification_status, created_at
                FROM memory_nodes WHERE thread_id = ? AND user_id = ? AND is_contradicted = 0
                ORDER BY created_at DESC
                """,
                (thread_id, user_id),
            ).fetchall()
            profile_rows = conn.execute(
                """
                SELECT content, sources_json FROM memory_nodes
                WHERE user_id = ? AND memory_type = ? AND thread_id != ? AND is_contradicted = 0
                ORDER BY created_at DESC LIMIT 200
                """,
                (user_id, MemoryType.USER_INSTRUCTION.value, thread_id),
            ).fetchall()
            subject = self._read_active_subject(conn, user_id, thread_id)

        if subject:
            pkg.active_subject = subject
            pkg.resolved_subject = next((subject[k] for k in SUBJECT_KEYS if subject.get(k)), None)

        instructions: List[str] = []
        history: List[str] = []
        evidence_rows: List[sqlite3.Row] = []
        for r in rows:
            m_type = r["memory_type"]
            sources = _sources_of(r)
            if m_type == MemoryType.QUERY_HISTORY.value:
                history.append(r["content"])
            elif m_type == MemoryType.USER_INSTRUCTION.value:
                if sources.get("origin") == "user_prompt":  # legacy rows: every question was stored as an instruction
                    history.append(r["content"].removeprefix(_LEGACY_GOAL_PREFIX))
                elif sources.get("scope") in ("investigation", "profile"):
                    instructions.append(r["content"])
            elif m_type == MemoryType.FAILURE_LESSON.value:
                if len(pkg.failure_lessons) < MAX_LESSONS:
                    pkg.failure_lessons.append(r["content"])
            elif m_type in (
                MemoryType.RETRIEVED_EVIDENCE.value,
                MemoryType.COMPUTED_RESULT.value,
                MemoryType.WORKING_HYPOTHESIS.value,
            ):
                evidence_rows.append(r)
        for r in profile_rows:
            if _sources_of(r).get("scope") == "profile":
                instructions.append(r["content"])

        pkg.user_instructions = list(dict.fromkeys(instructions))[:MAX_INSTRUCTIONS]
        pkg.query_history = list(reversed(list(dict.fromkeys(history))[:MAX_QUERY_HISTORY]))

        # Most relevant first when a query is given, else most recent (rows are already newest first).
        scores = self._rank_rows(evidence_rows, self._query_terms(query or "")) if query else {}
        if scores:
            evidence_rows = sorted(evidence_rows, key=lambda r: (scores.get(r["id"], 0.0), r["created_at"]), reverse=True)
        for r in evidence_rows:
            m_type, content = r["memory_type"], r["content"]
            verified = r["verification_status"] == VerificationStatus.VERIFIED_EMPIRICAL.value
            if m_type == MemoryType.RETRIEVED_EVIDENCE.value and verified:
                if len(pkg.verified_evidence) < MAX_EVIDENCE:
                    pkg.verified_evidence.append(content)
            elif m_type == MemoryType.COMPUTED_RESULT.value and verified:
                if len(pkg.computed_artifacts) < MAX_COMPUTED:
                    pkg.computed_artifacts.append(content)
            elif len(pkg.active_hypotheses) < MAX_HYPOTHESES:
                if m_type == MemoryType.RETRIEVED_EVIDENCE.value:
                    pkg.active_hypotheses.append(f"[Provisional Literature] {content}")
                elif m_type == MemoryType.COMPUTED_RESULT.value:
                    pkg.active_hypotheses.append(f"[Provisional Compute] {content}")
                else:
                    pkg.active_hypotheses.append(content)
        return pkg

    async def arecall(self, thread_id: str, user_id: str = DEFAULT_USER_ID, query: Optional[str] = None) -> MemoryContextPackage:
        return await asyncio.to_thread(self.get_context_package, thread_id, user_id, query)

    def clear_thread_memory(self, thread_id: str, user_id: str = DEFAULT_USER_ID) -> None:
        """Purge one user's memory nodes for exactly this thread id (never a prefix match)."""
        user_id = _require_user(user_id)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM contradictions WHERE thread_id = ? AND node_id IN "
                "(SELECT id FROM memory_nodes WHERE thread_id = ? AND user_id = ?)",
                (thread_id, thread_id, user_id),
            )
            conn.execute("DELETE FROM memory_nodes WHERE thread_id = ? AND user_id = ?", (thread_id, user_id))
            conn.execute("DELETE FROM active_subjects WHERE thread_id = ? AND user_id = ?", (thread_id, user_id))

    def clear_all_memory(self, user_id: str) -> None:
        """Remove every memory belonging to one user. There is deliberately no unscoped variant."""
        user_id = _require_user(user_id)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM contradictions WHERE node_id IN (SELECT id FROM memory_nodes WHERE user_id = ?)",
                (user_id,),
            )
            conn.execute("DELETE FROM memory_nodes WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM active_subjects WHERE user_id = ?", (user_id,))

    def get_memory_stats(self, user_id: str = DEFAULT_USER_ID) -> Dict[str, Any]:
        """Return memory vault metrics for one user."""
        user_id = _require_user(user_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT thread_id) FROM memory_nodes WHERE user_id = ?", (user_id,)
            ).fetchone()
            total_nodes = row[0] if row else 0
            total_threads = row[1] if row else 0
            contra_row = conn.execute(
                "SELECT COUNT(*) FROM contradictions WHERE node_id IN (SELECT id FROM memory_nodes WHERE user_id = ?)",
                (user_id,),
            ).fetchone()
            total_contras = contra_row[0] if contra_row else 0
            v_row = conn.execute(
                "SELECT COUNT(*) FROM memory_nodes WHERE verification_status = ? AND user_id = ?",
                (VerificationStatus.VERIFIED_EMPIRICAL.value, user_id),
            ).fetchone()
            total_verified = v_row[0] if v_row else 0

        db_size_kb = os.path.getsize(self.db_path) / 1024.0 if os.path.exists(self.db_path) else 0.0
        return {
            "status": "active",
            "storage_path": None,
            "db_size_kb": round(db_size_kb, 1),
            "total_nodes": total_nodes,
            "total_threads": total_threads,
            "total_verified_empirical": total_verified,
            "total_contradictions_resolved": total_contras,
        }

    def get_conversation_history(self, thread_id: str, user_id: str = DEFAULT_USER_ID) -> List[Dict[str, Any]]:
        """Return one user's conversational history (questions, instructions, answers) for a thread."""
        user_id = _require_user(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, thread_id, user_id, memory_type, content, verification_status, created_at FROM memory_nodes "
                "WHERE thread_id = ? AND user_id = ? AND memory_type IN (?, ?, ?) ORDER BY created_at ASC",
                (
                    thread_id,
                    user_id,
                    MemoryType.QUERY_HISTORY.value,
                    MemoryType.USER_INSTRUCTION.value,
                    MemoryType.DIALOGUE_TURN.value,
                ),
            ).fetchall()
            return [dict(r) for r in rows]

    async def aget_conversation_history(self, thread_id: str, user_id: str = DEFAULT_USER_ID) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.get_conversation_history, thread_id, user_id)

    async def asearch_memory(
        self,
        query: str,
        thread_id: Optional[str] = None,
        user_id: str = DEFAULT_USER_ID,
        memory_types: Optional[List[MemoryType]] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.search_memory, query, thread_id, user_id, memory_types, top_k)

    def get_entity_vault(self, user_id: str = DEFAULT_USER_ID) -> List[Dict[str, Any]]:
        """Return entity assertions aggregated from one user's memory nodes."""
        user_id = _require_user(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_refs_json, created_at FROM memory_nodes WHERE user_id = ? AND is_contradicted = 0",
                (user_id,),
            ).fetchall()
        agg: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            try:
                refs = json.loads(r["entity_refs_json"] or "[]")
            except (TypeError, ValueError):
                refs = []
            for ent in refs:
                item = agg.setdefault(ent, {"entity": ent, "mention_count": 0, "first_seen": r["created_at"], "last_seen": r["created_at"]})
                item["mention_count"] += 1
                item["first_seen"] = min(item["first_seen"], r["created_at"])
                item["last_seen"] = max(item["last_seen"], r["created_at"])
        return sorted(agg.values(), key=lambda x: x["mention_count"], reverse=True)

    @staticmethod
    def collect_identifiers(data: Any, tool_key: str = "") -> Dict[str, List[str]]:
        """
        Extract canonical identifiers from a tool output: BioModels, PMIDs, DOIs, Ensembl and UniProt, plus
        every identifier family (NCT, ChEMBL, HGNC, ...). Registered serialization filters run first.
        """
        data = apply_serialization_filters(tool_key, data)
        ids: Dict[str, Set[str]] = {k: set() for k in (*_LEGACY_ID_KEYS, *family_names())}
        if isinstance(data, dict):
            if data.get("model_id"):
                ids["biomodels"].add(str(data["model_id"]))
            for p in data.get("ranked_passages", []) or []:
                if isinstance(p, dict):
                    if p.get("pmid"):
                        ids["pmids"].add(str(p["pmid"]))
                    if p.get("doi"):
                        ids["dois"].add(str(p["doi"]))
        text = _tool_text(data)
        ids["biomodels"].update(re.findall(r"\bBIOMD\d{10}\b", text))
        ids["ensembl"].update(re.findall(r"\bENS(?:[A-Z]{3})?[GTP]\d{11}\b", text))
        ids["uniprot"].update(re.findall(r"\b(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\b", text))
        for name, found in extract_family_ids(text).items():
            ids[name].update(found)
        return {k: sorted(v) for k, v in ids.items()}

    def get_verified_identifiers(self, thread_id: str, user_id: str = DEFAULT_USER_ID) -> Dict[str, Set[str]]:
        """Identifiers observed by tools in this user's earlier verified turns of this thread (for the verifier).

        A kinetic model's output is stored provisional (it is model output, not clinical evidence), but the model id
        a verified turn observed is still an observed identifier: its node records turn_verified."""
        user_id = _require_user(user_id)
        result: Dict[str, Set[str]] = {k: set() for k in (*_LEGACY_ID_KEYS, *family_names())}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content, sources_json, verification_status FROM memory_nodes WHERE thread_id = ? AND user_id = ? "
                "AND is_contradicted = 0 AND verification_status IN (?, ?) AND memory_type IN (?, ?)",
                (
                    thread_id,
                    user_id,
                    VerificationStatus.VERIFIED_EMPIRICAL.value,
                    VerificationStatus.PROVISIONAL.value,
                    MemoryType.RETRIEVED_EVIDENCE.value,
                    MemoryType.COMPUTED_RESULT.value,
                ),
            ).fetchall()
        for r in rows:
            sources = _sources_of(r)
            if r["verification_status"] != VerificationStatus.VERIFIED_EMPIRICAL.value and sources.get("turn_verified") is not True:
                continue
            stored = sources.get("identifiers", {}) or {}
            if not isinstance(stored, dict):
                stored = {}
            for k in result:
                result[k].update(stored.get(k, []))
            result["biomodels"].update(KNOWN_BIOMODEL_PATTERN.findall(r["content"] or ""))
        return result

    async def aget_verified_identifiers(self, thread_id: str, user_id: str = DEFAULT_USER_ID) -> Dict[str, Set[str]]:
        return await asyncio.to_thread(self.get_verified_identifiers, thread_id, user_id)


_GLOBAL_CLINICAL_MEMORY: Optional[ClinicalMemoryEngine] = None


def get_clinical_memory_engine() -> ClinicalMemoryEngine:
    global _GLOBAL_CLINICAL_MEMORY
    if _GLOBAL_CLINICAL_MEMORY is None:
        _GLOBAL_CLINICAL_MEMORY = ClinicalMemoryEngine()
    return _GLOBAL_CLINICAL_MEMORY
