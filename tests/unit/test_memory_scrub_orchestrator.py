"""清除連備份：接上重置流程後的整體行為（O 組，對照設計 §1-2～§1-4、§6、§11）.

事實庫、升級備份與快照目錄都在 tmp_path；備份腳本換成替身（只在沙盒快照目錄寫一份 zip）。
重置只留記憶這一個 store，讓結果聚焦在清除本身。只用合成 ID。
"""

from __future__ import annotations

import asyncio
import os
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from memory_server import factual as fm
from memory_server.services.manager import MemoryManager
from memory_server.utils import collection_names as cn
from src import config as config_module
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.agent_reset import AgentResetResponse, LastResetStatus
from src.models.identity import AgentIdentity
from src.models.memory_scrub import MemoryScrubReport, ScrubCategoryResult, ScrubItemResult
from src.models.system_restore import RestoreStatus
from src.routers import system as system_router
from src.routers.auth import get_auth_service, get_current_user
from src.services import destructive_ops, memory_collection_guard, memory_scrub, system_backup
from src.services.agent_reset import last_result as L
from src.services.agent_reset import orchestrator as O
from src.services.agent_reset import primitives as P
from src.services.identity_router import get_verified_identity
from src.services.memory_scrub import orchestrator as SO
from src.services.memory_scrub import snapshots
from src.services.resource_manager import resource_manager
from src.utils import soul_io

from tests.helpers.memory_scrub import (
    OTHER,
    TARGET,
    SandboxBreach,
    assert_no_breach,
    fake_links,
    install_scrub_sandbox,
    make_db,
    rows,
    sha256,
    take_breaches,
    wait_scrub_idle,
    zip_tree,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

ADMIN = AgentIdentity.for_role(agent_id="zz-admin-o", role="admin", is_human=True)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    assert wait_scrub_idle(0) == [], "a previous test left the scrub flag or upgrade lock held"
    backups = install_scrub_sandbox(monkeypatch, tmp_path)
    # 測試順序隨機：每個測試用自己的備份鎖（會等鎖的測試會把鎖綁在自己的事件迴圈上）與乾淨的還原狀態
    monkeypatch.setattr(system_backup, "backup_execution_lock", asyncio.Lock())
    monkeypatch.setattr(system_router, "_last_restore", RestoreStatus(state="idle"))
    monkeypatch.setattr(L, "_last", LastResetStatus(state="idle"))
    vault = tmp_path / "vault"
    store = fm.FactualStore(base_dir=vault)
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", store)
    monkeypatch.setattr(soul_io, "list_soul_ids", lambda: [TARGET])
    monkeypatch.setattr(resource_manager, "is_owner", lambda *_a: True)
    monkeypatch.setattr(
        O, "ALL_STORES", (P.StorePurger("memory", P.purge_memory, P.remaining_memory),)
    )
    store.save_fact(AgentIdentity(agent_id=TARGET), "target memory")
    store.save_fact(AgentIdentity(agent_id=OTHER), "other memory")
    upgrade = make_db(
        store.upgrade_backup_dir / "core_memory.v0-to-v1.1700000001.db",
        [("fact_up_t", TARGET, "old target"), ("fact_up_o", OTHER, "old other")],
        schema="v0",
    )
    data = tmp_path / "snap-data"
    make_db(
        data / "vault" / "core_memory.db",
        [("fact_s_t", TARGET, "snap target"), ("fact_s_o", OTHER, "snap other")],
    )
    old_snapshot = zip_tree(data, backups / "Touka_Backup_old.zip")
    made: list[str] = []

    def fake_backup(*, auto: bool = False) -> dict[str, object]:
        """替身快照：照實打包當下的線上 vault（重置前做，所以含目標的記憶）."""
        name = f"Touka_Backup_reset{len(made)}.zip"
        with zipfile.ZipFile(backups / name, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(
                p for p in vault.rglob("*") if p.is_file() and ".memscrub-" not in p.name
            ):
                zf.write(path, f"data/vault/{path.relative_to(vault).as_posix()}")
        made.append(name)
        return {"status": "success", "message": "ok", "filename": name}

    monkeypatch.setattr(system_backup, "create_backup", fake_backup)
    yield SimpleNamespace(
        backups=backups,
        store=store,
        upgrade=upgrade,
        old_snapshot=old_snapshot,
        made=made,
        data=data,
    )
    assert wait_scrub_idle() == [], "a scrub worker was still running after the test"
    assert_no_breach()


REPLY_TIMEOUT_S = 60  # 每個等待都有上限：卡住的清除讓測試失敗，不會把整套卡死


async def _reset(**kwargs: object) -> O.ResetReport:
    return await asyncio.wait_for(
        O.reset_agent_data(TARGET, ADMIN, scrub=True, **kwargs), REPLY_TIMEOUT_S
    )


async def _until_entered(entered: threading.Event, task: asyncio.Task) -> None:
    """等清除走到替身那一步；重置先結束（例如出錯）或等太久就讓測試失敗，不會卡住."""
    for _ in range(1000):
        if entered.is_set():
            return
        if task.done():
            await task
            pytest.fail("the reset finished before the scrub reached the stub")
        await asyncio.sleep(0.01)
    pytest.fail("the scrub never reached the stub")


async def test_o01_scrub_runs_only_after_a_clean_reset(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def dirty(*_a: object) -> int:
        return 1

    monkeypatch.setattr(O, "ALL_STORES", (P.StorePurger("memory", P.purge_memory, dirty),))
    digests = {p.name: sha256(p) for p in (env.upgrade, env.old_snapshot)}
    report = await _reset()
    assert report.clean is False
    assert (report.scrub.ran, report.scrub.reason) == (False, "reset_not_clean")
    assert {p.name: sha256(p) for p in (env.upgrade, env.old_snapshot)} == digests
    assert "AUDIT: memory-scrub result" in caplog.text and "reason=reset_not_clean" in caplog.text
    assert not memory_scrub.in_progress()


async def test_o02_scrub_runs_in_a_worker_thread_under_the_backup_lock(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    seen: dict[str, object] = {}
    real = fm.FactualStore.compact_after_reset

    def spy(self: fm.FactualStore, identity: AgentIdentity) -> bool:
        seen["locked"] = system_backup.backup_execution_lock.locked()
        seen["thread"] = threading.get_ident()
        return real(self, identity)

    monkeypatch.setattr(fm.FactualStore, "compact_after_reset", spy)
    report = await _reset()
    assert report.scrub.complete, report.scrub
    assert seen["locked"] is True
    assert seen["thread"] != loop_thread


async def test_o03_scheduled_backup_waits_for_the_scrub(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    real = fm.FactualStore.compact_after_reset

    def slow(self: fm.FactualStore, identity: AgentIdentity) -> bool:
        entered.set()
        release.wait(10)
        return real(self, identity)

    monkeypatch.setattr(fm.FactualStore, "compact_after_reset", slow)
    reset = asyncio.create_task(_reset())
    await _until_entered(entered, reset)
    order: list[str] = []
    scheduled = asyncio.create_task(system_backup.run_backup_job(lambda: order.append("scheduled")))
    await asyncio.sleep(0.2)
    assert order == []
    release.set()
    report = await asyncio.wait_for(reset, REPLY_TIMEOUT_S)
    await asyncio.wait_for(scheduled, REPLY_TIMEOUT_S)
    assert order == ["scheduled"]
    assert report.scrub.ran


async def test_o04_preflight_refuses_before_any_backup(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    backups_called: list[int] = []
    monkeypatch.setattr(system_backup, "create_backup", lambda **_k: backups_called.append(1))
    monkeypatch.setattr(soul_io, "list_soul_ids", lambda: [TARGET, TARGET.replace("-", "_")])
    with pytest.raises(ToukaException) as exc:
        await _reset()
    assert exc.value.code is ErrorCode.MEMORY_SCRUB_COLLECTION_SHARED
    monkeypatch.setattr(soul_io, "list_soul_ids", lambda: [TARGET])
    monkeypatch.setattr(fm, "memory_scrub_supported", lambda: False)
    with pytest.raises(ToukaException) as exc:
        await _reset()
    assert exc.value.code is ErrorCode.MEMORY_SCRUB_UNAVAILABLE
    assert backups_called == []
    assert env.store.count_agent_facts(AgentIdentity(agent_id=TARGET)) == 1
    assert caplog.text.count("AUDIT: memory-scrub rejected") == 2
    assert not memory_scrub.in_progress()


async def test_o05_store_built_before_any_lock_does_not_deadlock(
    env: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "fresh" / "vault"
    make_db(vault / "core_memory.db", [("fact_v0", TARGET, "v0 target")], schema="v0")
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", None)
    fm.FactualStore.reset_shared()
    monkeypatch.setattr(fm, "_default_vault_dir", lambda: vault)
    try:
        report = await asyncio.wait_for(_reset(), timeout=30)
    finally:
        fm.FactualStore.reset_shared()
    upgrade_items = [i for i in report.scrub.items if i.kind == "upgrade_backup"]
    assert upgrade_items and all(i.status in ("scrubbed", "not_needed") for i in upgrade_items)
    assert report.scrub.complete, report.scrub


async def test_o06_memory_written_after_the_reset(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = SO.run_memory_scrub

    def heartbeat_writes_first(ctx: object, progress: SO.ScrubProgress) -> MemoryScrubReport:
        env.store.save_fact(AgentIdentity(agent_id=TARGET), "written after reset")  # 例如心跳還在跑
        return real(ctx, progress)

    monkeypatch.setattr(SO, "run_memory_scrub", heartbeat_writes_first)
    report = await _reset()
    live = next(i for i in report.scrub.items if i.kind == "live_store")
    assert (live.status, live.reason) == ("failed", "rewritten_after_reset")
    assert report.scrub.memory_remaining and report.scrub.memory_remaining > 0
    assert report.scrub.complete is False


async def test_o07_audit_has_counts_names_and_codes_only(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def locked(*_a: object, **_k: object) -> None:
        raise PermissionError(13, "in use")

    monkeypatch.setattr(resource_manager, "move_file", locked)
    report = await _reset()
    line = next(
        r.getMessage() for r in caplog.records if "AUDIT: memory-scrub result" in r.getMessage()
    )
    assert f"actor='{ADMIN.agent_id}'" in line and f"target='{TARGET}'" in line
    assert "complete=False" in line
    assert "'Touka_Backup_old.zip'=in_use" in line
    assert "snap target" not in caplog.text and "old target" not in caplog.text
    assert str(env.backups) not in line
    assert report.scrub.complete is False


@pytest.mark.parametrize(
    ("categories", "items", "own", "remaining", "internal", "recheck", "complete", "reason"),
    [
        ("done", [("snapshot", "own.zip", "scrubbed")], "own.zip", 0, False, False, True, None),
        ("done", [("snapshot", "own.zip", "not_needed")], "own.zip", 0, False, False, True, None),
        (
            "done",
            [("snapshot", "own.zip", "scrubbed"), ("snapshot", "b.zip", "failed")],
            "own.zip",
            0,
            False,
            False,
            False,
            None,
        ),
        (
            "upgrade_backups",
            [("snapshot", "own.zip", "scrubbed")],
            "own.zip",
            0,
            False,
            False,
            False,
            "lock_timeout",
        ),
        ("done", [], "own.zip", 0, False, False, False, "own_snapshot_missing"),
        (
            "done",
            [("snapshot", "own.zip", "scrubbed")],
            None,
            0,
            False,
            False,
            False,
            "own_snapshot_missing",
        ),
        (
            "done",
            [("snapshot", "own.zip", "scrubbed")],
            "own.zip",
            None,
            False,
            True,
            False,
            "recheck_failed",
        ),
        ("done", [("snapshot", "own.zip", "scrubbed")], "own.zip", 2, False, False, False, None),
        (
            "done",
            [("snapshot", "own.zip", "scrubbed")],
            "own.zip",
            0,
            True,
            False,
            False,
            "internal_error",
        ),
    ],
)
def test_o08_complete_table(
    categories, items, own, remaining, internal, recheck, complete, reason
) -> None:
    cats = [
        ScrubCategoryResult(
            name=n,
            state="failed" if n == categories else "done",
            reason="lock_timeout" if n == categories else None,
        )
        for n in ("leftovers", "live", "upgrade_backups", "snapshots")
    ]
    built = MemoryScrubReport.build(
        categories=cats,
        items=[ScrubItemResult(kind=k, name=n, status=s) for k, n, s in items],
        own_snapshot=own,
        memory_remaining=remaining,
        internal_error=internal,
        recheck_failed=recheck,
    )
    assert (built.complete, built.reason) == (complete, reason)


async def test_o09_cancelled_request_still_finishes_and_audits(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    entered, release = threading.Event(), threading.Event()
    real = snapshots.scrub_snapshot

    def slow(path: Path, identity: AgentIdentity, known: set[str]):
        entered.set()
        release.wait(10)
        return real(path, identity, known)

    monkeypatch.setattr(snapshots, "scrub_snapshot", slow)
    task = asyncio.create_task(_reset())
    await _until_entered(entered, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert system_backup.backup_execution_lock.locked()
    assert memory_scrub.in_progress()
    # 丙-4：請求取消了，清除還在跑，心跳照樣擋
    assert destructive_ops.is_held(AgentIdentity(agent_id=TARGET))
    with pytest.raises(ToukaException) as exc:
        await asyncio.wait_for(O.reset_agent_data(TARGET, ADMIN), REPLY_TIMEOUT_S)
    assert exc.value.code is ErrorCode.AGENT_RESET_IN_PROGRESS
    release.set()
    for _ in range(500):
        if not system_backup.backup_execution_lock.locked() and not memory_scrub.in_progress():
            break
        await asyncio.sleep(0.02)
    assert not memory_scrub.in_progress()
    assert not destructive_ops.is_held(AgentIdentity(agent_id=TARGET))  # 工作結束時自己放行
    assert "AUDIT: memory-scrub result" in caplog.text
    assert [p.name for p in env.backups.iterdir() if ".memscrub-" in p.name] == []


async def test_o10_rerun_and_empty_agent(env: SimpleNamespace) -> None:
    first = await _reset()
    assert first.scrub.complete, first.scrub
    second = await _reset()
    assert second.scrub.complete, second.scrub
    assert {i.status for i in second.scrub.items if i.kind in ("upgrade_backup", "snapshot")} == {
        "not_needed"
    }
    assert rows(env.upgrade, "SELECT COUNT(*) FROM facts WHERE agent_id = 'zz-scrub-a'") == [(0,)]


def test_o11_path_sources_are_fenced(env: SimpleNamespace) -> None:
    for source in (
        config_module.resolve_env_file_path,
        config_module.get_workspace_root,
        system_backup.resolve_env_file_path,
    ):
        with pytest.raises(SandboxBreach):
            source()
    assert len(take_breaches()) == 3
    assert system_backup.backup_dir() == env.backups


def test_o11c_a_breach_swallowed_by_the_product_still_fails_the_test(env: SimpleNamespace) -> None:
    """產品的類別邊界會吞掉一般例外；違規不是一般例外，吞不掉，也一定記下來."""
    with pytest.raises(SandboxBreach):
        SO._attempt(config_module.resolve_env_file_path)
    with pytest.raises(SandboxBreach):
        SO._attempt(lambda: system_backup.list_snapshot_files(Path(config_module.WORKSPACE_ROOT)))
    assert len(take_breaches()) == 2


def test_o11b_a_scrub_that_derives_its_own_directory_is_stopped(env: SimpleNamespace) -> None:
    real_backups = Path(config_module.WORKSPACE_ROOT) / "backups"
    with pytest.raises(SandboxBreach):
        snapshots.scrub_snapshots(AgentIdentity(agent_id=TARGET), real_backups, set())
    assert len(take_breaches()) == 1


async def test_o12b_failure_while_preparing_the_scrub_still_reports(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> Path:
        raise RuntimeError("cannot resolve the snapshot folder")

    monkeypatch.setattr(system_backup, "backup_dir", broken)
    report = await _reset()
    assert report.clean is True
    assert (report.scrub.ran, report.scrub.complete, report.scrub.reason) == (
        True,
        False,
        "internal_error",
    )
    assert not memory_scrub.in_progress()
    assert "AUDIT: memory-scrub result" in caplog.text


def test_o12_unexpected_failure_still_returns_200_with_a_report(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(ctx: object, progress: SO.ScrubProgress) -> MemoryScrubReport:
        progress.category(
            "leftovers", lambda: [ScrubItemResult(kind="leftover", name="x", status="removed")]
        )
        raise RuntimeError("unexpected")

    monkeypatch.setattr(SO, "run_memory_scrub", explode)
    auth = Mock()
    auth.verify_password.return_value = True
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        role="admin", hashed_password="h", id=ADMIN.agent_id
    )
    app.dependency_overrides[get_auth_service] = lambda: auth
    body = {
        "confirm": True,
        "confirm_agent_id": TARGET,
        "current_password": "pw",
        "scrub_backups": True,
        "scrub_acknowledged": True,
    }
    try:
        client = TestClient(app, raise_server_exceptions=False)
        with ThreadPoolExecutor(1) as pool:  # 同步的 TestClient 也要有上限
            response = pool.submit(
                client.post, f"/v1/admin/agents/{TARGET}/reset", json=body
            ).result(timeout=REPLY_TIMEOUT_S)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["clean"] is True
    assert (body["scrub"]["ran"], body["scrub"]["complete"], body["scrub"]["reason"]) == (
        True,
        False,
        "internal_error",
    )
    assert [i["name"] for i in body["scrub"]["items"]] == ["x"]
    assert "AUDIT: memory-scrub result" in caplog.text
    assert not memory_scrub.in_progress()


async def test_o13_upgrade_lock_timeout_is_a_category_failure(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fm, "FACTS_SCHEMA_LOCK_TIMEOUT_S", 0.1)
    acquired, release = threading.Event(), threading.Event()

    def hold() -> None:
        with fm._UPGRADE_BACKUP_LOCK:
            acquired.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert acquired.wait(5)
    try:
        report = await _reset()
    finally:
        release.set()
        holder.join(5)
    category = next(c for c in report.scrub.categories if c.name == "upgrade_backups")
    assert (category.state, category.reason) == ("failed", "lock_timeout")
    assert any(i.kind == "category" and i.name == "upgrade_backups" for i in report.scrub.items)
    assert report.scrub.complete is False


async def test_o14_snapshot_listing_failure(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(directory: object = None, **_kwargs: object) -> list:
        raise OSError(5, "listing failed")

    monkeypatch.setattr(system_backup, "list_snapshot_files", broken)
    report = await _reset()
    category = next(c for c in report.scrub.categories if c.name == "snapshots")
    assert (category.state, category.reason) == ("failed", "list_failed")
    assert (report.scrub.complete, report.scrub.reason) == (False, "list_failed")
    assert any(
        (i.kind, i.name, i.status, i.reason) == ("category", "snapshots", "failed", "list_failed")
        for i in report.scrub.items
    )


async def test_o15_recheck_failure_is_unknown(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(identity: AgentIdentity) -> int:
        raise RuntimeError("memory bridge down")

    monkeypatch.setattr(P, "count_remaining_memory", broken)

    async def fine(*_a: object) -> int:
        return 0

    monkeypatch.setattr(O, "ALL_STORES", (P.StorePurger("memory", P.purge_memory, fine),))
    report = await _reset()
    assert report.scrub.memory_remaining is None
    assert (report.scrub.complete, report.scrub.reason) == (False, "recheck_failed")


async def test_o16_own_snapshot_must_be_listed(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        system_backup,
        "create_backup",
        lambda **_k: {"status": "success", "message": "ok", "filename": None},
    )
    report = await _reset()
    assert (report.scrub.complete, report.scrub.reason) == (False, "own_snapshot_missing")


async def test_retained_list_drops_upgrade_backups_when_scrubbed(env: SimpleNamespace) -> None:
    report = await _reset()
    names = [r["name"] for r in report.to_dict()["retained"]]
    assert fm.FACTS_BACKUP_DIR_NAME not in names
    plain = await asyncio.wait_for(O.reset_agent_data(TARGET, ADMIN), REPLY_TIMEOUT_S)
    assert fm.FACTS_BACKUP_DIR_NAME in [r["name"] for r in plain.to_dict()["retained"]]
    assert os.path.exists(env.upgrade)


# --- 成果審修正：每種結局都有稽核（R2）、預檢不卡事件迴圈（R25） -----------------------


def _results(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    """結果稽核行的（ran, reason）."""
    found = []
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith("AUDIT: memory-scrub result"):
            fields = dict(part.split("=", 1) for part in message.split() if "=" in part)
            found.append((fields["ran"], fields["reason"]))
    return found


def _rejected(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage().rsplit("reason=", 1)[1]
        for record in caplog.records
        if record.getMessage().startswith("AUDIT: memory-scrub rejected")
    ]


async def test_o17_backup_failure_with_scrub_is_audited(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def failing(*, auto: bool = False) -> dict:
        raise ToukaException(ErrorCode.SYSTEM_BACKUP_FAILED)

    monkeypatch.setattr(system_backup, "create_backup", failing)
    with pytest.raises(ToukaException) as exc:
        await _reset()
    assert exc.value.code is ErrorCode.SYSTEM_BACKUP_FAILED
    assert _results(caplog) == [("False", "backup_failed")]
    assert not memory_scrub.in_progress()


async def test_o18_cancel_before_handover_is_a_result_line(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    entered, release = threading.Event(), threading.Event()
    real = system_backup.create_backup

    def slow(*, auto: bool = False) -> dict:
        entered.set()
        release.wait(10)
        return real()

    monkeypatch.setattr(system_backup, "create_backup", slow)
    task = asyncio.create_task(_reset())
    await _until_entered(entered, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert _results(caplog) == [("False", "cancelled")]
    assert _rejected(caplog) == []
    assert not memory_scrub.in_progress()


async def test_o19_unexpected_preparation_error_is_audited(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(store: object) -> set[str]:
        raise TypeError

    monkeypatch.setattr(memory_collection_guard, "known_memory_ids", broken)
    with pytest.raises(TypeError):
        await _reset()
    assert _rejected(caplog) == ["internal_error"]
    assert env.made == []
    assert not memory_scrub.in_progress()


async def test_o20_preflight_reads_the_store_off_the_event_loop(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread, seen = threading.get_ident(), []
    real = O._scrub_dependencies

    def spy() -> object:
        seen.append(threading.get_ident())
        return real()

    monkeypatch.setattr(O, "_scrub_dependencies", spy)
    await _reset()
    assert seen and seen[0] != loop_thread


# --- 自己那份快照要真的清掉東西（R5）、線上 Qdrant 資料夾要真的不見（R6） ---------------


@pytest.mark.parametrize(
    ("status", "facts", "collection", "removed", "complete", "reason"),
    [
        ("not_needed", 0, False, 3, False, "own_snapshot_unverified"),
        ("scrubbed", 0, False, 3, False, "own_snapshot_unverified"),
        ("scrubbed", 2, False, 3, True, None),
        ("scrubbed", 0, True, 3, True, None),
        ("not_needed", 0, False, 0, True, None),
    ],
)
def test_o08b_own_snapshot_must_have_lost_memory(
    status: str, facts: int, collection: bool, removed: int, complete: bool, reason: str | None
) -> None:
    built = MemoryScrubReport.build(
        categories=[
            ScrubCategoryResult(name=n, state="done")
            for n in ("leftovers", "live", "upgrade_backups", "snapshots")
        ],
        items=[
            ScrubItemResult(
                kind="snapshot",
                name="own.zip",
                status=status,
                facts_removed=facts,
                collection_removed=collection,
            )
        ],
        own_snapshot="own.zip",
        memory_remaining=0,
        internal_error=False,
        recheck_failed=False,
        reset_removed=removed,
    )
    assert (built.complete, built.reason) == (complete, reason)


async def test_o21_own_snapshot_that_finds_nothing_is_not_complete(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = snapshots._classify

    def blind(infos: list, identity: AgentIdentity) -> object:
        return real(infos, identity)._replace(
            databases=(), target_members=frozenset(), temp_members=frozenset(), meta=None
        )

    monkeypatch.setattr(snapshots, "_classify", blind)
    report = await _reset()
    assert report.clean is True
    assert (report.scrub.complete, report.scrub.reason) == (False, "own_snapshot_unverified")


@pytest.mark.parametrize("folder", ["memory_zz_scrub_a", "MEMORY_ZZ_SCRUB_A"])
async def test_o22_collection_folder_left_on_disk_fails_the_live_check(
    env: SimpleNamespace, folder: str
) -> None:
    leftover = env.store.base_dir / cn.QDRANT_DIR_NAME / cn.QDRANT_COLLECTION_DIR / folder
    leftover.mkdir(parents=True)
    (leftover / "storage.sqlite").write_bytes(b"old points")
    report = await _reset()
    assert report.scrub.complete is False
    assert [
        (i.name, i.reason)
        for i in report.scrub.items
        if i.kind == "live_store" and i.status == "failed"
    ] == [(folder, "live_verify_failed")]


def test_o07b_file_names_cannot_forge_an_audit_line(caplog: pytest.LogCaptureFixture) -> None:
    forged = "x.zip\nAUDIT: memory-scrub result complete=True"
    report = MemoryScrubReport.build(
        categories=[
            ScrubCategoryResult(name=n, state="done")
            for n in ("leftovers", "live", "upgrade_backups", "snapshots")
        ],
        items=[
            ScrubItemResult(kind="snapshot", name=forged, status="failed", reason="in_use"),
            ScrubItemResult(
                kind="info", name=forged, status="info", reason="out_of_scope", count=1
            ),
        ],
        own_snapshot=forged,
        memory_remaining=0,
        internal_error=False,
        recheck_failed=False,
    )
    SO.audit_result(report, "zz-admin-o", TARGET)
    (record,) = [
        r for r in caplog.records if r.getMessage().startswith("AUDIT: memory-scrub result")
    ]
    assert "\n" not in record.getMessage()


async def test_o22b_linked_collection_folder_fails_the_live_check(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = env.store.base_dir / cn.QDRANT_DIR_NAME / cn.QDRANT_COLLECTION_DIR
    root.mkdir(parents=True, exist_ok=True)
    fake_links(monkeypatch, root)  # 是連結或 junction：不跟進去，也不能當成沒有殘留
    report = await _reset()
    assert report.scrub.complete is False
    assert [
        (i.name, i.reason)
        for i in report.scrub.items
        if i.kind == "live_store" and i.status == "failed"
    ] == [(cn.QDRANT_COLLECTION_DIR, "live_verify_failed")]


# ── 最後一次重置的結果（舊審查報告丙-5，ADR-0289）────────────────────────────


def _states_at_end(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """每次清旗標時，最後結果是什麼狀態（要在旗標放開前就記好，下一次重置才蓋不掉它）."""
    seen: list[str] = []
    real_end = memory_scrub.ScrubSession.end

    def end(self: memory_scrub.ScrubSession) -> None:
        seen.append(L.last_reset().state)
        real_end(self)

    monkeypatch.setattr(memory_scrub.ScrubSession, "end", end)
    return seen


async def test_o23_cancelled_request_keeps_running_until_the_scrub_records_its_result(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _states_at_end(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    real = snapshots.scrub_snapshot

    def slow(path: Path, identity: AgentIdentity, known: set[str]):
        entered.set()
        release.wait(10)
        return real(path, identity, known)

    monkeypatch.setattr(snapshots, "scrub_snapshot", slow)
    task = asyncio.create_task(_reset())
    await _until_entered(entered, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert L.last_reset().state == "running"  # 清除還在跑：不是中斷
    release.set()
    assert wait_scrub_idle() == []
    assert seen[:1] == ["done"]
    last = L.last_reset()
    assert (last.state, last.agent_id) == ("done", TARGET)
    assert last.report.clean is True
    assert last.report.scrub.ran is True
    assert last.report.scrub.complete is True, last.report.scrub
    assert last.started_at <= last.finished_at


async def test_o24_finished_scrub_is_kept_with_the_returned_report(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _states_at_end(monkeypatch)
    report = await _reset()
    assert seen[:1] == ["done"]
    last = L.last_reset()
    assert last.state == "done"
    assert last.report == AgentResetResponse.model_validate(report.to_dict())
    assert last.report.scrub.complete is True


async def test_o25_cancel_before_handover_is_interrupted(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    real = system_backup.create_backup

    def slow(*, auto: bool = False) -> dict:
        entered.set()
        release.wait(10)
        return real()

    monkeypatch.setattr(system_backup, "create_backup", slow)
    task = asyncio.create_task(_reset())
    await _until_entered(entered, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    last = L.last_reset()
    assert (last.state, last.agent_id, last.report) == ("interrupted", TARGET, None)


async def test_o26_scrub_safety_net_report_is_the_kept_report(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> Path:
        raise RuntimeError("cannot resolve the snapshot folder")

    monkeypatch.setattr(system_backup, "backup_dir", broken)
    seen = _states_at_end(monkeypatch)
    report = await _reset()
    assert seen[:1] == ["done"]
    last = L.last_reset()
    assert last.report == AgentResetResponse.model_validate(report.to_dict())
    assert last.report.scrub.reason == "internal_error"
