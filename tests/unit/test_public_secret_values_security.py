"""三把自動持久化密鑰拒用歷來公開過的值（HEARTBEAT_TOKEN／TOUKA_AUTH_SECRET／DISPATCH_TOKEN）.

公開值一律不採用，三個解析點（心跳 token、登入簽章金鑰、派工 token）都一樣：
- `.env` 已存非公開值 → 沿用它並同步到執行中的程序，不寫檔（容器只重啟、沒重建時，
  程序環境還是公開值；不能每次重啟都換一個值）。
- 否則 → 產生新的隨機值寫回 `.env`。
- 寫不回或 bootstrap 關閉 → fail-closed：心跳只剩 JWT、登入金鑰用臨時值、派工子程序不帶 token。

比對一定是「鍵名＋值」：別的鍵用到同一個佔位字串不算。所有 `.env` 都是 tmp_path 裡的
暫存檔；`apply_runtime_env` 改到的 os.environ／settings／config 常數都由 monkeypatch 還原。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import jwt
import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from src import config
from src.core import security
from src.core.exceptions import ErrorCode, ToukaException
from src.core.security import AuthSecretManager
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import heartbeat
from src.routers.heartbeat import HeartbeatAwakenRequest, verify_heartbeat_token
from src.routers.setup import get_setup_service
from src.services.dispatch_supervisor import DispatchSupervisor
from src.services.identity_router import get_verified_identity
from src.services.setup_service import SetupService

if TYPE_CHECKING:
    from pathlib import Path

KEYS = ("HEARTBEAT_TOKEN", "TOUKA_AUTH_SECRET", "DISPATCH_TOKEN")

# 歷來公開過的值（出貨預設、範本佔位、文件範例）。刻意獨立列在測試裡、不讀產品對照表：
# 對照表少一個值時，這裡才會變紅。
PUBLIC_VALUES: tuple[tuple[str, str], ...] = (
    ("HEARTBEAT_TOKEN", "touka_heartbeat_secret_2026"),
    ("HEARTBEAT_TOKEN", "<your-generated-heartbeat-token>"),
    ("HEARTBEAT_TOKEN", "your_secure_token"),
    ("TOUKA_AUTH_SECRET", "TOUKA_INSECURE_DEFAULT_SECRET_FOR_DEV"),
    ("TOUKA_AUTH_SECRET", "touka_default_secret_2026"),
    ("TOUKA_AUTH_SECRET", "<your-random-secret>"),
    ("DISPATCH_TOKEN", "<your-generated-dispatch-token>"),
    ("DISPATCH_TOKEN", "touka_dispatch_secret_2026"),
    ("DISPATCH_TOKEN", "your_secret"),
)
PADDINGS = (("", ""), ("  ", "\t\n"))
PADDING_IDS = ["exact", "padded"]


def _values_of(key: str) -> list[str]:
    return [value for name, value in PUBLIC_VALUES if name == key]


def _case_id(case: tuple[str, str]) -> str:
    return f"{case[0]}-{PUBLIC_VALUES.index(case)}"


def _value_ids(key: str) -> list[str]:
    """測試編號只用序號，不把值印進輸出."""
    return [f"{key}-{index}" for index, _ in enumerate(_values_of(key))]


@pytest.fixture
def env_file(monkeypatch, tmp_path) -> Path:
    """暫存 `.env`＋bootstrap 開；三把金鑰的程序狀態先清空，結束時全部還原."""
    path = tmp_path / ".env"
    monkeypatch.setattr(config, "resolve_env_file_path", lambda: path)
    monkeypatch.setattr(config, "is_bootstrap_enabled", lambda: True)
    for key in KEYS:
        monkeypatch.setenv(key, "")  # 先登記原值（含「不存在」），teardown 才會還原
        monkeypatch.delenv(key)
        monkeypatch.setattr(config.settings, key, "")
        if hasattr(config, key):
            monkeypatch.setattr(config, key, "")
    monkeypatch.setattr(security, "TOUKA_AUTH_SECRET", "")
    AuthSecretManager.reset_for_test()
    yield path
    AuthSecretManager.reset_for_test()


def _set_process_value(monkeypatch, key: str, value: str) -> None:
    """模擬程序環境（容器建立時從 env_file 讀進來的值）帶著這個值啟動."""
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(config.settings, key, value)
    if hasattr(config, key):
        monkeypatch.setattr(config, key, value)
    if key == "TOUKA_AUTH_SECRET":
        monkeypatch.setattr(security, "TOUKA_AUTH_SECRET", value)


def _resolve(key: str, current: str) -> str | None:
    """經該金鑰的產品解析點解析一次；派工回傳子程序環境裡的值（沒有就是 None）."""
    if key == "HEARTBEAT_TOKEN":
        return config._resolve_heartbeat_token(current)
    if key == "TOUKA_AUTH_SECRET":
        AuthSecretManager.reset_for_test()
        return AuthSecretManager.get_secret()
    return DispatchSupervisor().build_child_env().get(key)


def _is_listed(key: str, value: str | None) -> bool:
    return (value or "").strip() in _values_of(key)


# --------------------------------------------------------------------------- #
# 共用判斷：鍵名＋值
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", PUBLIC_VALUES, ids=_case_id)
@pytest.mark.parametrize(("before", "after"), PADDINGS, ids=PADDING_IDS)
def test_every_public_value_is_recognised(case, before, after):
    key, value = case
    assert config.is_public_secret(key, f"{before}{value}{after}") is True


def test_match_needs_key_and_value():
    """別的鍵用同一個佔位字串（例如 SearXNG 的範本）不在範圍內."""
    assert config.is_public_secret("SEARXNG_SECRET", "<your-random-secret>") is False
    assert config.usable_secret("SEARXNG_SECRET", "<your-random-secret>") == (
        "<your-random-secret>"
    )


# --------------------------------------------------------------------------- #
# 三把金鑰：公開值 → 換新並寫回
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", PUBLIC_VALUES, ids=_case_id)
@pytest.mark.parametrize(("before", "after"), PADDINGS, ids=PADDING_IDS)
def test_public_value_is_replaced_and_persisted(monkeypatch, env_file, caplog, case, before, after):
    key, public = case
    env_file.write_text(f"{key}={public}\nUNRELATED=keep\n", encoding="utf-8")
    _set_process_value(monkeypatch, key, f"{before}{public}{after}")
    caplog.set_level(logging.INFO)

    resolved = _resolve(key, f"{before}{public}{after}")

    assert resolved
    assert not _is_listed(key, resolved)
    persisted = dotenv_values(env_file)
    assert persisted[key] == resolved
    assert persisted["UNRELATED"] == "keep"
    assert public not in env_file.read_text(encoding="utf-8")
    assert key in caplog.text
    assert public not in caplog.text
    assert resolved not in caplog.text


@pytest.mark.parametrize("case", PUBLIC_VALUES, ids=_case_id)
@pytest.mark.parametrize("failure", ["bootstrap_off", "write_fails"])
def test_public_value_fails_closed_without_touching_env(monkeypatch, env_file, case, failure):
    key, public = case
    env_file.write_text(f"{key}={public}\n", encoding="utf-8")
    original = env_file.read_bytes()
    _set_process_value(monkeypatch, key, public)
    if failure == "bootstrap_off":
        monkeypatch.setattr(config, "is_bootstrap_enabled", lambda: False)
    else:
        monkeypatch.setattr(config, "set_key", MagicMock(side_effect=OSError("read-only")))

    resolved = _resolve(key, public)

    if key == "HEARTBEAT_TOKEN":
        assert resolved == ""  # 只剩 JWT
    elif key == "TOUKA_AUTH_SECRET":
        assert resolved
        assert len(resolved) == 64  # 既有的臨時隨機值
        assert not _is_listed(key, resolved)
    else:
        assert resolved is None  # 子程序不帶 token → 派工拒絕所有請求
    assert env_file.read_bytes() == original


@pytest.mark.parametrize("case", PUBLIC_VALUES, ids=_case_id)
def test_public_process_value_uses_persisted_value_without_writing(
    monkeypatch, env_file, caplog, case
):
    """M1：程序環境還是公開值，但 `.env` 已存 X → 用 X，`.env` 位元組不變，程序同步成 X."""
    key, public = case
    stored = f"stored-non-public-{key.lower()}-value"
    env_file.write_text(f"# kept\n{key}={stored}\n", encoding="utf-8")
    original = env_file.read_bytes()
    _set_process_value(monkeypatch, key, public)
    caplog.set_level(logging.INFO)

    resolved = _resolve(key, public)

    assert resolved == stored
    assert env_file.read_bytes() == original
    assert config.get_full_env()[key] == stored
    assert getattr(config.settings, key) == stored
    if hasattr(config, key):
        assert getattr(config, key) == stored
    # 同步後啟動守衛的「值相符豁免」成立，不會把這份 .env 誤判成幽靈檔。
    live = {**config.get_full_env(), "GEMINI_API_KEY": "synthetic-live-key"}
    assert config.verify_env_persistence_integrity(env_file, live) == []
    assert key in caplog.text
    assert public not in caplog.text
    assert stored not in caplog.text


@pytest.mark.parametrize("key", KEYS)
def test_persisted_public_value_is_not_reused(monkeypatch, env_file, key):
    """`.env` 存的也是公開值（只是空白不同）時不能沿用，照樣換新."""
    public = _values_of(key)[0]
    env_file.write_text(f'{key}=" {public} "\n', encoding="utf-8")
    _set_process_value(monkeypatch, key, public)

    resolved = _resolve(key, public)

    assert resolved
    assert not _is_listed(key, resolved)
    assert dotenv_values(env_file)[key] == resolved


@pytest.mark.parametrize("key", KEYS)
def test_custom_value_is_returned_unchanged(monkeypatch, env_file, key):
    custom = f"operator-chosen-{key.lower()}-value"
    env_file.write_text(f"{key}=something-else\n", encoding="utf-8")
    original = env_file.read_bytes()
    _set_process_value(monkeypatch, key, custom)

    assert _resolve(key, custom) == custom
    assert env_file.read_bytes() == original
    assert config.get_full_env()[key] == custom


# --------------------------------------------------------------------------- #
# 心跳：公開的靜態 token 與公開登入金鑰簽的 JWT 都叫不醒
# --------------------------------------------------------------------------- #


def _request(header_token: str) -> MagicMock:
    request = MagicMock()
    request.headers.get = lambda name, default="": (
        header_token if name == "X-Heartbeat-Token" else default
    )
    return request


def _awaken(token: str) -> str:
    return verify_heartbeat_token(
        request=_request(token), req=HeartbeatAwakenRequest(token="", agent_id="DebugAgent")
    )


@pytest.mark.parametrize("public", _values_of("HEARTBEAT_TOKEN"), ids=_value_ids("HEARTBEAT_TOKEN"))
@pytest.mark.parametrize("bootstrap", [True, False])
def test_public_heartbeat_token_cannot_awaken(monkeypatch, env_file, public, bootstrap):
    env_file.write_text(f"HEARTBEAT_TOKEN={public}\n", encoding="utf-8")
    monkeypatch.setattr(config, "is_bootstrap_enabled", lambda: bootstrap)
    monkeypatch.setattr(security, "TOUKA_AUTH_SECRET", "synthetic-signing-key-" + "x" * 32)
    monkeypatch.setattr(heartbeat, "HEARTBEAT_TOKEN", config._resolve_heartbeat_token(public))

    with pytest.raises(ToukaException) as rejected:
        _awaken(public)
    assert rejected.value.code == ErrorCode.HEARTBEAT_INVALID_TOKEN


@pytest.mark.parametrize(
    "public", _values_of("TOUKA_AUTH_SECRET"), ids=_value_ids("TOUKA_AUTH_SECRET")
)
@pytest.mark.parametrize("bootstrap", [True, False])
@pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")  # 刻意用短的公開值偽造
def test_jwt_signed_with_public_auth_secret_cannot_awaken(monkeypatch, env_file, public, bootstrap):
    env_file.write_text(f"TOUKA_AUTH_SECRET={public}\n", encoding="utf-8")
    monkeypatch.setattr(config, "is_bootstrap_enabled", lambda: bootstrap)
    _set_process_value(monkeypatch, "TOUKA_AUTH_SECRET", public)
    monkeypatch.setattr(heartbeat, "HEARTBEAT_TOKEN", "")
    claims = {"sub": "heartbeat", "type": "service"}

    forged = jwt.encode(claims, public, algorithm="HS256")
    with pytest.raises(ToukaException) as rejected:
        _awaken(forged)
    assert rejected.value.code == ErrorCode.HEARTBEAT_INVALID_TOKEN

    genuine = jwt.encode(claims, AuthSecretManager.get_secret(), algorithm="HS256")
    assert _awaken(genuine) == genuine  # 對照組：用實際生效的金鑰簽的仍然有效


# --------------------------------------------------------------------------- #
# 派工：父環境繼承來的公開值不進子程序
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("public", _values_of("DISPATCH_TOKEN"), ids=_value_ids("DISPATCH_TOKEN"))
def test_inherited_public_dispatch_token_is_dropped(monkeypatch, env_file, public):
    monkeypatch.setattr(config, "is_bootstrap_enabled", lambda: False)
    monkeypatch.setenv("DISPATCH_TOKEN", public)
    monkeypatch.setattr(config.settings, "DISPATCH_TOKEN", "")

    child_env = DispatchSupervisor().build_child_env()

    assert "DISPATCH_TOKEN" not in child_env


# --------------------------------------------------------------------------- #
# 設定精靈：公開值與空值都顯示「還是預設值」
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "token",
    ["", "   ", *_values_of("DISPATCH_TOKEN"), " your_secret\n"],
    ids=["empty", "blank", *_value_ids("DISPATCH_TOKEN"), "padded"],
)
def test_setup_wizard_flags_public_or_empty_dispatch_token(monkeypatch, token):
    monkeypatch.setenv("DISPATCH_TOKEN", token)
    assert SetupService().get_status().dispatch_token_is_default is True


def test_setup_wizard_accepts_custom_dispatch_token(monkeypatch):
    monkeypatch.setenv("DISPATCH_TOKEN", "operator-chosen-dispatch-value")
    assert SetupService().get_status().dispatch_token_is_default is False


# --------------------------------------------------------------------------- #
# 設定精靈 apply：公開值拒收（422、`.env` 不寫）；非公開值照舊寫入
# --------------------------------------------------------------------------- #

# 精靈能寫的兩把金鑰 → apply 的參數名（也是 API 欄位名）
WIZARD_FIELDS = {"DISPATCH_TOKEN": "dispatch_token", "TOUKA_AUTH_SECRET": "auth_secret"}
WIZARD_PUBLIC_VALUES = tuple(case for case in PUBLIC_VALUES if case[0] in WIZARD_FIELDS)
_WIZARD_ADMIN = AgentIdentity.for_role(
    agent_id="DebugAgent", role="admin", capabilities=[], enabled_tools=[]
)


def _wizard_env(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text("DISPATCH_TOKEN=kept-dispatch\nTOUKA_AUTH_SECRET=kept-auth\n", encoding="utf-8")
    return path


def _post_apply(env_path: Path, body: dict[str, str]):
    """POST /v1/admin/setup/apply；精靈只寫暫存 `.env`（覆寫 get_setup_service）."""
    app.dependency_overrides[get_verified_identity] = lambda: _WIZARD_ADMIN
    app.dependency_overrides[get_setup_service] = lambda: SetupService(
        env_file_path=str(env_path)
    )
    try:
        return TestClient(app, raise_server_exceptions=False).post(
            "/v1/admin/setup/apply", json=body
        )
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)
        app.dependency_overrides.pop(get_setup_service, None)


@pytest.mark.parametrize("case", WIZARD_PUBLIC_VALUES, ids=_case_id)
@pytest.mark.parametrize(("before", "after"), PADDINGS, ids=PADDING_IDS)
def test_setup_wizard_apply_rejects_public_value(tmp_path, case, before, after):
    key, public = case
    env_path = _wizard_env(tmp_path)
    original = env_path.read_bytes()

    with pytest.raises(ValueError, match=WIZARD_FIELDS[key]) as rejected:
        SetupService(env_file_path=str(env_path)).apply(
            **{WIZARD_FIELDS[key]: f"{before}{public}{after}"}
        )

    assert public not in str(rejected.value)
    assert env_path.read_bytes() == original


@pytest.mark.parametrize("case", WIZARD_PUBLIC_VALUES, ids=_case_id)
def test_setup_wizard_public_value_blocks_the_whole_apply(tmp_path, case):
    """另一欄是合法新值也不寫：先全部驗完才寫（不留半套設定）."""
    key, public = case
    other = next(field for name, field in WIZARD_FIELDS.items() if name != key)
    env_path = _wizard_env(tmp_path)
    original = env_path.read_bytes()

    with pytest.raises(ValueError, match=WIZARD_FIELDS[key]):
        SetupService(env_file_path=str(env_path)).apply(
            **{WIZARD_FIELDS[key]: public, other: "fresh-operator-value"}
        )

    assert env_path.read_bytes() == original


@pytest.mark.parametrize("case", WIZARD_PUBLIC_VALUES, ids=_case_id)
def test_setup_wizard_endpoint_returns_422_for_public_value(tmp_path, case):
    key, public = case
    env_path = _wizard_env(tmp_path)
    original = env_path.read_bytes()

    response = _post_apply(env_path, {WIZARD_FIELDS[key]: public})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"
    assert public not in response.text
    assert env_path.read_bytes() == original


@pytest.mark.parametrize("key", list(WIZARD_FIELDS))
def test_setup_wizard_apply_still_writes_non_public_value(tmp_path, key):
    """釘住：非公開值照舊寫入，其他行不動."""
    custom = f"operator-chosen-{key.lower()}-value"
    env_path = _wizard_env(tmp_path)

    SetupService(env_file_path=str(env_path)).apply(**{WIZARD_FIELDS[key]: custom})

    persisted = dotenv_values(env_path)
    assert persisted[key] == custom
    assert len(persisted) == len(WIZARD_FIELDS)


def test_setup_wizard_endpoint_still_writes_non_public_values(tmp_path):
    env_path = _wizard_env(tmp_path)
    body = {field: f"operator-chosen-{field}" for field in WIZARD_FIELDS.values()}

    response = _post_apply(env_path, body)

    assert response.status_code == 200, response.text
    persisted = dotenv_values(env_path)
    assert {key: persisted[key] for key in WIZARD_FIELDS} == {
        key: body[field] for key, field in WIZARD_FIELDS.items()
    }
