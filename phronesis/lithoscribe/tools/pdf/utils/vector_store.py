"""
Vectorstore class for managing PDF embeddings with Milvus.
Manages GPU normalization and similarity search and MMR operations.
With automatic handling of COSINE to IP conversion for GPU compatibility.
Supports both GPU and CPU configurations.
"""

import logging
import os
import time
from typing import Any

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_milvus import Milvus

from .collection_manager import ensure_collection_exists
from .embedding_info import collection_name_for, resolve_embedding_dim
from .gpu_detection import (
    detect_nvidia_gpu,
    get_optimal_index_config,
    log_index_configuration,
)
from .singleton_manager import VectorstoreSingleton
from .vector_normalization import wrap_embedding_model_if_needed

# Set up logging with configurable level
log_level = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=getattr(logging, log_level))
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, log_level))


class Vectorstore:
    """
    Enhanced Vectorstore class with GPU normalization support.
    Automatically handles COSINE -> IP conversion for GPU compatibility.
    """

    def __init__(
        self,
        embedding_model: Embeddings,
        metadata_fields: list[str] | None = None,
        config: Any = None,
    ):
        """
        Initialize the document store with Milvus and GPU optimization.

        Args:
            embedding_model: The embedding model to use
            metadata_fields: Fields to include in document metadata
            config: Configuration object containing Milvus connection details
        """
        self.config = config
        self.metadata_fields = metadata_fields or [
            "title",
            "paper_id",
            "page",
            "chunk_id",
        ]
        self.initialization_time = time.time()

        # GPU detection with config override (SINGLE CALL)
        self.has_gpu = detect_nvidia_gpu(config)

        # Additional check for force CPU mode
        if (
            config
            and hasattr(config, "gpu_detection")
            and getattr(config.gpu_detection, "force_cpu_mode", False)
        ):
            logger.info("Running in forced CPU mode (config override)")
            self.has_gpu = False

        # Determine if we want to use COSINE similarity
        self.use_cosine = True  # Default preference
        if config and hasattr(config, "similarity_metric"):
            self.use_cosine = getattr(config.similarity_metric, "use_cosine", True)

        # Wrap embedding model with normalization if needed for GPU
        self.original_embedding_model = embedding_model
        self.embedding_model = wrap_embedding_model_if_needed(
            embedding_model, self.has_gpu, self.use_cosine
        )

        # Configure index parameters AFTER determining GPU usage and normalization. The vector size is
        # measured from the model: a fixed configured size fails on every insert for a different model.
        embedding_dim, measured = resolve_embedding_dim(embedding_model, config)
        self.embedding_dim = embedding_dim
        self.index_params, self.search_params = get_optimal_index_config(
            self.has_gpu, embedding_dim, self.use_cosine
        )

        # Log the configuration
        log_index_configuration(self.index_params, self.search_params, self.use_cosine)

        # Track loaded papers to prevent duplicate loading
        self.loaded_papers = set()

        # Initialize Milvus connection parameters with environment variable fallback
        env_uri = os.getenv("MILVUS_URI")
        env_token = os.getenv("MILVUS_TOKEN")
        cfg_uri = (
            getattr(config.milvus, "uri", None)
            if config and hasattr(config, "milvus")
            else None
        )
        cfg_token = (
            getattr(config.milvus, "token", None)
            if config and hasattr(config, "milvus")
            else None
        )

        self.connection_args = {
            "host": (
                config.milvus.host
                if config and hasattr(config, "milvus") and hasattr(config.milvus, "host")
                else os.getenv("MILVUS_HOST", "127.0.0.1")
            ),
            "port": (
                config.milvus.port
                if config and hasattr(config, "milvus") and hasattr(config.milvus, "port")
                else int(os.getenv("MILVUS_PORT", "19530"))
            ),
        }
        if env_uri or cfg_uri:
            self.connection_args["uri"] = env_uri or cfg_uri
        if env_token or cfg_token:
            self.connection_args["token"] = env_token or cfg_token

        # Log the connection parameters being used
        logger.info(
            "Using Milvus connection: URI=%s, host=%s, port=%s",
            self.connection_args.get("uri"),
            self.connection_args["host"],
            self.connection_args["port"],
        )
        self.collection_name = collection_name_for(
            config.milvus.collection_name if config else "pdf_rag_documents",
            embedding_model,
            embedding_dim,
            measured,
        )
        self.db_name = config.milvus.db_name if config else "pdf_rag_db"

        # Get singleton instance
        self._singleton = VectorstoreSingleton()

        # Connect to Milvus (reuses existing connection if available)
        self._connect_milvus()

        # Create collection with proper metric type
        self.collection = ensure_collection_exists(
            self.collection_name,
            self.config,
            self.index_params,
            self.has_gpu,
            embedding_dim=embedding_dim,
        )

        # Initialize the LangChain Milvus vector store
        self.vector_store = self._initialize_vector_store()

        # Load existing papers AFTER vector store is ready
        self._load_existing_paper_ids()

        # CRITICAL: Load collection into memory/GPU after any existing data is identified
        logger.info(
            "Calling _ensure_collection_loaded() for %s processing...",
            "GPU" if self.has_gpu else "CPU",
        )
        self._ensure_collection_loaded()

        # Store for document metadata (keeping for compatibility)
        self.documents: dict[str, Document] = {}
        self.paper_metadata: dict[str, dict[str, Any]] = {}

        # Log final configuration
        metric_info = (
            "IP (normalized for COSINE)"
            if self.has_gpu and self.use_cosine
            else self.index_params["metric_type"]
        )

        logger.info(
            "Milvus vector store initialized with collection: %s (GPU: %s, Metric: %s)",
            self.collection_name,
            "enabled" if self.has_gpu else "disabled",
            metric_info,
        )

    def _connect_milvus(self) -> None:
        """Establish connection to Milvus server using singleton."""
        self._singleton.get_connection(
            self.connection_args["host"],
            self.connection_args["port"],
            self.db_name,
            uri=self.connection_args.get("uri"),
            token=self.connection_args.get("token"),
        )

    def _initialize_vector_store(self) -> Milvus:
        """Initialize or load the Milvus vector store with proper embedding model."""
        # Use the wrapped embedding model (with normalization if needed)
        vector_store = self._singleton.get_vector_store(
            self.collection_name, self.embedding_model, self.connection_args
        )

        return vector_store

    def _load_existing_paper_ids(self):
        """Load already embedded paper IDs using LangChain's collection access."""
        logger.info("Checking for existing papers via LangChain collection...")

        # Access the collection through LangChain's wrapper
        langchain_collection = getattr(self.vector_store, "col", None)

        if langchain_collection is None:
            langchain_collection = getattr(self.vector_store, "collection", None)

        if langchain_collection is None:
            logger.warning("No LangChain collection found, proceeding with empty loaded_papers")
            return

        # Force flush and check entity count
        langchain_collection.flush()
        num_entities = langchain_collection.num_entities

        logger.info("LangChain collection entity count: %d", num_entities)

        if num_entities > 0:
            logger.info("Loading existing paper IDs from LangChain collection...")
            existing_paper_ids = self._query_all_paper_ids(langchain_collection)
            self.loaded_papers.update(existing_paper_ids)

            logger.info("Found %d unique papers in collection", len(existing_paper_ids))
        else:
            logger.info("Collection is empty - no existing papers")

    @staticmethod
    def _query_all_paper_ids(collection: Any, batch_size: int = 4096) -> set[str]:
        """Every stored paper_id. A single query returns at most 16384 rows, which would make papers past
        that point look new and be embedded again, so rows are read with a query iterator."""
        iterator_factory = getattr(collection, "query_iterator", None)
        if callable(iterator_factory):
            try:
                iterator = iterator_factory(batch_size=batch_size, expr="", output_fields=["paper_id"])
                ids: set[str] = set()
                try:
                    while True:
                        batch = iterator.next()
                        if not batch:
                            break
                        ids.update(row["paper_id"] for row in batch)
                finally:
                    iterator.close()
                return ids
            except Exception as exc:  # noqa: BLE001
                logger.warning("Query iterator unavailable (%s); reading the first 16384 rows only", exc)
        results = collection.query(
            expr="",  # No filter - get all
            output_fields=["paper_id"],
            limit=16384,  # Max limit of a single query
            consistency_level="Strong",
        )
        if len(results) >= 16384:
            logger.warning("Paper id listing hit the 16384-row query limit; some papers may be re-embedded")
        return {result["paper_id"] for result in results}

    @staticmethod
    def _filter_expr(filter_: dict[str, Any] | None) -> str | None:
        """Milvus boolean expression for a metadata filter; string values are escaped."""
        if not filter_:
            return None

        def literal(value: Any) -> str:
            if isinstance(value, str):
                return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
            return str(value)

        conditions = []
        for key, value in filter_.items():
            if isinstance(value, list):
                conditions.append(f"{key} in [{', '.join(literal(v) for v in value)}]")
            else:
                conditions.append(f"{key} == {literal(value)}")
        return " and ".join(conditions)

    def similarity_search(self, query: str, **kwargs: Any) -> list[Document]:
        """
        Perform similarity search on the vector store.
        Query embedding will be automatically normalized if using GPU with COSINE.
        Keyword args:
            k: int = 4
            filter: Optional[Dict[str, Any]] = None
            plus any other kwargs to pass through to the underlying vector_store.
        """
        # Extract our parameters
        k: int = kwargs.pop("k", 4)
        filter_: dict[str, Any] | None = kwargs.pop("filter", None)

        # Build Milvus expr from filter_, if present
        expr = self._filter_expr(filter_)

        # Delegate to the wrapped store
        return self.vector_store.similarity_search(query=query, k=k, expr=expr, **kwargs)

    def max_marginal_relevance_search(self, query: str, **kwargs: Any) -> list[Document]:
        """
        Perform MMR search on the vector store.
        Query embedding will be automatically normalized if using GPU with COSINE.
        Keyword args:
            k: int = 4
            fetch_k: int = 20
            lambda_mult: float = 0.5
            filter: Optional[Dict[str, Any]] = None
            plus any other kwargs to pass through.
        """
        # Extract our parameters
        k: int = kwargs.pop("k", 4)
        fetch_k: int = kwargs.pop("fetch_k", 20)
        lambda_mult: float = kwargs.pop("lambda_mult", 0.5)
        filter_: dict[str, Any] | None = kwargs.pop("filter", None)

        # Build Milvus expr from filter_, if present
        expr = self._filter_expr(filter_)

        # Delegate to the wrapped store
        return self.vector_store.max_marginal_relevance_search(
            query=query,
            k=k,
            fetch_k=fetch_k,
            lambda_mult=lambda_mult,
            expr=expr,
            **kwargs,
        )

    def _ensure_collection_loaded(self):
        """Ensure collection is loaded into memory/GPU after data insertion."""
        # Get the collection
        collection = getattr(self.vector_store, "col", None)
        if collection is None:
            collection = getattr(self.vector_store, "collection", None)

        if collection is None:
            logger.warning("Cannot access collection for loading")
            return

        # Force flush to ensure we see all data
        logger.info("Flushing collection to ensure data visibility...")
        collection.flush()

        # Check entity count after flush
        num_entities = collection.num_entities
        logger.info("Collection entity count after flush: %d", num_entities)

        if num_entities > 0:
            hardware_type = "GPU" if self.has_gpu else "CPU"
            logger.info(
                "Loading collection with %d entities into %s memory...",
                num_entities,
                hardware_type,
            )

            # Load collection into memory (CPU or GPU)
            collection.load()

            # Verify loading was successful
            final_count = collection.num_entities
            logger.info(
                "Collection successfully loaded into %s memory with %d entities",
                hardware_type,
                final_count,
            )
        else:
            logger.info("Collection is empty, skipping load operation")

    def get_embedding_info(self) -> dict[str, Any]:
        """Get information about the embedding configuration."""
        return {
            "has_gpu": self.has_gpu,
            "use_cosine": self.use_cosine,
            "metric_type": self.index_params["metric_type"],
            "index_type": self.index_params["index_type"],
            "normalization_enabled": hasattr(self.embedding_model, "normalize_for_gpu"),
            "original_model_type": type(self.original_embedding_model).__name__,
            "wrapped_model_type": type(self.embedding_model).__name__,
        }
