"""
Project memory store (feature 16): research projects and a bi-temporal fact graph, one SQLite file.

- Zep / Graphiti (Rasmussen et al. 2025, arXiv:2501.13956): each fact is an edge (subject, relation, object)
  with bi-temporal stamps: `valid_from`/`invalid_at` say when it held, `recorded_at` when it was ingested. A
  contradicting new fact invalidates the old one (invalid_at, superseded_by) instead of deleting it, so a
  project keeps the history of how its findings were revised.
- Mem0 (Chhikara et al. 2025, arXiv:2504.19413): extract, then decide ADD, UPDATE, INVALIDATE or NOOP against
  the existing facts. Here the decision is deterministic (see `upsert_facts`).
- HippoRAG (Gutierrez et al., NeurIPS 2024, arXiv:2405.14831): retrieval by Personalized PageRank over the fact
  graph (nodes = subjects and objects, edges = valid facts), seeded with the query's entities.
- Generative Agents (Park et al., UIST 2023, arXiv:2304.03442): the final ranking is recency x importance x
  relevance (each min-max normalised and summed with equal weights, as in the paper).
- MemGPT (Packer et al. 2023, arXiv:2310.08560): the top facts are paged into the prompt (working memory);
  everything else stays in this archival store and is reachable through the projects API.
- A-MEM (Xu et al., NeurIPS 2025, arXiv:2502.12110): facts that share an entity are linked notes; the graph
  walk is what follows those links.

Every read and write is scoped to one user. Ids: projects `prj_<16 alphanumerics>`, facts `fct_<16 hex>`.
Location: `PHRONESIS_PROJECTS_DIR`, else next to the clinical memory store (`PHRONESIS_MEMORY_DIR`, else
`<workspace>/.phronesis_memory`); pytest runs without either use a temporary directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sqlite3
import string
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

PROJECT_ID_RE = re.compile(r"^prj_[A-Za-z0-9]{6,40}$")
FACT_ID_RE = re.compile(r"^fct_[0-9a-f]{16}$")

# Relations with one current object per subject: a new object contradicts the old one.
SINGLE_VALUED_RELATIONS = frozenset({"classification", "mechanism", "germline_classification", "preferred_model"})
# Relations whose (subject, object) pair carries one current value: a new value contradicts the old one.
VALUED_RELATIONS = frozenset({"association_score_with"})

_BUSY_TIMEOUT_S = 5.0
_ALNUM = string.ascii_letters + string.digits
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]{1,}")
_STOP = {
    "the", "and", "for", "with", "what", "which", "how", "does", "is", "are", "of", "in", "on", "to", "a", "an",
    "about", "tell", "me", "be", "by", "or", "its", "it", "this", "that", "from", "as", "at", "do", "can",
}

MAX_TEXT = 500
DEFAULT_RECALL_K = 12
_RECENCY_DECAY_PER_HOUR = 0.995  # Generative Agents' exponential decay factor


class ProjectStoreError(ValueError):
    pass


def new_project_id() -> str:
    return "prj_" + "".join(secrets.choice(_ALNUM) for _ in range(16))


def new_fact_id() -> str:
    return "fct_" + uuid.uuid4().hex[:16]


def norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def tokens(text: str) -> Set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if t.lower() not in _STOP}


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def _require_user(user_id: Optional[str]) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise ProjectStoreError("user_id is required")
    return user_id


@dataclass
class FactInput:
    subject: str
    relation: str
    object: str
    value: Optional[str] = None
    confidence: float = 0.7
    verified: bool = False
    source: Optional[Dict[str, Any]] = None
    tags: Optional[List[str]] = None
    valid_from: Optional[float] = None

    def clean(self) -> "FactInput":
        self.subject = str(self.subject or "").strip()[:MAX_TEXT]
        self.relation = re.sub(r"[^a-z0-9_]+", "_", str(self.relation or "").strip().lower())[:64].strip("_")
        self.object = str(self.object or "").strip()[:MAX_TEXT]
        self.value = None if self.value is None else str(self.value)[:64]
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.tags = [str(t)[:40] for t in (self.tags or [])][:10]
        return self


class ProjectStore:
    def __init__(self, storage_dir: Optional[str] = None) -> None:
        self.storage_dir = storage_dir or default_projects_dir()
        os.makedirs(self.storage_dir, exist_ok=True)
        self.db_path = os.path.join(self.storage_dir, "projects.db")
        self._init_db()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with contextlib.closing(sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_S, check_same_thread=False)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {int(_BUSY_TIMEOUT_S * 1000)}")
            with conn:
                yield conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            with contextlib.suppress(sqlite3.DatabaseError):
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prj_user ON projects(user_id, updated_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS facts (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    subject_norm TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    object TEXT NOT NULL,
                    object_norm TEXT NOT NULL,
                    value TEXT,
                    valid_from REAL NOT NULL,
                    invalid_at REAL,
                    recorded_at REAL NOT NULL,
                    last_accessed REAL,
                    source_json TEXT NOT NULL DEFAULT '{}',
                    confidence REAL NOT NULL DEFAULT 0.7,
                    verified INTEGER NOT NULL DEFAULT 0,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    superseded_by TEXT,
                    invalidation_reason TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_project ON facts(project_id, user_id, invalid_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_key ON facts(project_id, subject_norm, relation)")

    # Projects ------------------------------------------------------------------------------------------------
    @staticmethod
    def _project_dict(row: sqlite3.Row, counts: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
        out = {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }
        if counts is not None:
            out["facts"] = {"valid": counts[0], "invalidated": counts[1]}
        return out

    def create_project(self, user_id: str, name: str, description: str = "") -> Dict[str, Any]:
        user_id = _require_user(user_id)
        name = str(name or "").strip()[:200]
        if not name:
            raise ProjectStoreError("A project needs a name")
        now = time.time()
        pid = new_project_id()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO projects (id, user_id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (pid, user_id, name, str(description or "")[:2000], now, now),
            )
        return self.get_project(pid, user_id) or {}

    def _counts(self, conn: sqlite3.Connection, project_id: str, user_id: str) -> Tuple[int, int]:
        row = conn.execute(
            "SELECT SUM(CASE WHEN invalid_at IS NULL THEN 1 ELSE 0 END) AS v, SUM(CASE WHEN invalid_at IS NULL THEN 0 ELSE 1 END) AS i "
            "FROM facts WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
        return int(row["v"] or 0), int(row["i"] or 0)

    def get_project(self, project_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        user_id = _require_user(user_id)
        if not PROJECT_ID_RE.match(project_id or ""):
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id)).fetchone()
            if row is None:
                return None
            return self._project_dict(row, self._counts(conn, project_id, user_id))

    def owns(self, project_id: Optional[str], user_id: Optional[str]) -> bool:
        if not project_id or not user_id or not PROJECT_ID_RE.match(project_id):
            return False
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id)).fetchone() is not None

    def list_projects(self, user_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        user_id = _require_user(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?", (user_id, max(1, min(500, limit)))
            ).fetchall()
            return [self._project_dict(r, self._counts(conn, r["id"], user_id)) for r in rows]

    def update_project(self, project_id: str, user_id: str, name: Optional[str] = None, description: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.owns(project_id, user_id):
            return None
        sets, args = ["updated_at = ?"], [time.time()]
        if name is not None:
            name = str(name).strip()[:200]
            if not name:
                raise ProjectStoreError("A project needs a name")
            sets.append("name = ?")
            args.append(name)
        if description is not None:
            sets.append("description = ?")
            args.append(str(description)[:2000])
        with self._connect() as conn:
            conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ? AND user_id = ?", (*args, project_id, user_id))
        return self.get_project(project_id, user_id)

    def delete_project(self, project_id: str, user_id: str) -> bool:
        if not self.owns(project_id, user_id):
            return False
        with self._connect() as conn:
            conn.execute("DELETE FROM facts WHERE project_id = ? AND user_id = ?", (project_id, user_id))
            conn.execute("DELETE FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id))
        return True

    def touch_project(self, conn: sqlite3.Connection, project_id: str, user_id: str) -> None:
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ? AND user_id = ?", (time.time(), project_id, user_id))

    # Facts ---------------------------------------------------------------------------------------------------
    @staticmethod
    def _fact_dict(row: sqlite3.Row) -> Dict[str, Any]:
        try:
            source = json.loads(row["source_json"] or "{}")
        except ValueError:
            source = {}
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except ValueError:
            tags = []
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "subject": row["subject"],
            "relation": row["relation"],
            "object": row["object"],
            "value": row["value"],
            "valid_from": _iso(row["valid_from"]),
            "invalid_at": _iso(row["invalid_at"]),
            "recorded_at": _iso(row["recorded_at"]),
            "source": source,
            "confidence": round(float(row["confidence"]), 3),
            "verified": bool(row["verified"]),
            "tags": tags,
            "superseded_by": row["superseded_by"],
            "invalidation_reason": row["invalidation_reason"],
        }

    def get_fact(self, project_id: str, fact_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        if not FACT_ID_RE.match(fact_id or "") or not self.owns(project_id, user_id):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM facts WHERE id = ? AND project_id = ? AND user_id = ?", (fact_id, project_id, user_id)
            ).fetchone()
        return self._fact_dict(row) if row is not None else None

    def list_facts(self, project_id: str, user_id: str, include_invalid: bool = False, limit: int = 200) -> List[Dict[str, Any]]:
        if not self.owns(project_id, user_id):
            return []
        where = "" if include_invalid else " AND invalid_at IS NULL"
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM facts WHERE project_id = ? AND user_id = ?{where} ORDER BY recorded_at DESC LIMIT ?",
                (project_id, user_id, max(1, min(2000, limit))),
            ).fetchall()
        return [self._fact_dict(r) for r in rows]

    def _insert(self, conn: sqlite3.Connection, project_id: str, user_id: str, f: FactInput, now: float) -> str:
        fid = new_fact_id()
        tags = list(f.tags or [])
        if not f.verified and "unverified" not in tags:
            tags.append("unverified")
        conn.execute(
            "INSERT INTO facts (id, project_id, user_id, subject, subject_norm, relation, object, object_norm, value, valid_from, "
            "invalid_at, recorded_at, last_accessed, source_json, confidence, verified, tags_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?)",
            (
                fid, project_id, user_id, f.subject, norm(f.subject), f.relation, f.object, norm(f.object), f.value,
                f.valid_from or now, now, json.dumps(f.source or {}, default=str)[:4000], f.confidence, int(bool(f.verified)),
                json.dumps(tags),
            ),
        )
        return fid

    @staticmethod
    def _invalidate(conn: sqlite3.Connection, fact_id: str, now: float, superseded_by: Optional[str], reason: str) -> None:
        conn.execute(
            "UPDATE facts SET invalid_at = ?, superseded_by = ?, invalidation_reason = ? WHERE id = ? AND invalid_at IS NULL",
            (now, superseded_by, reason[:300], fact_id),
        )

    def upsert_facts(self, project_id: str, user_id: str, facts: Sequence[FactInput]) -> Dict[str, Any]:
        """Mem0-style deterministic update of the fact graph with one batch of extracted facts.

        Per fact, against the currently valid facts with the same subject and relation:
        - same object (and the same value, for valued relations): NOOP, or UPDATE when the new fact is verified
          and the stored one is not, or the new confidence is higher;
        - a single-valued relation with a different object, or a valued relation with a different value:
          INVALIDATE the old fact (Zep: invalid_at + superseded_by, never deleted), then ADD the new one;
        - otherwise ADD.
        Facts of one batch never invalidate each other (a drug with two mechanisms is two facts).
        """
        if not self.owns(project_id, user_id):
            raise ProjectStoreError("Project not found")
        now = time.time()
        ops: List[Dict[str, Any]] = []
        batch_keys: Dict[Tuple[str, str], Set[str]] = {}
        cleaned = [f.clean() for f in facts if f.subject and f.relation and f.object]
        cleaned = [f for f in cleaned if f.subject and f.relation and f.object]
        for f in cleaned:
            batch_keys.setdefault((norm(f.subject), f.relation), set()).add(norm(f.object))
        written = invalidated = 0
        with self._connect() as conn:
            for f in cleaned:
                s_norm, o_norm = norm(f.subject), norm(f.object)
                existing = conn.execute(
                    "SELECT * FROM facts WHERE project_id = ? AND user_id = ? AND subject_norm = ? AND relation = ? AND invalid_at IS NULL",
                    (project_id, user_id, s_norm, f.relation),
                ).fetchall()
                same = [r for r in existing if r["object_norm"] == o_norm]
                if f.relation in VALUED_RELATIONS and same and f.value is not None:
                    differing = [r for r in same if r["value"] is not None and r["value"] != f.value]
                    if differing:
                        new_id = self._insert(conn, project_id, user_id, f, now)
                        for r in differing:
                            self._invalidate(conn, r["id"], now, new_id, f"value changed from {r['value']} to {f.value}")
                        ops.append({"op": "INVALIDATE", "fact_id": new_id, "invalidated": [r["id"] for r in differing]})
                        ops.append({"op": "ADD", "fact_id": new_id})
                        written += 1
                        invalidated += len(differing)
                        continue
                if same:
                    row = same[0]
                    better = (f.verified and not row["verified"]) or f.confidence > float(row["confidence"]) + 1e-6
                    if better:
                        tags = sorted(set(json.loads(row["tags_json"] or "[]")) - ({"unverified"} if f.verified else set()) | set(f.tags or []))
                        # An unverified write never replaces the provenance of a verified fact.
                        keep_source = bool(row["verified"]) and not f.verified
                        conn.execute(
                            "UPDATE facts SET confidence = ?, verified = ?, tags_json = ?, source_json = ?, value = COALESCE(?, value) WHERE id = ?",
                            (
                                max(f.confidence, float(row["confidence"])), int(bool(f.verified or row["verified"])), json.dumps(tags),
                                row["source_json"] if keep_source else json.dumps(f.source or {}, default=str)[:4000],
                                f.value, row["id"],
                            ),
                        )
                        ops.append({"op": "UPDATE", "fact_id": row["id"]})
                    else:
                        ops.append({"op": "NOOP", "fact_id": row["id"]})
                    continue
                contradicted: List[sqlite3.Row] = []
                if f.relation in SINGLE_VALUED_RELATIONS:
                    in_batch = batch_keys.get((s_norm, f.relation), set())
                    contradicted = [r for r in existing if r["object_norm"] not in in_batch and float(r["recorded_at"]) < now]
                new_id = self._insert(conn, project_id, user_id, f, now)
                for r in contradicted:
                    self._invalidate(conn, r["id"], now, new_id, f"contradicted by a newer {f.relation}: {f.object[:120]}")
                if contradicted:
                    ops.append({"op": "INVALIDATE", "fact_id": new_id, "invalidated": [r["id"] for r in contradicted]})
                    invalidated += len(contradicted)
                ops.append({"op": "ADD", "fact_id": new_id})
                written += 1
            if cleaned:
                self.touch_project(conn, project_id, user_id)
        return {"written": written, "invalidated": invalidated, "ops": ops}

    def invalidate_fact(self, project_id: str, fact_id: str, user_id: str, reason: str = "invalidated by the researcher") -> Optional[Dict[str, Any]]:
        if self.get_fact(project_id, fact_id, user_id) is None:
            return None
        with self._connect() as conn:
            self._invalidate(conn, fact_id, time.time(), None, reason)
        return self.get_fact(project_id, fact_id, user_id)

    def edit_fact(
        self,
        project_id: str,
        fact_id: str,
        user_id: str,
        *,
        object: Optional[str] = None,  # noqa: A002 - the fact's field name
        value: Optional[str] = None,
        confidence: Optional[float] = None,
        verified: Optional[bool] = None,
        tags: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Edit a fact. A changed object or value is a new version (old one invalidated, Zep-style)."""
        old = self.get_fact(project_id, fact_id, user_id)
        if old is None:
            return None
        if old["invalid_at"] is not None:
            raise ProjectStoreError("An invalidated fact cannot be edited")
        now = time.time()
        if (object is not None and object.strip() != old["object"]) or (value is not None and value != old["value"]):
            f = FactInput(
                subject=old["subject"],
                relation=old["relation"],
                object=object.strip() if object is not None else old["object"],
                value=value if value is not None else old["value"],
                confidence=confidence if confidence is not None else old["confidence"],
                verified=verified if verified is not None else old["verified"],
                source={**(old["source"] or {}), "edited_by": "researcher", "previous": fact_id},
                tags=[t for t in (tags if tags is not None else old["tags"]) if t != "unverified"] + ["edited"],
            ).clean()
            with self._connect() as conn:
                new_id = self._insert(conn, project_id, user_id, f, now)
                self._invalidate(conn, fact_id, now, new_id, "edited by the researcher")
                self.touch_project(conn, project_id, user_id)
            return self.get_fact(project_id, new_id, user_id)
        sets, args = [], []
        if confidence is not None:
            sets.append("confidence = ?")
            args.append(max(0.0, min(1.0, float(confidence))))
        if verified is not None:
            sets.append("verified = ?")
            args.append(int(bool(verified)))
        if tags is not None:
            sets.append("tags_json = ?")
            args.append(json.dumps([str(t)[:40] for t in tags][:10]))
        if sets:
            with self._connect() as conn:
                conn.execute(f"UPDATE facts SET {', '.join(sets)} WHERE id = ? AND user_id = ?", (*args, fact_id, user_id))
        return self.get_fact(project_id, fact_id, user_id)

    def delete_fact(self, project_id: str, fact_id: str, user_id: str) -> bool:
        if self.get_fact(project_id, fact_id, user_id) is None:
            return False
        with self._connect() as conn:
            conn.execute("DELETE FROM facts WHERE id = ? AND project_id = ? AND user_id = ?", (fact_id, project_id, user_id))
        return True

    # Retrieval -----------------------------------------------------------------------------------------------
    def recall(
        self,
        project_id: str,
        user_id: str,
        query: str = "",
        entities: Iterable[str] = (),
        k: int = DEFAULT_RECALL_K,
        touch: bool = True,
    ) -> List[Dict[str, Any]]:
        """Top-k valid facts by Personalized PageRank relevance combined with recency and importance."""
        if not self.owns(project_id, user_id):
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM facts WHERE project_id = ? AND user_id = ? AND invalid_at IS NULL", (project_id, user_id)
            ).fetchall()
        if not rows:
            return []
        ranked = rank_facts(rows, query, entities)
        top = ranked[: max(1, k)]
        if touch and top:
            now = time.time()
            with self._connect() as conn:
                conn.executemany("UPDATE facts SET last_accessed = ? WHERE id = ?", [(now, r["id"]) for r, _ in top])
        out = []
        for row, score in top:
            d = self._fact_dict(row)
            d["score"] = round(score, 4)
            out.append(d)
        return out


# ---------------------------------------------------------------------------------------------------------
# Ranking: HippoRAG Personalized PageRank + Generative Agents scoring
# ---------------------------------------------------------------------------------------------------------

def personalized_pagerank(
    edges: Sequence[Tuple[str, str, float]],
    seeds: Dict[str, float],
    damping: float = 0.85,
    iterations: int = 40,
    tol: float = 1e-8,
) -> Dict[str, float]:
    """PPR over an undirected weighted graph; the restart distribution is the normalised seed weights.

    With no seed in the graph the restart distribution is uniform (plain PageRank)."""
    adj: Dict[str, Dict[str, float]] = {}
    for a, b, w in edges:
        if a == b:
            continue
        w = max(1e-6, float(w))
        adj.setdefault(a, {})[b] = adj.get(a, {}).get(b, 0.0) + w
        adj.setdefault(b, {})[a] = adj.get(b, {}).get(a, 0.0) + w
    nodes = list(adj)
    if not nodes:
        return {}
    restart = {n: float(seeds.get(n, 0.0)) for n in nodes}
    total = sum(restart.values())
    if total <= 0:
        restart = {n: 1.0 / len(nodes) for n in nodes}
    else:
        restart = {n: v / total for n, v in restart.items()}
    out_w = {n: sum(adj[n].values()) for n in nodes}
    rank = dict(restart)
    for _ in range(iterations):
        nxt = {n: (1.0 - damping) * restart[n] for n in nodes}
        for n in nodes:
            if out_w[n] <= 0:
                continue
            share = damping * rank[n] / out_w[n]
            for m, w in adj[n].items():
                nxt[m] += share * w
        delta = sum(abs(nxt[n] - rank[n]) for n in nodes)
        rank = nxt
        if delta < tol:
            break
    return rank


def _minmax(values: List[float]) -> List[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [1.0 if hi > 0 else 0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def rank_facts(rows: Sequence[Any], query: str, entities: Iterable[str] = ()) -> List[Tuple[Any, float]]:
    """(row, score) sorted best first. Rows need subject/object/confidence/verified/recorded_at/last_accessed."""
    q_tokens = tokens(query)
    ent_norms = {norm(e) for e in entities if norm(e)}
    edges = [(norm(r["subject"]), norm(r["object"]), float(r["confidence"] or 0.5)) for r in rows]
    node_names = {n for a, b, _ in edges for n in (a, b)}
    seeds: Dict[str, float] = {}
    for n in node_names:
        weight = 0.0
        if n in ent_norms:
            weight += 2.0
        elif any(e and (e in n or n in e) for e in ent_norms if len(e) >= 3 and len(n) >= 3):
            weight += 1.0
        overlap = len(tokens(n) & q_tokens)
        if overlap:
            weight += 0.5 * overlap
        if weight:
            seeds[n] = weight
    ppr = personalized_pagerank(edges, seeds)
    now = time.time()
    rel_raw, rec_raw, imp_raw = [], [], []
    for r in rows:
        s, o = norm(r["subject"]), norm(r["object"])
        lexical = len(tokens(f"{r['subject']} {r['relation'].replace('_', ' ')} {r['object']}") & q_tokens)
        rel_raw.append(ppr.get(s, 0.0) + ppr.get(o, 0.0) + (0.05 * lexical if seeds else 0.1 * lexical))
        last = float(r["last_accessed"] or r["recorded_at"] or now)
        hours = max(0.0, (now - last) / 3600.0)
        rec_raw.append(_RECENCY_DECAY_PER_HOUR ** hours)
        imp_raw.append(float(r["confidence"] or 0.0) + (0.25 if r["verified"] else 0.0))
    rel, rec, imp = _minmax(rel_raw), _minmax(rec_raw), _minmax(imp_raw)
    # Relevance gets weight 1 like the paper's equal weights; with seeds it dominates ties, without seeds it is flat.
    scored = [(row, rel[i] * (1.5 if seeds else 1.0) + rec[i] + imp[i]) for i, row in enumerate(rows)]
    scored.sort(key=lambda x: (x[1], float(x[0]["recorded_at"])), reverse=True)
    return scored


# ---------------------------------------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------------------------------------

def default_projects_dir() -> str:
    env = os.getenv("PHRONESIS_PROJECTS_DIR")
    if env:
        return env
    mem = os.getenv("PHRONESIS_MEMORY_DIR")
    if mem:
        return mem
    from ..paths import data_dir

    root = data_dir("memory")
    if root is not None:
        return str(root)
    if "PYTEST_CURRENT_TEST" in os.environ:
        return os.path.join(tempfile.gettempdir(), "phronesis_projects_pytest")
    workspace_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    return os.path.join(workspace_root, ".phronesis_memory")


_STORES: Dict[str, ProjectStore] = {}
_STORES_LOCK = threading.Lock()


def get_project_store(storage_dir: Optional[str] = None) -> ProjectStore:
    """Process-wide store per directory; the directory is re-read on each call (tests point it at tmp_path)."""
    path = os.path.abspath(storage_dir or default_projects_dir())
    with _STORES_LOCK:
        store = _STORES.get(path)
        if store is None:
            store = ProjectStore(path)
            _STORES[path] = store
        return store


__all__ = [
    "FACT_ID_RE",
    "PROJECT_ID_RE",
    "SINGLE_VALUED_RELATIONS",
    "VALUED_RELATIONS",
    "FactInput",
    "ProjectStore",
    "ProjectStoreError",
    "get_project_store",
    "personalized_pagerank",
    "rank_facts",
]
