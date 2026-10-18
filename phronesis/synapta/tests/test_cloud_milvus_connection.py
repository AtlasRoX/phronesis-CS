"""
Unit tests for Cloud Milvus (Zilliz Cloud) and Milvus Lite support in MilvusConnectionManager.
Verifies zero-docker connectivity and configuration.
"""

from types import SimpleNamespace
from unittest.mock import patch, MagicMock
import pytest

from ..utils.database.milvus_connection_manager import MilvusConnectionManager


@pytest.fixture(autouse=True)
def clean_instances():
    """Clear singleton instances before each test."""
    MilvusConnectionManager.clear_instances()
    yield
    MilvusConnectionManager.clear_instances()


def test_cloud_milvus_uri_initialization(monkeypatch):
    """Test initializing MilvusConnectionManager with Cloud Milvus (Zilliz Cloud) URI and token."""
    monkeypatch.setenv("MILVUS_URI", "https://in03-xyz.serverless.gcp-us-west1.cloud.zilliz.com")
    monkeypatch.setenv("MILVUS_TOKEN", "db_admin:secret_token")

    cfg = SimpleNamespace(
        milvus_db=SimpleNamespace(
            alias="cloud_test",
            host="localhost",
            port=19530,
            user="root",
            password="pwd",
            database_name="cloud_db",
        )
    )

    mgr = MilvusConnectionManager(cfg)
    assert mgr.uri == "https://in03-xyz.serverless.gcp-us-west1.cloud.zilliz.com"
    assert mgr.token == "db_admin:secret_token"

    # Test get_connection_info returns cloud URI
    with patch("phronesis.synapta.utils.database.milvus_connection_manager.connections") as mock_conn:
        mock_conn.has_connection.return_value = True
        mock_conn.get_connection_addr.return_value = "https://in03-xyz.serverless.gcp-us-west1.cloud.zilliz.com"
        info = mgr.get_connection_info()
        assert info["uri"] == "https://in03-xyz.serverless.gcp-us-west1.cloud.zilliz.com"
        assert info["connected"] is True


def test_milvus_lite_embedded_uri(monkeypatch):
    """Test initializing MilvusConnectionManager with local embedded Milvus Lite file."""
    monkeypatch.setenv("MILVUS_URI", "./milvus_test.db")
    monkeypatch.delenv("MILVUS_TOKEN", raising=False)

    cfg = SimpleNamespace(
        milvus_db=SimpleNamespace(
            alias="lite_test",
            host="localhost",
            port=19530,
            user="",
            password="",
            database_name="default",
        )
    )

    mgr = MilvusConnectionManager(cfg)
    assert mgr.uri == "./milvus_test.db"

    with patch("phronesis.synapta.utils.database.milvus_connection_manager.connections") as mock_conn:
        mock_conn.has_connection.return_value = False
        with patch("phronesis.synapta.utils.database.milvus_connection_manager.db") as mock_db:
            res = mgr.ensure_connection()
            assert res is True
            mock_conn.connect.assert_called_once_with(alias="lite_test", uri="./milvus_test.db")


def test_cloud_milvus_sync_client_creation(monkeypatch):
    """Test that get_sync_client passes cloud URI and token to MilvusClient."""
    monkeypatch.setenv("MILVUS_URI", "https://in03-xyz.serverless.gcp-us-west1.cloud.zilliz.com")
    monkeypatch.setenv("MILVUS_TOKEN", "test_api_key")

    cfg = SimpleNamespace(
        milvus_db=SimpleNamespace(
            alias="client_test",
            host="localhost",
            port=19530,
            user="root",
            password="pwd",
            database_name="t2kg_cloud",
        )
    )

    mgr = MilvusConnectionManager(cfg)
    with patch("phronesis.synapta.utils.database.milvus_connection_manager.MilvusClient") as mock_client_cls:
        client = mgr.get_sync_client()
        mock_client_cls.assert_called_once_with(
            uri="https://in03-xyz.serverless.gcp-us-west1.cloud.zilliz.com",
            token="test_api_key",
            db_name="t2kg_cloud",
        )
        assert client is not None
