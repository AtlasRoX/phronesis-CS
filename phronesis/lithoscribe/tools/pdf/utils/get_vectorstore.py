"""
Create or retrieve a Vectorstore instance for PDF RAG.
"""

import logging
import threading
from typing import Any

from langchain_core.embeddings import Embeddings

from .embedding_info import model_identity
from .vector_store import Vectorstore

logger = logging.getLogger(__name__)

# Global cache for Vectorstore instances
_vectorstore_cache = {}
_cache_lock = threading.Lock()


def get_vectorstore(
    embedding_model: Embeddings, config: Any, force_new: bool = False
) -> "Vectorstore":
    """
    Factory function to get or create a Vectorstore instance.
    Ensures the same instance is reused across the application.

    Args:
        embedding_model: The embedding model to use
        config: Configuration object
        force_new: Force creation of a new instance

    Returns:
        Vectorstore instance
    """
    collection_name = config.milvus.collection_name if config else "pdf_rag_documents"
    # One instance per (collection, embedding model): vectors from different models are not comparable, so a
    # model change gets its own instance (and collection) instead of swapping the model on a shared one.
    cache_key = f"{collection_name}::{model_identity(embedding_model) or id(embedding_model)}"

    with _cache_lock:
        if force_new and cache_key in _vectorstore_cache:
            del _vectorstore_cache[cache_key]
            logger.info("Forced new Vectorstore instance for collection: %s", collection_name)

        if cache_key not in _vectorstore_cache:
            logger.info("Creating new Vectorstore instance for collection: %s", collection_name)
            _vectorstore_cache[cache_key] = Vectorstore(embedding_model=embedding_model, config=config)
        else:
            logger.info("Reusing existing Vectorstore instance for collection: %s", collection_name)

        return _vectorstore_cache[cache_key]
