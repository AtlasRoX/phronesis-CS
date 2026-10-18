"""
Embedding model identity and vector size for the PDF RAG collection.

A Milvus collection has a fixed vector size, so the size comes from the model itself (one probe embedding)
and the collection name carries the model, so two models never write into the same collection.
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DIM = 768


def model_identity(embedding_model: Any) -> str | None:
    """The model's configured name (e.g. 'text-embedding-3-small'), or None when it has none."""
    for attr in ("model", "model_name", "model_id"):
        value = getattr(embedding_model, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_embedding_dim(embedding_model: Any, config: Any = None) -> tuple[int, bool]:
    """(vector size, measured). Measured from one probe embedding; the configured size is only a fallback."""
    try:
        size = len(embedding_model.embed_query("dimension probe"))
        if isinstance(size, int) and size > 0:
            return size, True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not measure the embedding size (%s); using the configured size", exc)
    configured = getattr(getattr(config, "milvus", None), "embedding_dim", None) if config else None
    return (int(configured) if isinstance(configured, int) and configured > 0 else _DEFAULT_DIM), False


def collection_name_for(base: str, embedding_model: Any, dim: int, measured: bool) -> str:
    """'pdf_rag_documents_text_embedding_3_small_1536'; the base name alone when nothing was measured."""
    if not measured:
        return base
    name = model_identity(embedding_model)
    parts = [base] + ([re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()] if name else []) + [str(dim)]
    return "_".join(p for p in parts if p)[:255]
