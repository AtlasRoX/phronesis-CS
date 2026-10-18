#!/usr/bin/env python3

"""
A tool to fetch studies from the Single Cell Portal.
"""

import html
import logging
import time
from typing import Annotated, Any, Dict, List, Optional

import pandas as pd
import requests
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langchain_core.tools.base import InjectedToolCallId
from langgraph.types import Command

# Initialize logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SCP_ENDPOINT = "https://singlecell.broadinstitute.org/single_cell/api/v1/search?type=study"
SCP_BASE_URL = "https://singlecell.broadinstitute.org"
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 1.0
MAX_LIMIT = 50
_RETRY_STATUS = {429, 500, 502, 503, 504}
_COLUMNS = ["study_source", "name", "study_url", "gene_count", "cell_count"]


def fetch_studies(search_term: str, limit: int = 5, timeout: float = 10.0) -> List[Dict[str, Any]]:
    """GET the SCP study search with bounded retries (429/5xx/transport errors only); raises on failure."""
    params = {"terms": search_term}
    last_error: Optional[str] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(SCP_ENDPOINT, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            logger.info("Status code %s received from SCP (attempt %d)", response.status_code, attempt)
            if response.status_code == 200:
                studies = (response.json() or {}).get("studies") or []
                return list(studies)[: max(0, limit)]
            last_error = f"HTTP {response.status_code}"
            if response.status_code not in _RETRY_STATUS:
                break
        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
    raise RuntimeError(f"Single Cell Portal search failed after retries ({last_error})")


def studies_table(studies: List[Dict[str, Any]]) -> str:
    """Markdown grid of the studies, with the study name linked to its SCP page."""
    df = pd.DataFrame(studies)
    for column in _COLUMNS:
        if column not in df.columns:
            df[column] = ""
    df = df[_COLUMNS]
    df["name"] = df.apply(
        lambda x: f'<a href="{SCP_BASE_URL}/{html.escape(str(x["study_url"]), quote=True)}">'
        f'{html.escape(str(x["name"]))}</a>',
        axis=1,
    )
    df = df.drop(columns=["study_url"])
    df.insert(0, "S/N", range(1, 1 + len(df)))
    return df.to_markdown(tablefmt="grid")


@tool("search_studies")
def search_studies(
    search_term: str, tool_call_id: Annotated[str, InjectedToolCallId], limit: int = 5
):
    """
    Fetch studies from single cell portal

    Args:
        search_term (str): The search term to use. Example: "COVID-19", "cancer", etc.
        limit (int): The number of papers to return. Default is 5.

    """
    logger.log(logging.INFO, "Calling the tool search_studies")
    limit = max(1, min(int(limit), MAX_LIMIT))
    try:
        studies = fetch_studies(search_term, limit)
    except (RuntimeError, ValueError) as exc:
        message = f"Could not fetch studies on {search_term}: {exc}"
        return Command(
            update={
                "search_table": "",
                "messages": [ToolMessage(message, tool_call_id=tool_call_id)],
            }
        )

    if not studies:
        message = f"No Single Cell Portal studies matched {search_term}."
        table = ""
    else:
        message = f"Successfully fetched {len(studies)} studies on {search_term}."
        table = studies_table(studies)

    # Update the state key 'search_table' with the dataframe in markdown format
    return Command(
        update={
            # update the state keys
            "search_table": table,
            # update the message history
            "messages": [ToolMessage(message, tool_call_id=tool_call_id)],
        }
    )
