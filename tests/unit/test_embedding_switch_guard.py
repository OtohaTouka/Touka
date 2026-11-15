"""換模型的確認與側欄提示（ADR-0315）.

一條規則三處共用：精靈切換、`POST /config/env`、`POST /admin/setup/apply`。設定介面只在這次請求
真的改了設定檔裡的模型時才套用。全在沙盒：tmp 的 .env（``TOUKA_ENV_FILE``）、評估用替身，
不載入嵌入模型、不碰真的設定檔與 data/。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from memory_server import factual as fm
from memory_server.utils import embedding_index as ei
from memory_server.utils.embedding_index import TargetEvaluation, TargetOutcome
from src import main as main_module
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import config as config_router
from src.routers import observability
from src.routers import setup as setup_router
from src.services import embedding_service as es
from src.services.identity_router import get_verified_identity
from src.services.setup_service import SetupService

if TYPE_CHECKING:
    from pathlib import Path

E5 = "intfloat/multilingual-e5-base"
MINILM = "all-MiniLM-L6-v2"
PARAPHRASE = "paraphrase-multilingual-MiniLM-L12-v2"
ELSEWHERE = "example-org/some-other-model"  # 解析測試的寫入前模型，和上面都不同
WIZARD_VALUE = "fresh-value-for-test"  # 精靈一起送的另一個欄位
CALLER = AgentIdentity(agent_id="GuardAdmin")  # 角色由路由的 Depends 把關，這裡直接呼叫


@pytest.fixture(autouse=True)
def _fresh_index_state() -> None:
    ei.reset_index_status()


@pytest.fixture
def effective(monkeypatch: pytest.MonkeyPatch):
    def set_model(model: str) -> None:
        monkeypatch.setattr(es, "get_current_embedding_model", lambda: model)

    set_model(MINILM)
    return set_model


@pytest.fixture
def evaluated(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the live index evaluation; record which targets were evaluated."""
    state: dict[str, Any] = {
        "calls": [],
        "result": TargetEvaluation(TargetOutcome.MISMATCH, E5, 768),
    }

    def fake(target: str) -> TargetEvaluation:
        state["calls"].append(target)
        return state["result"]

    monkeypatch.setattr(es, "evaluate_index", fake)
    return state


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".env"
    path.write_text(f"EMBEDDING_MODEL={E5}\nKEPT=1\n", encoding="utf-8")
    monkeypatch.setenv("TOUKA_ENV_FILE", str(path))
    return path


# --- the rule (evaluate_switch) ---------------------------------------------------


def test_target_equal_to_the_model_in_use_needs_no_confirmation(effective, evaluated) -> None:
    effective(MINILM)

    result = es.evaluate_switch(" sentence-transformers/ALL-MiniLM-L6-v2 ")

    assert result["requires_confirmation"] is False


@pytest.mark.parametrize(
    ("outcome", "needs"),
    [
        (TargetOutcome.NO_MEMORIES, False),
        (TargetOutcome.NO_INDEX, False),
        (TargetOutcome.MATCH, False),
        (TargetOutcome.MISMATCH, True),
        (TargetOutcome.UNKNOWN, True),
        (TargetOutcome.UNREADABLE, True),
    ],
)
def test_switch_needs_confirmation_unless_live_evidence_says_safe(
    effective, evaluated, outcome: TargetOutcome, needs: bool
) -> None:
    evaluated["result"] = TargetEvaluation(outcome, E5, 768)

    result = es.evaluate_switch(PARAPHRASE)

    assert result["requires_confirmation"] is needs
    assert evaluated["calls"] == [PARAPHRASE]
    assert result["has_memories"] is (outcome is not TargetOutcome.NO_MEMORIES)


def test_evaluation_reports_the_model_the_memories_use(effective, evaluated) -> None:
    effective(E5)
    evaluated["result"] = TargetEvaluation(TargetOutcome.UNKNOWN, None, 384)

    result = es.evaluate_switch(MINILM)

    assert result["current_model"] == E5
    assert (result["index_model"], result["index_dim"], result["index_outcome"]) == (
        None,
        384,
        "unknown",
    )


def test_evaluate_index_without_the_memory_system_means_no_memories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> Any:
        raise ModuleNotFoundError

    monkeypatch.setattr(es.importlib, "import_module", missing)

    assert es.evaluate_index(E5).outcome is TargetOutcome.NO_MEMORIES


def test_evaluate_index_with_an_unopenable_store_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Manager:
        def get_factual_store(self) -> Any:
            raise fm.FactsSchemaNotReadyError(fm.FactsSchemaStatus.UNREADABLE)

    class _Module:
        MemoryManager = _Manager

    monkeypatch.setattr(es.importlib, "import_module", lambda _name: _Module)

    assert es.evaluate_index(E5).outcome is TargetOutcome.UNREADABLE


def test_evaluate_index_uses_the_live_evaluation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = fm.FactualStore(base_dir=tmp_path / "vault")
    monkeypatch.setattr(
        "memory_server.services.manager.MemoryManager._factual_store_singleton", store
    )
    seen: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        es, "evaluate_target", lambda model, factual: seen.append((model, factual)) or "x"
    )

    assert es.evaluate_index(E5) == "x"
    assert seen == [(E5, store)]


@pytest.mark.parametrize(("force", "needs", "applied"), [
    (False, True, False), (True, True, True), (False, False, True),
])
def test_switch_marks_the_sidebar_check_stale_only_when_applied(
    monkeypatch: pytest.MonkeyPatch, force: bool, needs: bool, applied: bool
) -> None:
    monkeypatch.setattr(
        es, "evaluate_switch", lambda target: {"requires_confirmation": needs}
    )
    events: list[str] = []
    monkeypatch.setattr(es, "apply_switch", lambda target: events.append("apply"))
    monkeypatch.setattr(es, "mark_index_status_stale", lambda: events.append("stale"))

    es.switch_model(PARAPHRASE, force=force)

    assert events == (["apply", "stale"] if applied else [])


# --- reading the model out of a settings file --------------------------------------


@pytest.mark.parametrize(
    ("text", "model"),
    [
        ("KEPT=1\n", MINILM),
        ("EMBEDDING_MODEL=\n", MINILM),
        ("EMBEDDING_MODEL=   \n", MINILM),
        (f"EMBEDDING_MODEL={E5}\n", E5),
        (f" EMBEDDING_MODEL = {E5}\n", E5),
        (f"export EMBEDDING_MODEL={E5}\n", E5),
        (f"embedding_model={E5}\n", E5),
        (f"EMBEDDING_MODEL={MINILM}\nEMBEDDING_MODEL={E5}\n", E5),
        (f"# EMBEDDING_MODEL={E5}\n", MINILM),
    ],
)
def test_model_in_settings_text(text: str, model: str) -> None:
    # 從一個別的模型換過來，回的就是解析出的寫入後模型
    assert es.env_model_change(f"EMBEDDING_MODEL={ELSEWHERE}\n", text) == (model,)


def test_every_spelling_of_the_key_counts() -> None:
    """Linux 只認 `EMBEDDING_MODEL`、Windows 認第一個：不同寫法的值都要算進來."""
    text = f"EMBEDDING_MODEL={E5}\nembedding_model={MINILM}\nEmbedding_Model={E5.upper()}\n"

    assert es.env_model_change(f"EMBEDDING_MODEL={ELSEWHERE}\n", text) == (E5, MINILM)


def test_change_is_reported_only_when_the_model_differs() -> None:
    assert es.env_model_change(f"EMBEDDING_MODEL={E5}\n", f"EMBEDDING_MODEL={E5.upper()}\n") is None
    assert es.env_model_change("KEPT=1\n", f"EMBEDDING_MODEL={MINILM}\n") is None
    assert es.env_model_change(f"EMBEDDING_MODEL={E5}\n", "KEPT=1\n") == (MINILM,)
    assert es.env_model_change(
        f"EMBEDDING_MODEL={E5}\n", f"EMBEDDING_MODEL={PARAPHRASE}\nembedding_model={E5}\n"
    ) == (PARAPHRASE, E5)


def test_swapping_the_values_of_two_spellings_is_a_change() -> None:
    """設定檔原本就分歧時互換兩個寫法的值：模型組合一樣，實際載入的卻換了（DEV-B 複核）."""
    before = f"EMBEDDING_MODEL={E5}\nembedding_model={PARAPHRASE}\n"
    swapped = f"EMBEDDING_MODEL={PARAPHRASE}\nembedding_model={E5}\n"

    assert es.env_model_change(before, swapped) == (PARAPHRASE, E5)


def test_a_swap_in_an_already_split_file_is_refused(env_file: Path, effective, evaluated) -> None:
    env_file.write_text(f"EMBEDDING_MODEL={E5}\nembedding_model={PARAPHRASE}\n", encoding="utf-8")
    effective(E5)
    evaluated["result"] = TargetEvaluation(TargetOutcome.NO_MEMORIES)
    before = env_file.read_bytes()

    with pytest.raises(ToukaException):
        _save({"EMBEDDING_MODEL": PARAPHRASE, "embedding_model": E5})

    assert env_file.read_bytes() == before


def test_settings_file_uses_a_model_only_when_every_spelling_agrees(tmp_path: Path) -> None:
    agreeing, split = tmp_path / "agreeing.env", tmp_path / "split.env"
    agreeing.write_text(f"EMBEDDING_MODEL={E5}\nembedding_model={E5.upper()}\n", encoding="utf-8")
    split.write_text(f"EMBEDDING_MODEL={E5}\nembedding_model={PARAPHRASE}\n", encoding="utf-8")

    assert es.env_file_uses_model(agreeing, E5) is True
    assert es.env_file_uses_model(split, E5) is False
    assert es.env_file_uses_model(tmp_path / "missing.env", MINILM) is True


# --- POST /config/env ------------------------------------------------------------


def _save(settings: dict[str, str]) -> dict[str, Any]:
    return asyncio.run(config_router.update_env(config_router.EnvUpdate(settings=settings), CALLER))


def test_saving_other_keys_never_consults_the_index(env_file: Path, effective, evaluated) -> None:
    effective(MINILM)  # 環境變數寫死英文模型、設定檔是照範例重建的 e5

    _save({"SOME_SETTING": "on"})

    assert evaluated["calls"] == []
    assert dotenv_values(env_file)["SOME_SETTING"] == "on"


def test_switch_needing_confirmation_is_refused_and_nothing_is_written(
    env_file: Path, effective, evaluated
) -> None:
    effective(E5)
    before = env_file.read_bytes()

    with pytest.raises(ToukaException) as raised:
        _save({"EMBEDDING_MODEL": PARAPHRASE, "SOME_SETTING": "on"})

    assert raised.value.code is ErrorCode.EMBEDDING_MODEL_SWITCH_NEEDS_CONFIRMATION
    assert evaluated["calls"] == [PARAPHRASE]
    assert env_file.read_bytes() == before


def test_switch_that_is_safe_is_written(env_file: Path, effective, evaluated) -> None:
    effective(E5)
    evaluated["result"] = TargetEvaluation(TargetOutcome.NO_MEMORIES)

    _save({"EMBEDDING_MODEL": PARAPHRASE})

    assert dotenv_values(env_file)["EMBEDDING_MODEL"] == PARAPHRASE


def test_restoring_the_model_in_use_is_written(env_file: Path, effective, evaluated) -> None:
    effective(MINILM)  # 生效的是英文模型，設定檔被重建成 e5

    _save({"EMBEDDING_MODEL": MINILM})

    assert dotenv_values(env_file)["EMBEDDING_MODEL"] == MINILM


def test_same_model_in_another_spelling_skips_the_check(
    env_file: Path, effective, evaluated
) -> None:
    effective(MINILM)

    _save({"EMBEDDING_MODEL": E5.upper()})

    assert evaluated["calls"] == []


@pytest.mark.parametrize(
    "settings",
    [
        {" EMBEDDING_MODEL": PARAPHRASE},
        {"export EMBEDDING_MODEL": PARAPHRASE},
        {"embedding_model": PARAPHRASE},
        {"SOME_SETTING": f"on\nEMBEDDING_MODEL={PARAPHRASE}"},
        # 舊寫法的鍵留著原模型：解析看起來沒變，實際載入的卻是新模型（DEV-B M1）
        {"EMBEDDING_MODEL": PARAPHRASE, "embedding_model": E5},
        {"EMBEDDING_MODEL": PARAPHRASE, "SOME_SETTING": f"on\nembedding_model={E5}"},
    ],
)
def test_disguised_switches_are_caught(
    env_file: Path, effective, evaluated, settings: dict[str, str]
) -> None:
    effective(E5)
    before = env_file.read_bytes()

    with pytest.raises(ToukaException):
        _save(settings)

    assert env_file.read_bytes() == before


def test_lines_that_disagree_on_the_model_are_refused_even_without_memories(
    env_file: Path, effective, evaluated
) -> None:
    """寫完會有兩個寫法、兩個模型：載入哪一個看平台，不能證明安全就要確認."""
    effective(E5)
    evaluated["result"] = TargetEvaluation(TargetOutcome.NO_MEMORIES)
    before = env_file.read_bytes()

    with pytest.raises(ToukaException):
        _save({"EMBEDDING_MODEL": PARAPHRASE, "embedding_model": E5})

    assert env_file.read_bytes() == before


def test_a_new_key_never_sticks_to_a_last_line_without_newline(
    env_file: Path, effective, evaluated
) -> None:
    env_file.write_text(f"KEPT=1\nEMBEDDING_MODEL={E5}", encoding="utf-8")  # 最後一行沒換行

    _save({"SOME_SETTING": "on"})

    values = dotenv_values(env_file)
    assert (values["EMBEDDING_MODEL"], values["SOME_SETTING"]) == (E5, "on")
    assert evaluated["calls"] == []


def test_a_switch_appearing_while_waiting_is_refused(
    env_file: Path, effective, evaluated, monkeypatch: pytest.MonkeyPatch
) -> None:
    effective(E5)
    texts = iter([f"EMBEDDING_MODEL={E5}\n", f"EMBEDDING_MODEL={MINILM}\n"])
    monkeypatch.setattr(config_router, "_read_env_text", lambda _path: next(texts))
    before = env_file.read_bytes()

    with pytest.raises(ToukaException):
        _save({"EMBEDDING_MODEL": E5})  # 檢查時沒改到模型；寫的時候卻會把 MiniLM 換成 e5

    assert env_file.read_bytes() == before


# --- POST /admin/setup/apply ------------------------------------------------------


def _apply(env: Path, **fields: str) -> dict[str, str]:
    body = setup_router.SetupApplyRequest(**fields)
    return asyncio.run(setup_router.apply_setup(body, CALLER, SetupService(env_file_path=str(env))))


def test_setup_apply_refuses_a_switch_needing_confirmation(
    env_file: Path, effective, evaluated
) -> None:
    effective(E5)
    before = env_file.read_bytes()

    with pytest.raises(ToukaException) as raised:
        _apply(env_file, embedding_model=PARAPHRASE, auth_secret=WIZARD_VALUE)

    assert raised.value.code is ErrorCode.EMBEDDING_MODEL_SWITCH_NEEDS_CONFIRMATION
    assert env_file.read_bytes() == before


def test_setup_apply_same_model_or_no_model_skips_the_check(
    env_file: Path, effective, evaluated
) -> None:
    effective(E5)

    _apply(env_file, embedding_model=E5)
    _apply(env_file, auth_secret=WIZARD_VALUE)

    assert evaluated["calls"] == []
    assert dotenv_values(env_file)["TOUKA_AUTH_SECRET"] == WIZARD_VALUE


def test_setup_apply_safe_switch_is_written(env_file: Path, effective, evaluated) -> None:
    effective(E5)
    evaluated["result"] = TargetEvaluation(TargetOutcome.MATCH, MINILM, 384)

    _apply(env_file, embedding_model=MINILM)

    assert dotenv_values(env_file)["EMBEDDING_MODEL"] == MINILM


def test_setup_service_refuses_an_unchecked_switch_under_the_lock(env_file: Path) -> None:
    """檢查時沒改到模型、寫的時候檔案已被換掉：鎖內重讀，沒評估過的換法不寫."""
    before = env_file.read_bytes()

    with pytest.raises(ToukaException) as raised:
        SetupService(env_file_path=str(env_file)).apply(
            embedding_model=PARAPHRASE, auth_secret=WIZARD_VALUE
        )

    assert raised.value.code is ErrorCode.EMBEDDING_MODEL_SWITCH_NEEDS_CONFIRMATION
    assert env_file.read_bytes() == before


def test_setup_service_writes_a_checked_switch(env_file: Path) -> None:
    SetupService(env_file_path=str(env_file)).apply(
        embedding_model=PARAPHRASE, embedding_switch_checked=True
    )

    assert dotenv_values(env_file)["EMBEDDING_MODEL"] == PARAPHRASE


# --- the sidebar notice -----------------------------------------------------------


def _mismatched_status() -> ei.IndexStatus:
    entry = ei.IndexEntry(name="memory_Alice", dim=384, model=None)
    return ei.IndexStatus(
        configured_model=E5,
        results=(ei.CollectionResult(entry, ei.Verdict.MISMATCH, ei.Basis.LENGTH),),
    )


@pytest.fixture
def client() -> Any:
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        agent_id="DebugAgent", role="Debugger"
    )
    try:
        with TestClient(app) as built:
            ei.reset_index_status()  # 不讓啟動時算的狀態影響這裡
            yield built
    finally:
        app.dependency_overrides.clear()


def _embedding_notices(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in body["memory_notices"] if n["code"] == "embedding_model_mismatch"]


def test_memory_endpoint_shows_the_mismatch(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability, "refresh_index_status", lambda _store: _mismatched_status())

    notices = _embedding_notices(client.get("/v1/memory").json())

    assert notices == [
        {
            "code": "embedding_model_mismatch",
            "level": "warning",
            "count": 1,
            "basis": "length",
            "configured_model": E5,
            "index_model": None,
            "index_dim": 384,
        }
    ]


def test_memory_endpoint_without_a_mismatch_shows_nothing(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observability, "refresh_index_status", lambda _store: None)

    assert _embedding_notices(client.get("/v1/memory").json()) == []


def test_memory_endpoint_keeps_the_last_result_on_errors(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_store: Any) -> None:
        raise fm.FactsSchemaNotReadyError(fm.FactsSchemaStatus.UNREADABLE)

    monkeypatch.setattr(observability, "refresh_index_status", broken)
    monkeypatch.setattr(observability, "current_index_status", _mismatched_status)

    response = client.get("/v1/memory")

    assert response.status_code == 200
    assert len(_embedding_notices(response.json())) == 1


def test_memory_endpoint_does_not_wait_long_for_a_busy_worker(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(_store: Any) -> None:
        time.sleep(0.5)

    monkeypatch.setattr(observability, "INDEX_CHECK_WAIT_S", 0.05)
    monkeypatch.setattr(observability, "refresh_index_status", slow)
    monkeypatch.setattr(observability, "current_index_status", _mismatched_status)

    assert len(_embedding_notices(client.get("/v1/memory").json())) == 1


def test_memory_endpoint_never_builds_the_fact_store(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(fm.FactualStore, "existing_shared", classmethod(lambda cls: None))
    monkeypatch.setitem(app.state.context, "memory_factual_store", None)
    monkeypatch.setattr(
        fm.FactualStore, "shared", classmethod(lambda cls: pytest.fail("must not build"))
    )
    monkeypatch.setattr(observability, "refresh_index_status", lambda store: seen.append(store))

    client.get("/v1/memory")

    assert seen == [None]


def test_memory_endpoint_reuses_a_fresh_result(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []

    def counted(store: Any) -> ei.IndexStatus:
        calls.append(store)
        return _mismatched_status()

    monkeypatch.setattr(observability, "refresh_index_status", counted)
    monkeypatch.setattr(observability, "fresh_index_status", lambda _age: None)
    client.get("/v1/memory")
    monkeypatch.setattr(observability, "fresh_index_status", lambda _age: _mismatched_status())
    body = client.get("/v1/memory").json()

    assert len(calls) == 1
    assert len(_embedding_notices(body)) == 1


def test_startup_logs_a_mismatch(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    monkeypatch.setattr(observability, "refresh_index_status", lambda _store: _mismatched_status())
    with caplog.at_level(logging.WARNING), TestClient(app):
        pass

    assert any("embedding model" in r.getMessage().lower() for r in caplog.records
               if r.levelno == logging.WARNING)


def test_startup_survives_a_failing_check(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_store: Any) -> None:
        raise fm.FactsSchemaNotReadyError(fm.FactsSchemaStatus.UNREADABLE)

    monkeypatch.setattr(observability, "refresh_index_status", broken)

    with TestClient(app) as started:
        assert started.app is main_module.app
