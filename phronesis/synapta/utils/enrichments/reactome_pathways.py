#!/usr/bin/env python3

"""
Enrichment class for enriching Reactome pathways with textual descriptions
"""

import logging

import hydra
import requests

from .enrichments import Enrichments

# Initialize logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _summation_text(pathway_id: str, body: str) -> str:
    """
    The summation text of a Reactome ContentService `/summation` plain-text response
    ("<stId>\t<summation text>"). Raises ValueError for any other body shape instead of an
    IndexError (KG-016).
    """
    fields = (body or "").strip("\r\n").split("\t")
    if len(fields) < 2 or not fields[1].strip():
        raise ValueError(
            f"unexpected Reactome /summation response for {pathway_id!r}: expected "
            f"'<stId><TAB><text>', got {(body or '')[:120]!r}"
        )
    return fields[1]


class EnrichmentWithReactome(Enrichments):
    """
    Enrichment class using Reactome pathways
    """

    def enrich_documents(self, texts: list[str]) -> list[str]:
        """
        Enrich a list of input Reactome pathways

        Args:
            texts: The list of Reactome pathways to be enriched.

        Returns:
            The list of enriched descriptions
        """

        reactome_pathways_ids = texts

        logger.log(logging.INFO, "Load Hydra configuration for reactome enrichment")
        with hydra.initialize(version_base=None, config_path="../../configs"):
            cfg = hydra.compose(
                config_name="config",
                overrides=["utils/enrichments/reactome_pathways=default"],
            )
            cfg = cfg.utils.enrichments.reactome_pathways

        descriptions = []
        for reactome_pathway_id in reactome_pathways_ids:
            r = requests.get(
                cfg.base_url + reactome_pathway_id + "/summation",
                headers={"Accept": "text/plain"},
                timeout=cfg.timeout,
            )
            # if the response is not ok
            if not r.ok:
                descriptions.append(None)
                continue
            descriptions.append(_summation_text(reactome_pathway_id, r.text))
        return descriptions

    def enrich_documents_with_rag(self, texts, docs):
        """
        Enrich a list of input Reactome pathways

        Args:
            texts: The list of Reactome pathways to be enriched.

        Returns:
            The list of enriched descriptions
        """
        return self.enrich_documents(texts)
