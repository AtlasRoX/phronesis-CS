"""
Singleton manager for Milvus connections and vector stores.
Handles connection reuse, event loops, and GPU detection caching.
"""

import asyncio
import logging
import os
import threading
from typing import Any, Optional

from langchain_core.embeddings import Embeddings
from langchain_milvus import Milvus
from pymilvus import connections, db, utility
from pymilvus.exceptions import MilvusException

from .gpu_detection import detect_nvidia_gpu

logger = logging.getLogger(__name__)


class VectorstoreSingleton:
    """Singleton manager for Milvus connections and vector stores."""

    _instance: Optional["VectorstoreSingleton"] = None
    _lock: threading.Lock = threading.Lock()
    _connections: dict[str, str] = {}  # Store connections by connection string
    _vector_stores: dict[str, Milvus] = {}  # Store vector stores by collection name
    _event_loops: dict[int, asyncio.AbstractEventLoop] = {}  # Store event loops by thread ID
    _gpu_detected: Optional[bool] = None  # Cache GPU detection result

    def __new__(cls) -> "VectorstoreSingleton":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def get_event_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create event loop for current thread."""
        thread_id = threading.get_ident()

        if thread_id not in self._event_loops:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    raise RuntimeError("Event loop is closed")
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            self._event_loops[thread_id] = loop
            logger.info("Created new event loop for thread %s", thread_id)

        return self._event_loops[thread_id]

    def detect_gpu_once(self) -> bool:
        """Detect GPU availability once and cache the result."""
        if self._gpu_detected is None:
            self._gpu_detected = detect_nvidia_gpu()
            gpu_status = "available" if self._gpu_detected else "not available"
            logger.info("GPU detection completed: NVIDIA GPU %s", gpu_status)
        return self._gpu_detected

    def get_connection(
        self,
        host: str,
        port: int,
        db_name: str,
        uri: str | None = None,
        token: str | None = None,
    ) -> str:
        """Get or create a Milvus connection (supports Cloud Milvus like Zilliz Cloud & Milvus Lite)."""
        env_uri = os.getenv("MILVUS_URI")
        env_token = os.getenv("MILVUS_TOKEN")
        resolved_uri = uri or env_uri
        resolved_token = token or env_token

        conn_key = resolved_uri if resolved_uri else f"{host}:{port}/{db_name}"

        if conn_key not in self._connections:
            try:
                # Check if already connected
                if connections.has_connection("default"):
                    connections.remove_connection("default")

                # Connect to Milvus via Cloud URI or host/port
                if resolved_uri:
                    connect_kwargs: dict[str, Any] = {"alias": "default", "uri": resolved_uri}
                    if resolved_token:
                        connect_kwargs["token"] = resolved_token
                    connections.connect(**connect_kwargs)
                    logger.info("Connected to Milvus via URI: %s", resolved_uri)
                else:
                    connections.connect(
                        alias="default",
                        host=host,
                        port=port,
                    )
                    logger.info("Connected to Milvus at %s:%s", host, port)

                # Check if database exists, create and use if supported
                try:
                    existing_dbs = db.list_database()
                    if db_name not in existing_dbs:
                        db.create_database(db_name)
                        logger.info("Created database: %s", db_name)

                    # Use the database
                    db.using_database(db_name)
                    logger.info("Using database: %s", db_name)
                    logger.debug(
                        "Milvus DB switched to: %s, available collections: %s",
                        db_name,
                        utility.list_collections(),
                    )
                except Exception as db_err:
                    logger.debug(
                        "Database creation/selection skipped (e.g. Milvus Lite/default): %s",
                        db_err,
                    )

                self._connections[conn_key] = "default"

            except MilvusException as e:
                logger.error("Failed to connect to Milvus: %s", e)
                raise

        return self._connections[conn_key]

    def get_vector_store(
        self,
        collection_name: str,
        embedding_model: Embeddings,
        connection_args: dict[str, Any],
    ) -> Milvus:
        """Get or create a vector store for a collection."""
        if collection_name not in self._vector_stores:
            # Ensure event loop exists for this thread
            self.get_event_loop()

            # Create LangChain Milvus instance with explicit URI format
            resolved_uri = connection_args.get("uri") or os.getenv("MILVUS_URI")
            resolved_token = connection_args.get("token") or os.getenv("MILVUS_TOKEN")
            milvus_uri = (
                resolved_uri
                or f"http://{connection_args.get('host', 'localhost')}:{connection_args.get('port', 19530)}"
            )

            conn_args: dict[str, Any] = {
                "uri": milvus_uri,
            }
            if resolved_token:
                conn_args["token"] = resolved_token
            if "host" in connection_args:
                conn_args["host"] = connection_args["host"]
            if "port" in connection_args:
                conn_args["port"] = connection_args["port"]

            vector_store = Milvus(
                embedding_function=embedding_model,
                collection_name=collection_name,
                connection_args=conn_args,
                text_field="text",
                auto_id=False,
                drop_old=False,
                consistency_level="Strong",
            )

            self._vector_stores[collection_name] = vector_store
            logger.info(
                "Created new vector store for collection: %s with URI: %s",
                collection_name,
                milvus_uri,
            )

        return self._vector_stores[collection_name]
