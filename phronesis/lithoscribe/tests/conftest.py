"""
Shared fixtures for Lithoscribe unit tests.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def placeholder_openai_key(monkeypatch):
    """
    Unit tests construct OpenAI clients but mock every call; supply a placeholder key so they run
    without secrets. A real OPENAI_API_KEY, when present, is left untouched.
    """
    if not os.getenv("OPENAI_API_KEY"):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-placeholder")
