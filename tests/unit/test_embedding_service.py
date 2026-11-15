"""TDD — bug①：嵌入模型立即下載＋進度＋切換防呆（embedding_service）。

下載＝安全新增（per-model 快取、絕不刪既有、不 re-embed）；切換＝偵測既有記憶時警告/封鎖。
全程 mock huggingface_hub 與記憶系統，不下載真模型、不碰真 data/。
"""
from __future__ import annotations

import pytest
from dotenv import get_key
from src.services import embedding_service as es

_NET_ERR = RuntimeError("network down")  # 模組級：raise 變數避免 TRY003（字面訊息）


@pytest.fixture(autouse=True)
def _clean_state():
    es.reset_download_state()
    yield
    es.reset_download_state()


# --- repo id 解析 ---

def test_resolve_repo_id_prepends_sentence_transformers():
    assert es.resolve_repo_id("all-MiniLM-L6-v2") == "sentence-transformers/all-MiniLM-L6-v2"


def test_resolve_repo_id_keeps_namespaced():
    assert es.resolve_repo_id("intfloat/multilingual-e5-base") == "intfloat/multilingual-e5-base"


# --- 防呆①：已在快取 → 不重下；下載中 → 不重複啟動 ---

def test_begin_download_already_cached_no_spawn(monkeypatch):
    monkeypatch.setattr(es, "is_cached", lambda m: True)
    state, should_spawn = es.begin_download("all-MiniLM-L6-v2")
    assert should_spawn is False
    assert state["status"] == "done"
    assert state.get("cached") is True


def test_begin_download_fresh_spawns(monkeypatch):
    monkeypatch.setattr(es, "is_cached", lambda m: False)
    state, should_spawn = es.begin_download("all-MiniLM-L6-v2")
    assert should_spawn is True
    assert state["status"] == "downloading"


def test_begin_download_duplicate_no_respawn(monkeypatch):
    monkeypatch.setattr(es, "is_cached", lambda m: False)
    es.begin_download("all-MiniLM-L6-v2")
    _state, should_spawn = es.begin_download("all-MiniLM-L6-v2")
    assert should_spawn is False  # 防呆：下載中不重複啟動


# --- perform_download：進度 + additive（絕不 force_download，不覆蓋既有）---

def test_perform_download_reports_progress_additive(monkeypatch):
    monkeypatch.setattr(es, "list_model_files",
                        lambda repo: [("config.json", 100), ("model.safetensors", 300)])
    calls = []

    def fake_dl(**kw):
        calls.append(kw)
        return f"/cache/{kw['filename']}"

    monkeypatch.setattr(es, "hf_hub_download", fake_dl)
    es.perform_download("all-MiniLM-L6-v2")
    st = es.get_download_state("all-MiniLM-L6-v2")
    assert st["status"] == "done"
    assert st["pct"] == 100
    assert len(calls) == 2
    # 防呆②：絕不 force_download（預設 skip 既有檔、不覆蓋）
    assert all(not kw.get("force_download", False) for kw in calls)


# --- 下載只抓 sentence-transformers 用得到的檔（ADR-0314）---

# intfloat/multilingual-e5-base 的檔案清單（節錄，大小照 Hugging Face 上的實際值）。
_E5_FILES = [
    (".gitattributes", 1528), ("1_Pooling/config.json", 200), ("README.md", 179345),
    ("config.json", 694), ("model.safetensors", 1112201288), ("modules.json", 387),
    ("onnx/config.json", 686), ("onnx/model.onnx", 1110059084),
    ("onnx/sentencepiece.bpe.model", 5069051), ("onnx/tokenizer.json", 17082660),
    ("openvino/openvino_model.bin", 1109816496), ("openvino/openvino_model.xml", 367736),
    ("pytorch_model.bin", 1112242989), ("sentence_bert_config.json", 57),
    ("sentencepiece.bpe.model", 5069051), ("special_tokens_map.json", 280),
    ("tokenizer.json", 17082660), ("tokenizer_config.json", 418),
]
_E5_KEPT = {
    ".gitattributes", "1_Pooling/config.json", "README.md", "config.json", "model.safetensors",
    "modules.json", "sentence_bert_config.json", "sentencepiece.bpe.model",
    "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json",
}


def _names(files):
    return {name for name, _ in files}


def test_select_download_files_skips_other_runtime_formats():
    assert _names(es.select_download_files(_E5_FILES)) == _E5_KEPT


def test_select_download_files_e5_is_about_one_point_one_gb():
    total = sum(size for _, size in es.select_download_files(_E5_FILES))
    assert 1.1e9 < total < 1.2e9  # 全部約 5.3GB


def test_select_download_files_keeps_tokenizer_files_and_drops_other_frameworks():
    files = [
        ("config.json", 612), ("model.safetensors", 90868376), ("pytorch_model.bin", 90888945),
        ("rust_model.ot", 90887379), ("tf_model.h5", 91005696), ("flax_model.msgpack", 90000000),
        ("onnx/model.onnx", 90405214), ("tokenizer.json", 466247), ("vocab.txt", 231508),
    ]
    assert _names(es.select_download_files(files)) == {
        "config.json", "model.safetensors", "tokenizer.json", "vocab.txt"}


def test_select_download_files_keeps_pytorch_weights_without_safetensors():
    files = [("config.json", 1), ("pytorch_model.bin", 10), ("vocab.txt", 1)]
    assert es.select_download_files(files) == files


def test_select_download_files_drops_sharded_pytorch_weights_when_safetensors_exist():
    files = [
        ("model-00001-of-00002.safetensors", 5), ("model-00002-of-00002.safetensors", 5),
        ("model.safetensors.index.json", 1), ("pytorch_model-00001-of-00002.bin", 5),
        ("pytorch_model-00002-of-00002.bin", 5), ("pytorch_model.bin.index.json", 1),
    ]
    assert _names(es.select_download_files(files)) == {
        "model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors",
        "model.safetensors.index.json"}


def test_select_download_files_keeps_weights_inside_module_folders():
    # 2_Dense 這類模組資料夾的權重由 sentence-transformers 直接載入，不能排除。
    files = [("model.safetensors", 5), ("2_Dense/pytorch_model.bin", 2), ("2_Dense/config.json", 1)]
    assert es.select_download_files(files) == files


def test_perform_download_fetches_only_selected_files(monkeypatch):
    monkeypatch.setattr(es, "list_model_files", lambda repo: list(_E5_FILES))
    fetched = []

    def fake_dl(**kw):
        fetched.append(kw["filename"])
        return f"/cache/{kw['filename']}"

    monkeypatch.setattr(es, "hf_hub_download", fake_dl)
    es.perform_download("intfloat/multilingual-e5-base")
    st = es.get_download_state("intfloat/multilingual-e5-base")
    assert set(fetched) == _E5_KEPT
    assert st["status"] == "done"
    assert st["total"] == sum(size for name, size in _E5_FILES if name in _E5_KEPT)


def test_perform_download_reports_bytes_inside_a_large_file(monkeypatch):
    # e5 幾乎只剩一個 1.1GB 的檔：只按檔數算會停在 0%，要按位元組回報（ADR-0314）
    monkeypatch.setattr(es, "list_model_files", lambda repo: list(_E5_FILES))
    sizes = dict(_E5_FILES)
    seen = {}

    def fake_dl(**kw):
        size = sizes[kw["filename"]]
        with kw["tqdm_class"](total=size, initial=0, desc=kw["filename"]) as bar:
            bar.update(size // 2)
            if kw["filename"] == "model.safetensors":
                seen["mid"] = es.get_download_state("intfloat/multilingual-e5-base")["pct"]
            bar.update(size - size // 2)
        return f"/cache/{kw['filename']}"

    monkeypatch.setattr(es, "hf_hub_download", fake_dl)
    es.perform_download("intfloat/multilingual-e5-base")
    st = es.get_download_state("intfloat/multilingual-e5-base")
    assert 40 <= seen["mid"] <= 60
    assert st["status"] == "done"
    assert st["downloaded"] == st["total"]


def test_perform_download_counts_initial_bytes(monkeypatch):
    # huggingface_hub 1.21 一律傳 initial=0；之後的版本若帶已有的位元組也照算
    monkeypatch.setattr(es, "list_model_files", lambda repo: [("model.safetensors", 100)])
    seen = {}

    def fake_dl(**kw):
        with kw["tqdm_class"](total=100, initial=60, desc=kw["filename"]) as bar:
            seen["initial"] = es.get_download_state("m")["downloaded"]
            bar.update(40)
        return "/cache/model.safetensors"

    monkeypatch.setattr(es, "hf_hub_download", fake_dl)
    es.perform_download("m")
    assert seen["initial"] == 60
    assert es.get_download_state("m")["pct"] == 100


def test_perform_download_handles_rejected_resume(monkeypatch):
    # 伺服器不接受續傳時 http_get 會 update(負數) 扣回，再從頭傳
    monkeypatch.setattr(es, "list_model_files", lambda repo: [("model.safetensors", 100)])
    seen = {}

    def fake_dl(**kw):
        with kw["tqdm_class"](total=100, initial=0, desc=kw["filename"]) as bar:
            bar.update(60)
            bar.update(-60)
            seen["after_reset"] = es.get_download_state("m")["downloaded"]
            bar.update(100)
        return "/cache/model.safetensors"

    monkeypatch.setattr(es, "hf_hub_download", fake_dl)
    es.perform_download("m")
    assert seen["after_reset"] == 0
    assert es.get_download_state("m")["downloaded"] == 100


@pytest.mark.parametrize("fails", [False, True], ids=["done", "error"])
def test_late_progress_after_finish_keeps_the_result(monkeypatch, fails):
    # Xet 的進度回呼由背景計時呼叫，可能在下載結束後才到；不能把結果蓋回「下載中」
    monkeypatch.setattr(es, "list_model_files",
                        lambda repo: [("model.safetensors", 100), ("tokenizer.json", 10)])
    bars = []

    def fake_dl(**kw):
        bar = kw["tqdm_class"](total=100, initial=0, desc=kw["filename"])
        bars.append(bar)
        if fails and kw["filename"] == "tokenizer.json":
            raise _NET_ERR
        with bar:
            bar.update(1)
        return "/cache/x"

    monkeypatch.setattr(es, "hf_hub_download", fake_dl)
    es.perform_download("m")
    bars[0].update(5)  # 晚到的回呼
    assert es.get_download_state("m")["status"] == ("error" if fails else "done")


def test_begin_download_after_failure_ignores_partial_cache(monkeypatch):
    # 抓到一半失敗時快取資料夾已經建立，is_cached 會說已下載；重試要真的再下載
    def boom(repo):
        raise _NET_ERR

    monkeypatch.setattr(es, "list_model_files", boom)
    es.perform_download("intfloat/multilingual-e5-base")
    monkeypatch.setattr(es, "is_cached", lambda m: True)
    state, should_spawn = es.begin_download("intfloat/multilingual-e5-base")
    assert should_spawn is True
    assert state["status"] == "downloading"


def test_perform_download_error_sets_error_state(monkeypatch):
    monkeypatch.setattr(es, "list_model_files", lambda repo: [("config.json", 100)])

    def boom(**kw):
        raise _NET_ERR

    monkeypatch.setattr(es, "hf_hub_download", boom)
    es.perform_download("all-MiniLM-L6-v2")
    st = es.get_download_state("all-MiniLM-L6-v2")
    assert st["status"] == "error"
    assert "network" in st.get("error", "")


# --- 切換防呆③：既有記憶 + 換模型 → 需確認 ---

def test_evaluate_switch_same_model_no_confirmation(monkeypatch):
    monkeypatch.setattr(es, "get_current_embedding_model", lambda: "all-MiniLM-L6-v2")
    monkeypatch.setattr(es, "has_existing_memories", lambda: True)
    ev = es.evaluate_switch("all-MiniLM-L6-v2")
    assert ev["requires_confirmation"] is False


def test_evaluate_switch_diff_model_with_memories_requires_confirmation(monkeypatch):
    monkeypatch.setattr(es, "get_current_embedding_model", lambda: "all-MiniLM-L6-v2")
    monkeypatch.setattr(es, "has_existing_memories", lambda: True)
    ev = es.evaluate_switch("paraphrase-multilingual-MiniLM-L12-v2")
    assert ev["requires_confirmation"] is True
    assert ev["has_memories"] is True


def test_evaluate_switch_diff_model_no_memories_ok(monkeypatch):
    monkeypatch.setattr(es, "get_current_embedding_model", lambda: "all-MiniLM-L6-v2")
    monkeypatch.setattr(es, "has_existing_memories", lambda: False)
    ev = es.evaluate_switch("paraphrase-multilingual-MiniLM-L12-v2")
    assert ev["requires_confirmation"] is False


# --- apply_switch：寫 EMBEDDING_MODEL（SSOT resolve）＋重置單例（免重啟）---

def test_apply_switch_writes_env_and_resets(monkeypatch, tmp_path):
    # set_key 於 .env 不存在時自動建立；以 dotenv get_key 驗證（避免 Path IO 觸發 IO_PRISON）。
    env = tmp_path / ".env"
    monkeypatch.setenv("TOUKA_ENV_FILE", str(env))
    applied: dict = {}
    monkeypatch.setattr(es, "apply_runtime_env", applied.update)
    reset_called = {"n": 0}
    monkeypatch.setattr(es, "_reset_memory_singletons",
                        lambda: reset_called.__setitem__("n", reset_called["n"] + 1))
    es.apply_switch("paraphrase-multilingual-MiniLM-L12-v2")
    assert get_key(str(env), "EMBEDDING_MODEL") == "paraphrase-multilingual-MiniLM-L12-v2"
    assert applied.get("EMBEDDING_MODEL") == "paraphrase-multilingual-MiniLM-L12-v2"
    assert reset_called["n"] == 1  # 重置向量單例 → 新模型免重啟生效
