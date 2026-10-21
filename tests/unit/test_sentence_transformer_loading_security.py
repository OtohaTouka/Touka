"""CVE-2026-68770: local custom modules need explicit trust, with offline compatibility."""

from __future__ import annotations

import json
import socket
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import numpy as np
import pytest
import torch
from huggingface_hub import constants as hub_constants
from memory_server import vector
from sentence_transformers import SentenceTransformer, models
from transformers import BertConfig, BertModel, BertTokenizer, dynamic_module_utils

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def owned_offline_model_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep even the vulnerable loader's module writes inside the owned test directory."""
    cache = tmp_path / "hf-cache"
    modules_cache = cache / "modules"
    monkeypatch.setenv("HF_HOME", str(cache))
    monkeypatch.setenv("HF_HUB_CACHE", str(cache / "hub"))
    monkeypatch.setenv("HF_MODULES_CACHE", str(modules_cache))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setattr(hub_constants, "HF_HUB_CACHE", str(cache / "hub"))
    monkeypatch.setattr(dynamic_module_utils, "HF_MODULES_CACHE", str(modules_cache))
    monkeypatch.setattr(sys, "path", list(sys.path))
    before_modules = set(sys.modules)

    def refuse_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("The local model regression must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    try:
        yield
    finally:
        for name in set(sys.modules) - before_modules:
            if name == "transformers_modules" or name.startswith("transformers_modules."):
                sys.modules.pop(name, None)


@pytest.fixture
def local_custom_module(tmp_path: Path) -> tuple[Path, Path]:
    """Importing this harmless local fixture creates one owned marker and stops immediately."""
    model = tmp_path / ("custom_" + tmp_path.name.replace("-", "_"))
    model.mkdir()
    marker = tmp_path / "custom-module-imported.txt"
    (model / "modules.json").write_text(
        json.dumps(
            [{"idx": 0, "name": "0", "path": "", "type": "touka_security_probe.GuardProbe"}]
        ),
        encoding="utf-8",
    )
    (model / "config_sentence_transformers.json").write_text("{}", encoding="utf-8")
    (model / "README.md").write_text("Owned synthetic security fixture.\n", encoding="utf-8")
    (model / "touka_security_probe.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
        "raise RuntimeError('SYNTHETIC_CUSTOM_MODULE_IMPORTED')\n",
        encoding="utf-8",
    )
    return model, marker


@pytest.mark.parametrize("trust", ["omitted", False, None], ids=["default", "false", "none"])
def test_cve_2026_68770_local_custom_module_is_denied_before_import(
    local_custom_module: tuple[Path, Path], tmp_path: Path, trust: str | bool | None
) -> None:
    """Exercise the real dependency loader, not a mocked security decision."""
    model, marker = local_custom_module
    kwargs: dict[str, Any] = {
        "device": "cpu",
        "local_files_only": True,
        "cache_folder": str(tmp_path / "model-cache"),
    }
    if trust != "omitted":
        kwargs["trust_remote_code"] = trust
    try:
        with pytest.raises(ValueError, match="trust_remote_code"):
            SentenceTransformer(str(model), **kwargs)
    finally:
        assert not marker.exists(), "Untrusted local module code ran before refusal"


def _sandbox_store(monkeypatch: pytest.MonkeyPatch, root: Path, model: Path) -> vector.VectorStore:
    monkeypatch.setattr(vector, "get_resource_proxy", lambda: MagicMock())
    monkeypatch.setattr(vector, "QdrantClient", MagicMock())
    monkeypatch.setattr(vector.VectorStore, "_client_cache", {})
    return vector.VectorStore(base_dir=root, model_name=str(model))


def test_vector_store_rejects_custom_module_without_initializing_model(
    local_custom_module: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only storage is mocked; the production adapter calls the actual SentenceTransformer."""
    model, marker = local_custom_module
    store = _sandbox_store(monkeypatch, tmp_path / "store", model)
    try:
        with pytest.raises(ValueError, match="trust_remote_code"):
            store.get_embedding("alpha beta")
    finally:
        assert not marker.exists(), "VectorStore executed untrusted local model code"
        assert store._model is None
        assert store._embedding_cache == {}


@pytest.fixture
def tiny_local_transformer(tmp_path: Path) -> Path:
    """Build a tiny standard BERT locally; there are no pretrained downloads or Owner caches."""
    backbone = tmp_path / "backbone"
    backbone.mkdir()
    vocab = {
        token: index
        for index, token in enumerate(
            ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]", "alpha", "beta", "gamma"]
        )
    }
    tokenizer = BertTokenizer(vocab=vocab, model_max_length=32)
    tokenizer.save_pretrained(backbone)
    config = BertConfig(
        vocab_size=len(vocab),
        hidden_size=8,
        num_hidden_layers=1,
        num_attention_heads=2,
        intermediate_size=16,
        max_position_embeddings=32,
        hidden_dropout_prob=0.0,
        attention_probs_dropout_prob=0.0,
    )
    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(23)
        BertModel(config).save_pretrained(backbone)
    transformer = models.Transformer(
        str(backbone),
        max_seq_length=16,
        model_args={"local_files_only": True, "trust_remote_code": False},
        tokenizer_args={"local_files_only": True, "trust_remote_code": False},
        config_args={"local_files_only": True, "trust_remote_code": False},
    )
    model = SentenceTransformer(
        modules=[transformer, models.Pooling(8), models.Normalize()],
        device="cpu",
        local_files_only=True,
        trust_remote_code=False,
    )
    destination = tmp_path / "sentence-transformer"
    model.save(str(destination), create_model_card=False)
    return destination


def test_standard_local_transformer_reload_preserves_string_float_embeddings(
    tiny_local_transformer: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cover the tracked str/list[str] float route, not chat, quantization or multiprocess APIs."""
    model = SentenceTransformer(
        str(tiny_local_transformer),
        device="cpu",
        local_files_only=True,
        trust_remote_code=False,
        cache_folder=str(tmp_path / "reload-cache"),
    )
    texts = ["alpha beta", "gamma", "beta alpha"]
    batch = model.encode(texts, batch_size=2)
    singles = np.stack([model.encode(text) for text in texts])
    assert isinstance(batch, np.ndarray) and batch.shape == (3, 8)
    assert batch.dtype == np.float32 and np.isfinite(batch).all()
    assert model.device.type == "cpu"
    np.testing.assert_allclose(batch, singles, rtol=1e-5, atol=1e-6)
    np.testing.assert_allclose(np.linalg.norm(batch, axis=1), np.ones(3), atol=1e-5)
    store = _sandbox_store(monkeypatch, tmp_path / "store", tiny_local_transformer)
    first = store.get_embedding(texts[0])
    assert isinstance(first, list) and all(isinstance(value, float) for value in first)
    assert len(first) == 8 and np.isfinite(first).all()
    np.testing.assert_allclose(first, singles[0], rtol=1e-5, atol=1e-6)
    assert store.get_embedding(texts[0]) is first
