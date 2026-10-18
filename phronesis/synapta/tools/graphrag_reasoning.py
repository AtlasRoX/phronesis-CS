"""
Tool for performing Graph RAG reasoning.
"""

import logging
import os
from collections import OrderedDict
from typing import Annotated

import hydra
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain.chains.retrieval import create_retrieval_chain
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader
from langchain_core.messages import ToolMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import BaseTool
from langchain_core.tools.base import InjectedToolCallId
from langchain_core.vectorstores import InMemoryVectorStore
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import BaseModel, Field

# Initialize logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Vector stores built from uploaded PDFs, keyed by file signatures, splitter settings and embedding
# model, so repeated questions over the same documents do not re-embed them.
_VECTORSTORE_CACHE: "OrderedDict[tuple, InMemoryVectorStore]" = OrderedDict()
_VECTORSTORE_CACHE_SIZE = 8


def _file_signature(path: str) -> tuple:
    try:
        stat = os.stat(path)
        return (os.path.abspath(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return (os.path.abspath(path), None, None)


def _embedding_key(embedding_model) -> tuple:
    model_name = getattr(embedding_model, "model", None) or getattr(
        embedding_model, "model_name", None
    )
    return (type(embedding_model).__name__, str(model_name), id(embedding_model))


def clear_vectorstore_cache() -> None:
    """Forget cached document embeddings."""
    _VECTORSTORE_CACHE.clear()


def _response_answer(response) -> str:
    """The answer text of a retrieval chain response (dict with "answer"), or the response itself."""
    if isinstance(response, dict):
        return str(response.get("answer", ""))
    return str(response)


class GraphRAGReasoningInput(BaseModel):
    """
    GraphRAGReasoningInput is a Pydantic model representing an input for Graph RAG reasoning.

    Args:
        state: Injected state.
        prompt: Prompt to interact with the backend.
        extraction_name: Name assigned to the subgraph extraction process
    """

    tool_call_id: Annotated[str, InjectedToolCallId] = Field(description="Tool call ID.")
    state: Annotated[dict, InjectedState] = Field(description="Injected state.")
    prompt: str = Field(description="Prompt to interact with the backend.")
    extraction_name: str = Field(
        description="""Name assigned to the subgraph extraction process
                    when the subgraph_extraction tool is invoked."""
    )


class GraphRAGReasoningTool(BaseTool):
    """
    This tool performs reasoning using a Graph Retrieval-Augmented Generation (RAG) approach
    over user's request by considering textualized subgraph context and document context.
    """

    name: str = "graphrag_reasoning"
    description: str = """A tool to perform reasoning using a Graph RAG approach
                        by considering textualized subgraph context and document context."""
    args_schema: type[BaseModel] = GraphRAGReasoningInput

    def _run(
        self,
        tool_call_id: Annotated[str, InjectedToolCallId],
        state: Annotated[dict, InjectedState],
        prompt: str,
        extraction_name: str,
    ):
        """
        Run the Graph RAG reasoning tool.

        Args:
            tool_call_id: The tool call ID.
            state: The injected state.
            prompt: The prompt to interact with the backend.
            extraction_name: The name assigned to the subgraph extraction process.
        """
        logger.log(logging.INFO, "Invoking graphrag_reasoning tool for %s", extraction_name)

        # The subgraph must exist before any documents are embedded
        extracted_graph = {dic["name"]: dic for dic in state.get("dic_extracted_graph", [])}
        if extraction_name not in extracted_graph:
            available = ", ".join(sorted(extracted_graph)) or "none"
            message = (
                f"No extracted subgraph named '{extraction_name}'. "
                f"Available extractions: {available}. Run subgraph_extraction first or use "
                "one of the available names."
            )
            logger.warning(message)
            return Command(
                update={"messages": [ToolMessage(content=message, tool_call_id=tool_call_id)]}
            )

        # Load Hydra configuration
        with hydra.initialize(version_base=None, config_path="../configs"):
            cfg = hydra.compose(
                config_name="config", overrides=["tools/graphrag_reasoning=default"]
            )
            cfg = cfg.tools.graphrag_reasoning

        vector_store = self._document_store(state, cfg)

        # Set another prompt template
        prompt_template = ChatPromptTemplate.from_messages(
            [("system", cfg.prompt_graphrag_w_docs), ("human", "{input}")]
        )

        # Prepare chain with retrieved documents
        qa_chain = create_stuff_documents_chain(state["llm_model"], prompt_template)
        rag_chain = create_retrieval_chain(
            vector_store.as_retriever(
                search_type=cfg.retriever_search_type,
                search_kwargs={
                    "k": cfg.retriever_k,
                    "fetch_k": cfg.retriever_fetch_k,
                    "lambda_mult": cfg.retriever_lambda_mult,
                },
            ),
            qa_chain,
        )

        # Invoke the chain
        response = rag_chain.invoke(
            {
                "input": prompt,
                "subgraph_summary": extracted_graph[extraction_name]["graph_summary"],
            }
        )

        return Command(
            update={
                # update the message history with the answer only (not the retrieved documents)
                "messages": [
                    ToolMessage(content=_response_answer(response), tool_call_id=tool_call_id)
                ]
            }
        )

    @staticmethod
    def _document_store(state: dict, cfg) -> InMemoryVectorStore:
        """Embed the uploaded drug PDFs once per (files, splitter, embedding model)."""
        paths = [
            f["file_path"]
            for f in state.get("uploaded_files", [])
            if f.get("file_type") == "drug_data"
        ]
        key = (
            tuple(_file_signature(p) for p in paths),
            cfg.splitter_chunk_size,
            cfg.splitter_chunk_overlap,
            _embedding_key(state["embedding_model"]),
        )
        cached = _VECTORSTORE_CACHE.get(key)
        if cached is not None:
            _VECTORSTORE_CACHE.move_to_end(key)
            return cached

        all_docs = []
        for path in paths:
            raw_documents = PyPDFLoader(file_path=path).load()
            # May need to find an optimal chunk size and overlap configuration
            all_docs.extend(
                RecursiveCharacterTextSplitter(
                    chunk_size=cfg.splitter_chunk_size,
                    chunk_overlap=cfg.splitter_chunk_overlap,
                ).split_documents(raw_documents)
            )
        store = InMemoryVectorStore.from_documents(
            documents=all_docs, embedding=state["embedding_model"]
        )
        _VECTORSTORE_CACHE[key] = store
        while len(_VECTORSTORE_CACHE) > _VECTORSTORE_CACHE_SIZE:
            _VECTORSTORE_CACHE.popitem(last=False)
        return store
