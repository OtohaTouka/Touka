"""ADR-0151：密鑰持久化 .env 錨定 compose 目錄（與 WORKSPACE_ROOT 解耦）＋分歧守衛。

根因：TOUKA_ENV_FILE=/app/workspace/.env 把「寫入的 .env」綁在 ${WORKSPACE_ROOT} 掛載上。
使用者設 WORKSPACE_ROOT=D:/ → /app/workspace＝D: 磁碟根 → 寫入落到幽靈 D:/.env，而 compose
的 env_file 實際載入的是 compose 目錄的 D:/Touka/.env → 自動生成的三個 token 寫到沒人讀的檔、
每次重啟重生成（首裝精靈亦寫同一幽靈檔）。

修法：新增穩定掛載 `.`（compose 目錄，永遠含被 env_file 載入的 .env）→ /app/host_root，
TOUKA_ENV_FILE 改指 /app/host_root/.env——與 WORKSPACE_ROOT 解耦。set_key 以 temp+rename
覆寫，故必須把 .env 寫在「目錄掛載」內的檔案（非單檔掛載點）。另加啟動 fail-loud 守衛。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src import config

ROOT = Path(__file__).resolve().parents[2]
COMPOSES = [ROOT / "docker-compose.yml", ROOT / "docker-compose.ghcr.yml"]


def _proxy(compose_path: Path) -> dict:
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    return data["services"]["touka-proxy"]


def _str_list(value: object) -> list[str]:
    return [v for v in (value or []) if isinstance(v, str)]


# ── compose：.env 錨定 compose 目錄（解耦 WORKSPACE_ROOT） ──────────────────────


@pytest.mark.parametrize("compose_path", COMPOSES, ids=lambda p: p.name)
def test_env_file_anchored_to_host_root_not_workspace(compose_path: Path):
    """TOUKA_ENV_FILE 指向 /app/host_root/.env（compose 目錄），不再綁 /app/workspace。"""
    env = _str_list(_proxy(compose_path).get("environment"))
    assert any(e.replace(" ", "") == "TOUKA_ENV_FILE=/app/host_root/.env" for e in env), (
        f"{compose_path.name}: proxy 應設 TOUKA_ENV_FILE=/app/host_root/.env（ADR-0151 解耦）；env={env!r}"
    )
    assert not any("TOUKA_ENV_FILE=/app/workspace/.env" in e for e in env), (
        f"{compose_path.name}: 不得再把 TOUKA_ENV_FILE 綁在 WORKSPACE_ROOT 的 /app/workspace"
    )


@pytest.mark.parametrize("compose_path", COMPOSES, ids=lambda p: p.name)
def test_compose_dir_mounted_at_host_root(compose_path: Path):
    """proxy 必須把 compose 目錄（`.`）以『目錄掛載』掛到 /app/host_root（set_key rename 需目錄掛載）。"""
    vols = _str_list(_proxy(compose_path).get("volumes"))
    assert any(v.endswith(":/app/host_root") for v in vols), (
        f"{compose_path.name}: proxy 應掛 compose 目錄到 /app/host_root（穩定、與 WORKSPACE_ROOT 無關）；volumes={vols!r}"
    )
    # 來源須為 compose 目錄（`.` / `./`），與 ${WORKSPACE_ROOT} 無關
    host_root_mounts = [v for v in vols if v.endswith(":/app/host_root")]
    assert all(
        m.split(":")[0] in {".", "./"} for m in host_root_mounts
    ), f"{compose_path.name}: /app/host_root 來源須為 compose 目錄（. 或 ./），實得 {host_root_mounts!r}"


# ── 啟動 fail-loud 守衛：偵測寫入 .env 與載入設定分歧（幽靈檔） ──────────────────


def _write_env(path: Path, keys: list[str]) -> None:
    path.write_text("".join(f"{k}=x\n" for k in keys), encoding="utf-8")


def test_guard_flags_phantom_env(tmp_path):
    """os.environ 有生效設定鍵、但持久化 .env 一個都沒有 → 判為幽靈檔、回報 missing。"""
    persist = tmp_path / ".env"
    _write_env(persist, ["DISPATCH_TOKEN", "HEARTBEAT_TOKEN"])  # 幽靈檔：只有 token、無 API 金鑰
    environ = {"ANTHROPIC_API_KEY": "sk-real", "WORKSPACE_ROOT": "D:/", "GEMINI_API_KEY": "g"}
    missing = config.verify_env_persistence_integrity(env_path=persist, environ=environ)
    assert set(missing) == {"ANTHROPIC_API_KEY", "WORKSPACE_ROOT", "GEMINI_API_KEY"}


def test_guard_quiet_when_aligned(tmp_path):
    """持久化 .env 含生效鍵（≥1）→ 非幽靈、不報警（回空）。"""
    persist = tmp_path / ".env"
    _write_env(persist, ["ANTHROPIC_API_KEY", "WORKSPACE_ROOT", "DISPATCH_TOKEN"])
    environ = {"ANTHROPIC_API_KEY": "sk-real", "WORKSPACE_ROOT": "D:/Touka"}
    assert config.verify_env_persistence_integrity(env_path=persist, environ=environ) == []


def test_guard_quiet_when_no_live_keys(tmp_path):
    """os.environ 沒有任何 probe 設定鍵（全新、金鑰未填）→ 無可分歧、不報警。"""
    persist = tmp_path / ".env"
    _write_env(persist, ["DISPATCH_TOKEN"])
    assert config.verify_env_persistence_integrity(env_path=persist, environ={}) == []


def test_guard_partial_overlap_is_not_phantom(tmp_path):
    """部分鍵在 .env（例：金鑰走 compose environment，另有些在 .env）→ 非幽靈、不報警。"""
    persist = tmp_path / ".env"
    _write_env(persist, ["WORKSPACE_ROOT"])  # WORKSPACE_ROOT 在 .env，API 金鑰走別處
    environ = {"ANTHROPIC_API_KEY": "sk", "WORKSPACE_ROOT": "D:/Touka"}
    # 至少一個 live 鍵在持久化檔 → 非幽靈
    assert config.verify_env_persistence_integrity(env_path=persist, environ=environ) == []


def test_guard_missing_file_no_false_alarm(tmp_path):
    """持久化 .env 不存在（全新、尚無 .env）→ 無法比對、不誤報（回空）。"""
    persist = tmp_path / "nope" / ".env"
    environ = {"ANTHROPIC_API_KEY": "sk"}
    assert config.verify_env_persistence_integrity(env_path=persist, environ=environ) == []


def test_guard_token_roundtrip_exonerates(tmp_path):
    """值相符豁免（審查 #2）：operator 以 compose environment:/-e 注入 API 金鑰（探針鍵不進
    .env）、.env 只有自動生成的 token 且已被 env_file 載入（檔值==生效值）→ 健康、不誤報。"""
    persist = tmp_path / ".env"
    persist.write_text(
        "TOUKA_AUTH_SECRET=s1\nDISPATCH_TOKEN=d1\nHEARTBEAT_TOKEN=h1\n", encoding="utf-8"
    )
    environ = {"ANTHROPIC_API_KEY": "sk-injected", "DISPATCH_TOKEN": "d1"}  # 往返一致
    assert config.verify_env_persistence_integrity(env_path=persist, environ=environ) == []


def test_guard_token_value_mismatch_still_flags(tmp_path):
    """幽靈檔含 token 鍵但值與生效值不符（生效值空/不同＝檔沒被載入）→ 不豁免、仍報警。"""
    persist = tmp_path / ".env"
    persist.write_text(
        "DISPATCH_TOKEN=stale-phantom\nHEARTBEAT_TOKEN=stale2\n", encoding="utf-8"
    )
    environ = {"ANTHROPIC_API_KEY": "sk-real", "DISPATCH_TOKEN": "different-live"}
    missing = config.verify_env_persistence_integrity(env_path=persist, environ=environ)
    assert missing == ["ANTHROPIC_API_KEY"]


def test_guard_survives_non_utf8_env(tmp_path):
    """持久化 .env 為非 UTF-8（如 UTF-16/亂碼）→ 不炸啟動、視為無法比對（回空）。"""
    persist = tmp_path / ".env"
    persist.write_bytes("ANTHROPIC_API_KEY=x\n".encode("utf-16"))
    environ = {"ANTHROPIC_API_KEY": "sk"}
    assert config.verify_env_persistence_integrity(env_path=persist, environ=environ) == []


# ── G13（守衛涵蓋缺口）：探針鍵純度——絕不由 proxy compose environment: 注入 ──────────
# _ENV_PERSISTENCE_PROBE_KEYS 的判準是「生效鍵全數不在持久化 .env 才報幽靈檔」，前提是這些鍵
# 『只會經 .env 進入』。若混入 proxy 的 environment:（如誤加 WORKSPACE_ROOT），該鍵在 os.environ
# 恆存在卻可能合法不在健康 .env → 守衛靜默誤報/漏報。此不變式先前靠人工檢視、無機器比對。


@pytest.mark.parametrize("compose_path", COMPOSES, ids=lambda p: p.name)
def test_probe_keys_never_injected_by_proxy_environment(compose_path: Path):
    """proxy 的 compose environment: 鍵集與 _ENV_PERSISTENCE_PROBE_KEYS 交集必須為空。"""
    env = _str_list(_proxy(compose_path).get("environment"))
    injected_keys = {e.split("=", 1)[0].strip() for e in env if "=" in e}
    probe = set(config._ENV_PERSISTENCE_PROBE_KEYS)  # noqa: SLF001
    overlap = injected_keys & probe
    assert not overlap, (
        f"{compose_path.name}: proxy environment: 注入了探針鍵 {sorted(overlap)}——"
        "探針鍵須只來自 .env（ADR-0151/G13）；混入 environment 會讓幽靈檔守衛判準失真"
    )
