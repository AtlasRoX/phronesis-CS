"""
Unit tests for BioContext MCP Client and biomedical tools.
Tests OpenTargets, KEGG, EuropePMC, UniProt, Ensembl, and general BioContext tool integration.
"""

import json
from unittest.mock import MagicMock, patch
import pytest

from ...utils.biocontext import BioContextMCPClient
from ...tools.biocontext import (
    BioContextEnsemblTool,
    BioContextEuropePMCTool,
    BioContextKEGGTool,
    BioContextOpenTargetsTool,
    BioContextPanglaoDBTool,
    BioContextTool,
    BioContextUniProtTool,
)


@pytest.fixture
def mock_biocontext_client():
    """Mock the BioContextMCPClient response methods."""
    with patch("phronesis.tools.biocontext.opentargets.BioContextMCPClient") as mock_ot_cls, \
         patch("phronesis.tools.biocontext.kegg.BioContextMCPClient") as mock_kegg_cls, \
         patch("phronesis.tools.biocontext.europepmc.BioContextMCPClient") as mock_epmc_cls, \
         patch("phronesis.tools.biocontext.uniprot.BioContextMCPClient") as mock_uniprot_cls, \
         patch("phronesis.tools.biocontext.ensembl.BioContextMCPClient") as mock_ensembl_cls, \
         patch("phronesis.tools.biocontext.panglaodb.BioContextMCPClient") as mock_panglao_cls, \
         patch("phronesis.tools.biocontext.general.BioContextMCPClient") as mock_gen_cls:

        mock_ot = MagicMock()
        mock_ot.query_opentargets.return_value = {
            "source": "OpenTargets (BioContext)",
            "query": "TNF",
            "total_hits": 1,
            "results": [{"id": "ENSG00000232810", "name": "TNF", "entity": "target"}],
        }
        mock_ot_cls.return_value = mock_ot

        mock_kegg = MagicMock()
        mock_kegg.query_kegg.return_value = {
            "source": "KEGG (BioContext)",
            "results": [{"id": "path:hsa04010", "description": "MAPK signaling pathway"}],
        }
        mock_kegg_cls.return_value = mock_kegg

        mock_epmc = MagicMock()
        mock_epmc.query_europepmc.return_value = {
            "source": "EuropePMC (BioContext)",
            "count": 1,
            "articles": [{"id": "PMC123456", "title": "Target discovery in inflammatory disease"}],
        }
        mock_epmc_cls.return_value = mock_epmc

        mock_uniprot = MagicMock()
        mock_uniprot.query_uniprot.return_value = {
            "source": "UniProt (BioContext)",
            "count": 1,
            "proteins": [{"accession": "P01375", "entry_id": "TNFA_HUMAN", "protein_name": "Tumor necrosis factor"}],
        }
        mock_uniprot_cls.return_value = mock_uniprot

        mock_ensembl = MagicMock()
        mock_ensembl.query_ensembl.return_value = {
            "source": "Ensembl (BioContext)",
            "gene_symbol": "TNF",
            "id": "ENSG00000232810",
        }
        mock_ensembl_cls.return_value = mock_ensembl

        mock_panglao = MagicMock()
        mock_panglao.query_panglaodb.return_value = {
            "source": "PanglaoDB / CellMarkers (BioContext)",
            "query": "CD3D",
            "status": "annotated",
        }
        mock_panglao_cls.return_value = mock_panglao

        mock_gen = MagicMock()
        mock_gen.query_opentargets.return_value = {"source": "OpenTargets", "results": ["target1"]}
        mock_gen_cls.return_value = mock_gen

        yield {
            "ot": mock_ot,
            "kegg": mock_kegg,
            "epmc": mock_epmc,
            "uniprot": mock_uniprot,
            "ensembl": mock_ensembl,
            "panglao": mock_panglao,
            "gen": mock_gen,
        }


def test_biocontext_opentargets_tool(mock_biocontext_client):
    """Test BioContext OpenTargets tool invocation."""
    tool = BioContextOpenTargetsTool()
    res = tool.invoke({"query": "TNF", "size": 3})
    data = json.loads(res)
    assert data["source"] == "OpenTargets (BioContext)"
    assert data["results"][0]["name"] == "TNF"


def test_biocontext_kegg_tool(mock_biocontext_client):
    """Test BioContext KEGG tool invocation."""
    tool = BioContextKEGGTool()
    res = tool.invoke({"argument": "MAPK", "operation": "find"})
    data = json.loads(res)
    assert data["source"] == "KEGG (BioContext)"
    assert "MAPK" in data["results"][0]["description"]


def test_biocontext_europepmc_tool(mock_biocontext_client):
    """Test BioContext EuropePMC tool invocation."""
    tool = BioContextEuropePMCTool()
    res = tool.invoke({"query": "Crohn's disease", "limit": 2})
    data = json.loads(res)
    assert data["source"] == "EuropePMC (BioContext)"
    assert len(data["articles"]) == 1


def test_biocontext_uniprot_tool(mock_biocontext_client):
    """Test BioContext UniProt tool invocation."""
    tool = BioContextUniProtTool()
    res = tool.invoke({"query": "TNF", "limit": 1})
    data = json.loads(res)
    assert data["source"] == "UniProt (BioContext)"
    assert data["proteins"][0]["accession"] == "P01375"


def test_biocontext_ensembl_tool(mock_biocontext_client):
    """Test BioContext Ensembl tool invocation."""
    tool = BioContextEnsemblTool()
    res = tool.invoke({"gene_symbol": "TNF", "species": "human"})
    data = json.loads(res)
    assert data["source"] == "Ensembl (BioContext)"
    assert data["id"] == "ENSG00000232810"


def test_biocontext_general_tool(mock_biocontext_client):
    """Test BioContext universal tool invocation."""
    tool = BioContextTool()
    res = tool.invoke({"resource": "opentargets", "query": "TNF"})
    data = json.loads(res)
    assert "OpenTargets" in data["source"]


def test_biocontext_client_real_methods():
    """Test BioContext client initialization and timeout defaults."""
    client = BioContextMCPClient(endpoint_url="https://test.mcp/mcp/")
    assert client.endpoint_url == "https://test.mcp/mcp/"
    assert "Phronesis" in client._sync_session.headers["User-Agent"]
