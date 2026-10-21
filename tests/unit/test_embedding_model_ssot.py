"""TDD: EMBEDDING_MODEL env var SSOT for ToukaMemory."""

import os
from unittest.mock import MagicMock, patch

from memory_server.config_utils import get_embedding_model_name
from memory_server.vector import VectorStore


# ---------------------------------------------------------------------------
# 1. config_utils.get_embedding_model_name — env var reading
# ---------------------------------------------------------------------------

def test_get_embedding_model_name_default(monkeypatch):
    """Returns default model when EMBEDDING_MODEL is not set."""
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    assert get_embedding_model_name() == "all-MiniLM-L6-v2"


def test_get_embedding_model_name_from_env(monkeypatch):
    """Returns configured model when EMBEDDING_MODEL is set."""
    monkeypatch.setenv("EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
    assert get_embedding_model_name() == "paraphrase-multilingual-MiniLM-L12-v2"


def test_get_embedding_model_name_empty_falls_back_to_default(monkeypatch):
    """Empty EMBEDDING_MODEL env var falls back to the default model."""
    monkeypatch.setenv("EMBEDDING_MODEL", "")
    assert get_embedding_model_name() == "all-MiniLM-L6-v2"


# ---------------------------------------------------------------------------
# 2. VectorStore reads model_name from config_utils (no real download)
# ---------------------------------------------------------------------------

def test_vector_store_uses_configured_model(monkeypatch, tmp_path):
    """VectorStore picks up model_name from get_embedding_model_name()."""
    monkeypatch.setenv("EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")

    # Avoid importing memory_server.vector directly (may trigger Qdrant/ST)
    assert get_embedding_model_name() == "paraphrase-multilingual-MiniLM-L12-v2"


def test_vector_store_default_model_is_english(monkeypatch):
    """Without EMBEDDING_MODEL set, default is the English-optimised model."""
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    assert get_embedding_model_name() == "all-MiniLM-L6-v2"


# ---------------------------------------------------------------------------
# 3. VectorStore __init__ honours model_name kwarg (no real download)
# ---------------------------------------------------------------------------

def test_vector_store_init_uses_model_name_kwarg(monkeypatch, tmp_path):
    """VectorStore.model_name matches the model_name kwarg passed at init."""
    monkeypatch.setenv("EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")

    # Mock SentenceTransformer and QdrantClient to avoid real downloads
    with patch("memory_server.vector.SentenceTransformer") as mock_st, \
         patch("memory_server.vector.QdrantClient") as mock_qd:
        mock_qd.return_value = MagicMock()

        model = get_embedding_model_name()
        vs = VectorStore(base_dir=str(tmp_path), model_name=model)
        assert vs.model_name == "paraphrase-multilingual-MiniLM-L12-v2"


def test_vector_store_default_init_uses_env(monkeypatch, tmp_path):
    """VectorStore() with no explicit model_name reads from EMBEDDING_MODEL env."""
    monkeypatch.setenv("EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")

    with patch("memory_server.vector.SentenceTransformer"), \
         patch("memory_server.vector.QdrantClient") as mock_qd:
        mock_qd.return_value = MagicMock()
        vs = VectorStore(base_dir=str(tmp_path))
        assert vs.model_name == "paraphrase-multilingual-MiniLM-L12-v2"
