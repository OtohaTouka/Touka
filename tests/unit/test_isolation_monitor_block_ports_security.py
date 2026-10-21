"""封鎖埠 B 的 monitor 端（iso-c2 C2-2 U2d；設計 2.3 版 §2.10，HIGH-1／MEDIUM-A／MEDIUM-B）.

- 上游埠從 ``connections.json`` 的全部條目推導（在 refresh 的執行緒那一段經 ResourceManager 讀、
  只取 url），記憶體裡的來源（路由快照、registry）取聯集；行程存活期間只增不減。
- 三種讀取狀態：未讀等同讀不到；讀不到（``block_ports_incomplete``）或溢位
  （``block_ports_overflow``）判 none，降級去處保留執行器或本機 Landlock、features 不含 tcp。
- 金鑰：log、狀態與往外丟的例外都不帶 ``connections.json`` 的內容。
"""

from __future__ import annotations

import asyncio
import errno
import functools
import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from src.config import settings
from src.main import app
from src.models.identity import AgentIdentity
from src.models.system_restore import RestoreStatus
from src.routers import connections as connections_router
from src.routers import system as system_router
from src.sandbox_kernel import spec as kernel_spec
from src.services import settings_snapshot_service as settings_snapshot
from src.services.identity_router import get_verified_identity
from src.services.isolation import block_ports as bp
from src.services.isolation import status as status_module
from src.services.isolation.consent import Consent, ExecutionGrant
from src.services.isolation.executor_client import ExecutorHealth, ExecutorReady, ExecutorRoot
from src.services.isolation.profiles import SandboxLayout, SandboxProfile
from src.services.isolation.runners import ExecRequest, RunnerBroker
from src.services.isolation.status import (
    BELOW_EXPECTED_INTERVAL_SECONDS,
    FallbackPlan,
    IsolationMonitor,
)
from src.services.models import model_manager
from src.services.resource_manager import resource_manager
from src.utils import config_utils
from src.utils.isolation_policy import landlock_features

from tests.support.isolation import FixedBroker, fixed_interpreter

FAKE_API_KEY = "sk-test-upstream-fake-0123456789"
ANY_V4 = ".".join("0000")  # 0.0.0.0（寫成字面值會被當成 bind 所有介面）
UPSTREAM = 4100  # 使用者自己的本機模型閘道（不在任何預設裡）
OWNER = AgentIdentity.for_role(agent_id="OwnerUser", role="Owner", is_human=True, capabilities=[])

# --- 純函式：非公網判斷、上游埠、核心 B ------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "localhost", "LOCALHOST.", "api.localhost", "host.docker.internal",
        "gateway.docker.internal", "host.containers.internal", "printer.local", "nas.home.arpa",
        "litellm", "ollama", "127.0.0.1", "127.1", "2130706433", "0x7f.1", "10.1.2.3",
        "172.16.0.1", "192.168.1.1", "100.64.0.1", "169.254.1.1", ANY_V4, "::1", "fd00::1",
        "fe80::1", "::",
    ],
)  # fmt: skip
def test_non_public_hosts(host):
    assert bp.is_non_public_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["api.openai.com", "8.8.8.8", "2001:4860:4860::8888", "example.com", "localhost.example.com"],
)
def test_public_hosts(host):
    assert bp.is_non_public_host(host) is False


@pytest.mark.parametrize(
    ("url", "port"),
    [
        ("http://localhost:4000", 4000),
        ("http://user:pass@litellm:4001/v1", 4001),
        ("https://[::1]:8443/x", 8443),
        ("http://localhost", 80),
        ("https://host.docker.internal", 443),
        ("https://api.openai.com/v1", None),
        ("http://localhost:99999", None),
        ("not a url", None),
        ("", None),
        ("ftp://localhost", None),
    ],
)
def test_upstream_port(url, port):
    assert bp.upstream_port(url) == port


def test_core_b_has_touka_ports_and_local_settings_upstreams_only(monkeypatch):
    monkeypatch.setattr(settings, "LITELLM_BASE_URL", "http://localhost:4000")
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "TOOL_BASE_URL", "http://localhost:80")  # 80 不加
    monkeypatch.setattr(settings, "SEARXNG_BASE_URL", "http://searxng:8888")
    executor = bp.core_block_ports("executor")
    proxy = bp.core_block_ports("proxy")
    assert set(kernel_spec.DEFAULT_BLOCK_PORTS) <= set(executor)
    assert {4000, 11434} <= set(executor)
    assert 80 not in executor and 443 not in executor
    assert 8888 in proxy and 8888 not in executor  # SearXNG 只在 proxy
    assert list(executor) == sorted(executor)


def test_core_b_follows_runtime_settings_changes(monkeypatch):
    """apply_runtime_env 在執行中改 Settings：下一次組 B 就跟著變（不用等 refresh）。"""
    monkeypatch.setattr(settings, "CHAT_PORT", "3999")
    assert 3999 in bp.core_block_ports("executor")
    monkeypatch.setattr(settings, "CHAT_PORT", "3998")
    monkeypatch.setattr(settings, "PORT", 8123)
    ports = bp.core_block_ports("executor")
    assert {3998, 8123} <= set(ports) and 3999 not in ports


# --- monitor：讀取狀態、只增不減 ------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def _selftest(_kernel_dir: Path, _test_dir: Path) -> kernel_spec.SelfTestResult:
    return kernel_spec.SelfTestResult(ok=True, abi=6)


def _layout(tmp_path: Path, *, executor: bool = False) -> SandboxLayout:
    data = tmp_path / "data"
    (data / "mcp_servers").mkdir(parents=True, exist_ok=True)
    kernel = tmp_path / "kernel"
    kernel.mkdir(exist_ok=True)
    (kernel / "launcher.py").write_text("# kernel\n", encoding="utf-8")
    live = tmp_path / "ToukaProxy" / "src" / "sandbox_kernel"
    live.mkdir(parents=True, exist_ok=True)
    (live / "launcher.py").write_text("# kernel\n", encoding="utf-8")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    return SandboxLayout(
        data_root=data,
        config_dir=tmp_path / "config",
        install_root=tmp_path,
        proxy_root=tmp_path / "ToukaProxy",
        env_file=tmp_path / ".env",
        state_root=tmp_path / "state",
        kernel_dir=kernel,
        executor_dir=tmp_path / "exec" if executor else None,
        mirror_roots=(),
        in_container=True,
    )


def _write_connections(layout: SandboxLayout, *urls: str, enabled: bool = True) -> Path:
    items = [
        {"id": f"c{i}", "name": "x", "url": url, "apiKey": FAKE_API_KEY, "enabled": enabled}
        for i, url in enumerate(urls)
    ]
    path = layout.config_dir / "connections.json"
    path.write_text(json.dumps({"connections": items}), encoding="utf-8")
    return path


class _Executor:
    """執行器替身：READY 照這次 HELLO 的 B 算封鎖埠雜湊；記錄每次收到的 B。"""

    def __init__(self, layout: SandboxLayout) -> None:
        self.layout = layout
        self.calls: list[tuple[int, ...]] = []

    async def __call__(self, _ipc_dir: Path, block_ports: tuple[int, ...]) -> ExecutorHealth:
        self.calls.append(tuple(block_ports))
        if len(block_ports) > kernel_spec.MAX_BLOCK_PORTS:  # 真的伺服器會回 ERROR
            return ExecutorHealth(None, "executor_version_mismatch")
        data, layout = self.layout.data_root, self.layout
        roots = (
            ExecutorRoot(str(data / "personal_drive"), "rw"),
            ExecutorRoot(str(data / "mcp_servers"), "rw"),
            ExecutorRoot(str(layout.config_dir / "skills"), "ro"),
        )
        live = kernel_spec.kernel_digest(layout.proxy_root / "src" / "sandbox_kernel")
        ready = ExecutorReady(
            1, 6, True, None, ("fs", "tcp"), roots, "/x/state", live or "", "e" * 64, "b" * 32,
            _Digests().block_ports_digest(block_ports), 1001,
        )
        return ExecutorHealth(ready)


class _Digests:
    def server_digest(self, _proxy_root: Path) -> str:
        return "e" * 64

    def block_ports_digest(self, block_ports: tuple[int, ...]) -> str:
        return "p" + ",".join(map(str, block_ports))


class _Routes:
    """記憶體裡的上游來源（路由快照、registry）的替身。"""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self) -> list[str]:
        return list(self.urls)


def _monitor(layout: SandboxLayout, **kwargs) -> IsolationMonitor:
    executor = kwargs.pop("executor", None)
    return IsolationMonitor(
        layout_provider=lambda: layout,
        selftest=_selftest,
        clock=kwargs.pop("clock", _Clock()),
        platform="linux",
        start_background=False,
        executor_health=executor or _Executor(layout),
        executor_digests=_Digests(),
        **kwargs,
    )


async def test_upstream_started_after_touka_is_blocked_from_the_first_b_to_the_last(tmp_path):
    """CTO 指定：Touka 先開機、上游還沒起來（探測失敗、路由快照沒有它）；之後上游才開機、探測成功。

    那個埠從第一次組 B（第一次 refresh 讀完設定）開始，到最後每一次組 B 都在 B 裡。
    """
    layout = _layout(tmp_path)
    path = _write_connections(layout, f"http://localhost:{UPSTREAM}/v1")
    routes = _Routes()  # 開機時探測失敗：路由快照是空的
    monitor = _monitor(layout, upstream_urls=routes)
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.block_ports_issue) == ("landlock", None)
    seen = [monitor.block_ports("proxy"), monitor.block_ports("executor")]
    routes.urls = [f"http://localhost:{UPSTREAM}/v1"]  # 上游起來了、探測成功
    seen.append(monitor.block_ports("proxy"))
    routes.urls = []  # 路由刷新中（INITIALIZING）：快照暫時是空的
    seen.append(monitor.block_ports("executor"))
    path.write_text(json.dumps({"connections": []}), encoding="utf-8")  # 從設定拿掉
    await monitor.refresh()
    seen.append(monitor.block_ports("proxy"))
    assert all(UPSTREAM in ports for ports in seen), seen


async def test_disabled_connections_and_memory_sources_are_blocked(tmp_path):
    layout = _layout(tmp_path)
    _write_connections(layout, "http://192.168.1.5:4200", enabled=False)
    routes = _Routes()
    routes.urls = ["http://ollama:11500", "https://api.example.com:9443"]
    monitor = _monitor(layout, upstream_urls=routes)
    await monitor.ensure_ready()
    ports = monitor.block_ports("executor")
    assert {4200, 11500} <= set(ports) and 9443 not in ports


def _real_singleton_b(monkeypatch) -> functools.partial[tuple[int, ...]]:
    """main 接好上游來源的 monitor 單例，算真的 B（替身換的是實例上的方法）."""
    monitor = status_module.isolation_monitor
    for name, fresh in (("_known_ports", set()), ("_warned_ports", set()), ("_overflow", False)):
        monkeypatch.setattr(monitor, name, fresh)  # 只增不減的全域狀態：測完還原
    return functools.partial(IsolationMonitor.block_ports, monitor)


def _gateway(**extra: object) -> dict[str, object]:
    url = f"http://localhost:{UPSTREAM}/v1"
    return {"id": "local-gw", "name": "gw", "url": url, "provider": "custom", **extra}


def test_saved_connection_is_blocked_before_any_refresh_or_discovery(tmp_path, monkeypatch):
    """CTO 指定（U2e2）：從連線設定存一條新連線，不等 monitor refresh、也不等探測（存檔只撤下
    舊路由，探測失敗也一樣），那個埠已經在 B 裡：main 把模型路由的 URL 來源接到 monitor。
    """
    monkeypatch.setattr(connections_router, "CONFIG_PATH", str(tmp_path / "connections.json"))
    real_b = _real_singleton_b(monkeypatch)
    assert UPSTREAM not in real_b("executor")
    item = _gateway(apiKey=FAKE_API_KEY, protocol="openai", enabled=True)
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    resp = TestClient(app).post("/v1/ui/connections", json=[item])
    assert resp.status_code == 200, resp.text
    assert UPSTREAM in real_b("executor") and UPSTREAM in real_b("proxy")


def test_imported_connection_is_blocked_before_any_refresh_or_discovery(tmp_path, monkeypatch):
    """U2e3：設定快照匯入一條本機連線（整份寫 connections.json），不 refresh、不探測就在 B 裡."""
    monkeypatch.setattr(config_utils, "CONFIG_DIR", tmp_path)
    real_b = _real_singleton_b(monkeypatch)
    assert UPSTREAM not in real_b("executor")
    snapshot = {
        "schema_version": settings_snapshot.SCHEMA_VERSION,
        "export_kind": settings_snapshot.EXPORT_KIND,
        "manifest": {},
        "sections": {"connections": {"data": {"connections": [_gateway(enabled=True)]}}},
    }
    settings_snapshot.apply_snapshot(snapshot, mode="merge")
    assert UPSTREAM in real_b("executor") and UPSTREAM in real_b("proxy")


def test_bootstrap_restored_connections_are_blocked_before_discovery(tmp_path, monkeypatch):
    """U2e3：啟動自癒從範本補出 connections.json；不等啟動探測，那個埠已經在 B 裡.

    lifespan 只開自癒那一段：其他設定不補、不驗、密鑰不落盤，啟動探測換成空的，所以 B 裡的
    上游埠只可能來自自癒後的重讀。
    """
    monkeypatch.setenv("TOUKA_BOOTSTRAP_ENABLED", "true")
    for target in ("src.main.is_bootstrap_enabled", "src.config.is_bootstrap_enabled"):
        monkeypatch.setattr(target, lambda: False)  # 只開 lifespan 的自癒段
    monkeypatch.setattr("src.config.verify_env_persistence_integrity", lambda *_a, **_k: None)
    monkeypatch.setattr("src.utils.bootstrap.validate_live_configs", lambda *_a, **_k: [])
    monkeypatch.setattr("src.utils.bootstrap.ESSENTIAL_CONFIGS", ["connections.json"])
    monkeypatch.setattr("src.utils.bootstrap.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_utils, "CONFIG_DIR", tmp_path)
    for name in ("refresh_all_dynamic_models", "fetch_custom_connections_models"):
        monkeypatch.setattr(model_manager, name, AsyncMock())
    monkeypatch.setattr(model_manager, "check_models_health", AsyncMock())
    example = {"connections": [_gateway(enabled=True)]}
    (tmp_path / "connections.json.example").write_text(json.dumps(example), encoding="utf-8")
    real_b = _real_singleton_b(monkeypatch)
    with TestClient(app):
        ports = real_b("executor")
    assert (tmp_path / "connections.json").is_file()
    assert UPSTREAM in ports


@pytest.mark.parametrize("fails", [True, False], ids=["restore-fails", "restore-done"])
def test_restore_blocks_the_restored_connections(tmp_path, monkeypatch, fails):
    """U2e4：系統還原把 connections.json 換掉之後（中途失敗、行程沒重啟也一樣），還原進來的
    連線立刻在 ``route_endpoints`` 和 B 裡.
    """
    monkeypatch.setattr(config_utils, "CONFIG_DIR", tmp_path)
    real_b = _real_singleton_b(monkeypatch)
    restored = {"connections": [_gateway(enabled=True)]}

    def _restore(cmd, **_kwargs):
        (tmp_path / "connections.json").write_text(json.dumps(restored), encoding="utf-8")
        if fails:
            raise subprocess.CalledProcessError(1, cmd)

    exits: list[int] = []
    monkeypatch.setattr(system_router.time, "sleep", lambda _s: None)
    monkeypatch.setattr(system_router, "run_sync", _restore)
    monkeypatch.setattr(system_router.os, "_exit", exits.append)  # 不能真的結束測試行程
    monkeypatch.setattr(system_router, "_last_restore", RestoreStatus(state="idle"))
    system_router._execute_restore(tmp_path / "manage_backup.py", tmp_path / "b.zip")
    assert exits == ([] if fails else [0])
    assert f"http://localhost:{UPSTREAM}/v1" in model_manager.route_endpoints()
    assert UPSTREAM in real_b("executor")


async def test_missing_connections_file_is_read_with_no_custom_upstreams(tmp_path):
    monitor = _monitor(_layout(tmp_path))
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.block_ports_issue) == ("landlock", None)


async def test_unread_before_the_first_refresh_is_incomplete(tmp_path):
    layout = _layout(tmp_path)
    monitor = _monitor(layout)
    await monitor.run_selftest("data", notify=False)  # 本機自測好了、設定還沒讀過
    snap = monitor.snapshot()
    assert (snap.level, snap.reason) == ("none", "block_ports_incomplete")
    assert monitor.fallback_for(profile_complete=True) == FallbackPlan(
        "landlock", landlock_features(6) - {"tcp"}
    )


def _deny_exists(target: Path):
    real = resource_manager.exists

    def _exists(path):
        if Path(path) == target:
            raise PermissionError(errno.EACCES, "denied")
        return real(path)

    return _exists


async def test_exists_raising_is_unreadable_not_missing(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    path = _write_connections(layout, f"http://localhost:{UPSTREAM}")
    monkeypatch.setattr(resource_manager, "exists", _deny_exists(path))
    monitor = _monitor(layout)
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.reason, snap.block_ports_issue) == (
        "none", "block_ports_incomplete", "block_ports_incomplete"
    )
    assert UPSTREAM not in monitor.block_ports("proxy")


_UNREADABLE = {
    "broken_json": '{"connections": [{"url": "http://localhost:1", "apiKey": "' + FAKE_API_KEY,
    "top_level_list": json.dumps([{"url": "http://localhost:1"}]),
    "connections_dict": json.dumps({"connections": {"url": "http://localhost:1"}}),
    "no_connections_key": json.dumps({"items": []}),
    "item_not_dict": json.dumps({"connections": ["http://localhost:1"]}),
    "url_number": json.dumps({"connections": [{"url": 4000, "apiKey": FAKE_API_KEY}]}),
    "url_empty": json.dumps({"connections": [{"url": " ", "apiKey": FAKE_API_KEY}]}),
    "api_key_int_url_missing": json.dumps({"connections": [{"apiKey": 12345}]}),
}


@pytest.mark.parametrize("case", list(_UNREADABLE))
async def test_malformed_connections_are_unreadable(tmp_path, case):
    layout = _layout(tmp_path)
    (layout.config_dir / "connections.json").write_text(_UNREADABLE[case], encoding="utf-8")
    snap = await _monitor(layout).ensure_ready()
    assert (snap.level, snap.reason) == ("none", "block_ports_incomplete")


async def test_read_text_raising_is_unreadable(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    _write_connections(layout, "http://localhost:1")

    def _eio(*_args, **_kwargs):
        raise OSError(errno.EIO, "9p hiccup")

    monkeypatch.setattr(resource_manager, "read_file_text", _eio)
    snap = await _monitor(layout).ensure_ready()
    assert snap.reason == "block_ports_incomplete"


async def test_after_one_good_read_later_failures_keep_the_known_ports(tmp_path, caplog):
    layout = _layout(tmp_path)
    path = _write_connections(layout, f"http://localhost:{UPSTREAM}")
    clock = _Clock()
    monitor = _monitor(layout, clock=clock)
    assert (await monitor.ensure_ready()).level == "landlock"
    path.write_text("{not json", encoding="utf-8")
    caplog.set_level(logging.WARNING)
    snap = await monitor.refresh()
    assert (snap.level, snap.block_ports_issue) == ("landlock", None)
    assert UPSTREAM in monitor.block_ports("proxy")
    assert "JSONDecodeError" in caplog.text


async def test_unreadable_recovers_on_the_next_background_round_without_restart(tmp_path):
    """r4 LOW-4：還沒讀成功過時每一輪都重試；修好之後下一輪背景檢查（約 30 秒）就恢復."""
    layout = _layout(tmp_path)
    path = layout.config_dir / "connections.json"
    path.write_text("{broken", encoding="utf-8")
    clock = _Clock()
    monitor = _monitor(layout, clock=clock)
    assert (await monitor.ensure_ready()).reason == "block_ports_incomplete"
    clock.now += 61  # 過了開機寬限期
    assert monitor.next_interval() == BELOW_EXPECTED_INTERVAL_SECONDS
    _write_connections(layout, f"http://localhost:{UPSTREAM}")
    clock.now += BELOW_EXPECTED_INTERVAL_SECONDS
    await monitor.run_due_checks()
    snap = monitor.snapshot()
    assert (snap.level, snap.block_ports_issue) == ("landlock", None)
    assert UPSTREAM in monitor.block_ports("executor")


async def test_a_failed_or_cancelled_disk_check_never_counts_as_read(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    _write_connections(layout, "http://localhost:1")

    def _boom(_path):
        raise RuntimeError("bug")

    monkeypatch.setattr(status_module, "read_connection_ports", _boom)
    monitor = _monitor(layout)
    await monitor.run_selftest("data", notify=False)
    with pytest.raises(RuntimeError):
        await monitor.refresh()
    assert monitor.snapshot().reason == "block_ports_incomplete"
    release = threading.Event()

    def _slow(_path):
        release.wait(5)
        return bp.ConnectionsRead(ok=True, ports=frozenset())

    monkeypatch.setattr(status_module, "read_connection_ports", _slow)
    task = asyncio.ensure_future(monitor.refresh())
    await asyncio.sleep(0.05)
    monitor._refreshing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.05)
    assert monitor.snapshot().reason == "block_ports_incomplete"


_KEY_NUMBER = 987654321123
_FAKE_API_KEY_CASES = {
    # apiKey 型別不對、而且這一條沒有 url：結構不對
    "api_key_int": (json.dumps({"connections": [{"apiKey": _KEY_NUMBER}]}), str(_KEY_NUMBER)),
    # JSON 壞掉的位置就在 apiKey 附近（JSONDecodeError.doc 是整份原文）
    "broken_near_key": (
        '{"connections": [{"url": "http://x:1", "apiKey": "' + FAKE_API_KEY + '",,]}',
        FAKE_API_KEY,
    ),
    "good": (
        json.dumps({"connections": [{"url": "http://x:1", "apiKey": FAKE_API_KEY}]}),
        FAKE_API_KEY,
    ),
}


@pytest.mark.parametrize("case", list(_FAKE_API_KEY_CASES))
async def test_connection_secrets_never_reach_logs_state_or_errors(tmp_path, caplog, case):
    text, secret = _FAKE_API_KEY_CASES[case]
    layout = _layout(tmp_path)
    (layout.config_dir / "connections.json").write_text(text, encoding="utf-8")
    caplog.set_level(logging.DEBUG)
    monitor = _monitor(layout)
    snap = await monitor.ensure_ready()
    await monitor.refresh()
    assert secret not in caplog.text
    assert secret not in repr(snap) and secret not in repr(vars(monitor))
    result = bp.read_connection_ports(layout.config_dir / "connections.json")
    assert secret not in repr(result)


async def test_unblockable_upstream_ports_are_warned_once_and_not_added(tmp_path, caplog):
    layout = _layout(tmp_path)
    _write_connections(layout, "http://localhost:80", "https://litellm", "http://localhost:53")
    caplog.set_level(logging.WARNING)
    monitor = _monitor(layout)
    await monitor.ensure_ready()
    await monitor.refresh()
    ports = monitor.block_ports("proxy")
    assert not {53, 80, 443} & set(ports)
    assert caplog.text.count("cannot be blocked") == 3


# --- 讀不到與溢位時的層級、降級去處與 B ------------------------------------------------


async def test_incomplete_on_the_executor_falls_back_to_it_without_tcp(tmp_path):
    layout = _layout(tmp_path, executor=True)
    (layout.config_dir / "connections.json").write_text("[]", encoding="utf-8")
    monitor = _monitor(layout)
    snap = await monitor.ensure_ready()
    incomplete = ("none", "executor", "block_ports_incomplete")
    assert (snap.level, snap.expected, snap.reason) == incomplete
    assert snap.executor is not None
    plan = monitor.fallback_for(profile_complete=True, executor_eligible=True)
    assert plan == FallbackPlan("executor", landlock_features(6) - {"tcp"})
    assert monitor.fallback_for(profile_complete=True) == FallbackPlan(
        "landlock", landlock_features(6) - {"tcp"}
    )


def _many_upstreams(count: int) -> list[str]:
    return [f"http://localhost:{20000 + i}" for i in range(count)]


async def test_overflow_is_none_uses_core_b_and_keeps_the_executor(tmp_path):
    """溢位：判 none；B 只用核心 B；健康檢查送的也是這份（r4 LOW-1），所以執行器仍是降級去處."""
    layout = _layout(tmp_path, executor=True)
    _write_connections(layout, *_many_upstreams(kernel_spec.MAX_BLOCK_PORTS))
    executor = _Executor(layout)
    monitor = _monitor(layout, executor=executor)
    snap = await monitor.ensure_ready()
    assert (snap.level, snap.reason, snap.block_ports_issue) == (
        "none", "block_ports_overflow", "block_ports_overflow"
    )
    core = {loc: bp.core_block_ports(loc) for loc in ("proxy", "executor")}
    assert monitor.block_ports("proxy") == core["proxy"]
    assert monitor.block_ports("executor") == core["executor"]
    assert executor.calls[-1] == core["executor"]
    assert snap.executor is not None
    plan = monitor.fallback_for(profile_complete=True, executor_eligible=True)
    assert plan == FallbackPlan("executor", landlock_features(6) - {"tcp"})


async def test_overflow_found_while_building_a_profile_changes_the_level(tmp_path):
    """記憶體來源在兩次 refresh 之間長到溢位：組 B 時發現就改層級並通知（LOW-7）."""
    layout = _layout(tmp_path)
    routes = _Routes()
    monitor = _monitor(layout, upstream_urls=routes)
    assert (await monitor.ensure_ready()).level == "landlock"
    seen: list[str | None] = []

    async def _record(snapshot):
        seen.append(snapshot.reason)

    monitor.on_level_change(_record)
    routes.urls = _many_upstreams(kernel_spec.MAX_BLOCK_PORTS)
    ports = monitor.block_ports("executor")
    await asyncio.sleep(0)
    assert ports == bp.core_block_ports("executor")
    assert monitor.snapshot().reason == "block_ports_overflow"
    assert seen == ["block_ports_overflow"]
    # 去處仍是本機 Landlock（不降到未隔離），features 不含 tcp
    assert monitor.fallback_for(profile_complete=True) == FallbackPlan(
        "landlock", landlock_features(6) - {"tcp"}
    )
    routes.urls = []  # 只增不減：要重新啟動才會恢復
    assert monitor.block_ports("executor") == bp.core_block_ports("executor")
    assert monitor.snapshot().reason == "block_ports_overflow"


async def test_health_check_sends_the_b_the_commands_will_use(tmp_path):
    layout = _layout(tmp_path, executor=True)
    _write_connections(layout, f"http://localhost:{UPSTREAM}")
    executor = _Executor(layout)
    monitor = _monitor(layout, executor=executor)
    assert (await monitor.ensure_ready()).level == "executor"
    assert executor.calls[-1] == monitor.block_ports("executor")
    assert UPSTREAM in executor.calls[-1]


async def test_monitor_b_follows_runtime_settings_changes(tmp_path, monkeypatch):
    """apply_runtime_env 改 CHAT_PORT 之後，下一次組 profile 的 B 就跟著變（不用等 refresh）。"""
    monitor = _monitor(_layout(tmp_path))
    await monitor.ensure_ready()
    monkeypatch.setattr(settings, "CHAT_PORT", "3997")
    assert 3997 in monitor.block_ports("executor")
    monkeypatch.setattr(settings, "CHAT_PORT", "3996")
    ports = monitor.block_ports("executor")
    assert 3996 in ports and 3997 not in ports


async def test_consented_landlock_fallback_with_incomplete_b_sends_net_strict_false(tmp_path):
    """讀不到設定時核准的本機 Landlock：features 不含 tcp，所以 spec 的 net_strict 是 false."""
    layout = _layout(tmp_path)
    (layout.config_dir / "connections.json").write_text("{broken", encoding="utf-8")
    monitor = _monitor(layout)
    assert (await monitor.ensure_ready()).reason == "block_ports_incomplete"
    broker = FixedBroker(
        RunnerBroker(monitor=monitor, layout_provider=lambda: layout, interpreter=fixed_interpreter)
    )
    ports = monitor.block_ports("proxy")
    profile = SandboxProfile(name="agent_command", cwd=tmp_path, rules=(), block_ports=ports)
    consent = ExecutionGrant("none", Consent(subject="s", fallback="landlock"))
    runner = await broker.acquire(consent, profile)
    plan = runner.plan(ExecRequest(argv=("true",), env={}, profile=profile))
    try:
        chunks = []
        while chunk := os.read(plan.pass_fds[0], 65536):
            chunks.append(chunk)
    finally:
        plan.release()
    spec = json.loads(b"".join(chunks))
    assert (plan.level, "tcp" in plan.features) == ("landlock", False)
    assert (spec["net_strict"], spec["block_ports"]) == (False, list(ports))
