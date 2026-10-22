"""權限自癒（iso-c2 U2n）：裝飾器 ``utils/permission_repair.py``、服務 ``isolation/repair.py``.

代理人和 proxy 同 uid、Landlock 不管 chmod，所以代理人能把自己碰得到的工作區改成讀不到。自癒只在
白名單內補 proxy 自己擁有的 inode 的擁有者位元；Touka 自己的狀態一律不修，PermissionError 原樣
往上丟（同一個例外物件）。本檔是純函式測試（假掛鉤、假 kernel、注入的版面），Windows 也能跑；
真核心的案例在 ``tests/integration/test_isolation_linux.py``。
"""

from __future__ import annotations

import errno
import logging
import shutil
import stat
import sys
import threading
import time
import types
from dataclasses import replace
from pathlib import Path

import pytest
from memory_server.services.personal_catalog import CatalogPathDeniedError
from src.sandbox_kernel import owner_repair
from src.sandbox_kernel.owner_repair import RepairOutcome
from src.sandbox_kernel.spec import SelfTestResult
from src.services.isolation import repair, status
from src.services.isolation.profiles import SandboxLayout
from src.services.isolation.repair import PermissionRepairService
from src.services.isolation.status import IsolationMonitor
from src.services.resource_manager import resource_manager
from src.utils import permission_repair as pr
from src.utils.permission_repair import (
    permission_repair,
    permission_repair_exempt,
    set_permission_repair,
)

EACCES_PATH = "/data/personal_drive/Alice/notes.txt"


class FakeHook:
    """記錄呼叫的假掛鉤；``result`` 是 ``repair`` 的回傳值，``error`` 有值時改成丟出它。"""

    def __init__(self, result: bool = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.retries: list[tuple[str, bool]] = []
        self.retry_flags: list[bool] = []

    def repair(self, method: str, paths: tuple[object, ...], *, retry: bool) -> bool:
        self.calls.append((method, paths))
        self.retry_flags.append(retry)
        if self.error is not None:
            raise self.error
        return self.result

    def note_retry(self, method: str, succeeded: bool) -> None:
        self.retries.append((method, succeeded))


def _eacces(path: str = EACCES_PATH) -> PermissionError:
    return PermissionError(errno.EACCES, "Permission denied", path)


class Flaky:
    """前 ``failures`` 次丟出 ``error``（同一個物件），之後回 ``"ok"``。"""

    def __init__(self, failures: int, error: BaseException | None = None) -> None:
        self.failures = failures
        self.error = error or _eacces()
        self.runs = 0

    def __call__(self) -> str:
        self.runs += 1
        if self.runs <= self.failures:
            raise self.error
        return "ok"


@pytest.fixture
def on_linux(monkeypatch):
    """只把裝飾器模組看到的平台換成 linux（不動全域 ``sys.platform``）。"""
    monkeypatch.setattr(pr, "sys", types.SimpleNamespace(platform="linux"))


def _decorated(body: Flaky, *, retry: bool):
    @permission_repair(retry=retry)
    def read_something() -> str:
        return body()

    return read_something


# --- 1～3、17：修完要不要重試 ---


def test_retry_true_repairs_then_retries_exactly_once(on_linux):
    hook, body = FakeHook(), Flaky(failures=1)
    set_permission_repair(hook)
    assert _decorated(body, retry=True)() == "ok"
    assert body.runs == 2
    assert hook.calls == [("read_something", (EACCES_PATH, None))]
    assert hook.retries == [("read_something", True)]


def test_retry_false_repairs_and_raises_the_same_error_object(on_linux):
    hook, body = FakeHook(), Flaky(failures=1)
    set_permission_repair(hook)
    with pytest.raises(PermissionError) as caught:
        _decorated(body, retry=False)()
    assert caught.value is body.error
    assert body.runs == 1 and len(hook.calls) == 1 and hook.retries == []
    assert hook.retry_flags == [False]


def test_a_failed_retry_is_raised_and_there_is_no_third_attempt(on_linux):
    hook, body = FakeHook(), Flaky(failures=5)
    set_permission_repair(hook)
    with pytest.raises(PermissionError):
        _decorated(body, retry=True)()
    assert body.runs == 2 and len(hook.calls) == 1
    assert hook.retries == [("read_something", False)]


def test_nothing_changed_means_no_retry(on_linux):
    """kernel 回「沒有改動」（例如 LSM 擋的 EACCES）時掛鉤回 False：不重試、原例外往上丟。"""
    hook, body = FakeHook(result=False), Flaky(failures=1)
    set_permission_repair(hook)
    with pytest.raises(PermissionError) as caught:
        _decorated(body, retry=True)()
    assert caught.value is body.error and body.runs == 1 and hook.retries == []


# --- 4、13：只處理 OS 層的 EACCES；政策拒絕的 errno 是 None ---


def _policy_refusal_with_filename() -> PermissionError:
    """errno 是 None、但 filename 有值：只靠 errno 那一層擋。"""
    error = PermissionError("Soul Protection Error: Cannot write to a protected path.")
    error.filename = EACCES_PATH
    return error


@pytest.mark.parametrize(
    "error",
    [
        PermissionError("Soul Protection Error: Cannot write to a protected path."),
        _policy_refusal_with_filename(),
        PermissionError(errno.EPERM, "Operation not permitted", EACCES_PATH),
        PermissionError(errno.EROFS, "Read-only file system", EACCES_PATH),
        PermissionError(errno.EACCES, "Permission denied"),
        FileNotFoundError(errno.ENOENT, "No such file", EACCES_PATH),
    ],
    ids=["errno-none", "errno-none-filename", "eperm", "erofs", "no-filename", "not-eacces-type"],
)
def test_only_eacces_with_a_filename_reaches_the_hook(on_linux, error):
    hook, body = FakeHook(), Flaky(failures=1, error=error)
    set_permission_repair(hook)
    with pytest.raises(type(error)) as caught:
        _decorated(body, retry=True)()
    assert caught.value is error
    assert hook.calls == [] and body.runs == 1


def test_policy_refusals_never_reach_the_hook_and_keep_errno_none(on_linux, tmp_path):
    """Soul Protection Error 與個人空間目錄的 ``CatalogPathDeniedError``.

    掛鉤沒被呼叫、權限不變、不重試。
    """
    target = tmp_path / "soul_file.txt"
    target.write_text("x", encoding="utf-8")
    target.chmod(stat.S_IREAD)
    before = target.stat().st_mode
    soul = PermissionError("Soul Protection Error: Cannot write to a protected path.")
    for refusal in (soul, CatalogPathDeniedError()):
        hook, body = FakeHook(), Flaky(failures=1, error=refusal)
        set_permission_repair(hook)
        with pytest.raises(PermissionError) as caught:
            _decorated(body, retry=True)()
        assert caught.value is refusal and caught.value.errno is None
        assert hook.calls == [] and body.runs == 1
    assert target.stat().st_mode == before
    target.chmod(stat.S_IREAD | stat.S_IWRITE)


# --- 5、15：只在 Linux ---


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_non_linux_platforms_never_reach_the_hook(monkeypatch, platform):
    monkeypatch.setattr(pr, "sys", types.SimpleNamespace(platform=platform))
    hook, body = FakeHook(), Flaky(failures=1)
    set_permission_repair(hook)
    with pytest.raises(PermissionError):
        _decorated(body, retry=True)()
    assert hook.calls == [] and body.runs == 1


# --- 6：巢狀只由最外層處理 ---


class _Nested:
    def __init__(self, body: Flaky) -> None:
        self.body = body

    @permission_repair(retry=True)
    def outer(self) -> str:
        return self.inner()

    @permission_repair(retry=True)
    def inner(self) -> str:
        return self.body()


def test_nested_decorated_calls_are_handled_only_by_the_outermost(on_linux):
    """內層失敗兩次：最外層只修一次、只重試一次（內層不各自重試），第二次照實往上丟。"""
    hook, body = FakeHook(), Flaky(failures=2)
    set_permission_repair(hook)
    with pytest.raises(PermissionError):
        _Nested(body).outer()
    assert len(hook.calls) == 1 and body.runs == 2
    assert hook.calls[0][0] == "outer"


def test_an_inner_call_outside_any_outer_call_is_handled_by_itself(on_linux):
    hook, body = FakeHook(), Flaky(failures=1)
    set_permission_repair(hook)
    assert _Nested(body).inner() == "ok"
    assert hook.calls[0][0] == "inner"


# --- 14：掛鉤出錯 ---


def test_a_failing_hook_keeps_the_original_permission_error(on_linux, caplog):
    hook, body = FakeHook(error=RuntimeError("boom")), Flaky(failures=1)
    set_permission_repair(hook)
    with pytest.raises(PermissionError) as caught:
        _decorated(body, retry=True)()
    assert caught.value is body.error
    assert type(caught.value) is PermissionError and caught.value.errno == errno.EACCES
    assert caught.value.__cause__ is None and body.runs == 1
    assert "boom" not in caplog.text


# --- 豁免只標記、不包裝 ---


def test_exempt_marks_the_function_without_wrapping_it():
    def move_file() -> None:
        raise _eacces()

    marked = permission_repair_exempt(category="swap_semantics", reason="swap")(move_file)
    assert marked is move_file
    assert marked.__permission_repair__ == ("exempt", "swap_semantics")


def test_exempt_function_never_reaches_the_hook(on_linux):
    hook = FakeHook()
    set_permission_repair(hook)

    @permission_repair_exempt(category="swap_semantics", reason="swap")
    def remove_file() -> None:
        raise _eacces()

    with pytest.raises(PermissionError):
        remove_file()
    assert hook.calls == []


# --- 11：conftest 每個測試前後把掛鉤重設成 None（兩個測試依檔案順序執行）---


def test_installing_a_hook_without_cleanup():
    set_permission_repair(FakeHook())
    assert pr._hook is not None


def test_the_next_test_starts_without_a_hook():
    assert pr._hook is None


# ======================================================================================
# 服務：白名單（W1～W9）、受保護路徑、頻率限制、計數與記錄（services/isolation/repair.py）
# ======================================================================================


def _layout(tmp_path: Path) -> SandboxLayout:
    base = tmp_path.resolve()
    return SandboxLayout(
        data_root=base / "data",
        config_dir=base / "config",
        install_root=base / "app",
        proxy_root=base / "app" / "ToukaProxy",
        env_file=base / "host_root" / ".env",
        state_root=base / "state",
        kernel_dir=None,
        executor_dir=None,
        mirror_roots=(),
        in_container=False,
    )


class FakeKernel:
    """假 ``repair_chain``：記錄 (root, target)，回傳設定好的結果。"""

    def __init__(self, outcome: RepairOutcome | None = None) -> None:
        self.outcome = outcome or RepairOutcome(changed=1)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, root: str, target: str) -> RepairOutcome:
        self.calls.append((root, target))
        return self.outcome


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class LockCheckingClock(Clock):
    """每次讀時間都讓出 GIL，並核對是在服務的鎖裡讀（計數與時間窗都在同一把鎖內改）。"""

    def __init__(self) -> None:
        super().__init__()
        self.service: PermissionRepairService | None = None
        self.unlocked = 0

    def __call__(self) -> float:
        time.sleep(0)
        if self.service is not None and not self.service._lock.locked():
            self.unlocked += 1
        return self.now


def _protected_in(layout: SandboxLayout):
    """和 ``is_protected_path`` 同規則：版面 data 根底下的 soul、legacy。"""
    roots = (layout.data_root / "soul", layout.data_root / "legacy")
    return lambda path: any(Path(path).resolve().is_relative_to(root) for root in roots)


def _service(tmp_path, kernel=None, clock=None, protected=None):
    layout = _layout(tmp_path)
    service = PermissionRepairService(
        lambda: layout,
        is_protected=protected or _protected_in(layout),
        repair_chain=kernel or FakeKernel(),
        clock=clock or Clock(),
    )
    return service, layout


def _repair(service, path: Path, method: str = "read_file_text") -> bool:
    return service.repair(method, (str(path), None), retry=True)


@pytest.mark.parametrize(
    ("relative", "root", "reserved"),
    [
        ("data/personal_drive/Alice/notes.txt", "data/personal_drive", False),
        ("data/personal_drive/Alice/.touka/catalog.json", "data/personal_drive", False),
        ("data/personal_drive/Alice", "data/personal_drive", False),
        ("data/personal_drive", "data/personal_drive", True),
        ("data/mcp_servers", "data/mcp_servers", True),
        ("data/mcp_servers/managed", "data/mcp_servers", True),
        ("data/mcp_servers/managed/web-search/server.py", "data/mcp_servers", False),
        ("data/custom", "data/custom", True),  # W8：加入定期檢查的根
        ("data/custom/gpt-x/drive", "data/custom", False),
        ("data/custom/gpt-x/drive/a/b.txt", "data/custom", False),
        ("state", "state", True),
        ("state/proxy", "state", True),
        ("state/proxy/.touka-selftest-1/x", "state", False),
    ],
)
def test_whitelisted_targets_walk_from_the_topmost_whitelisted_root(
    tmp_path, relative, root, reserved
):
    kernel = FakeKernel()
    service, layout = _service(tmp_path, kernel=kernel)
    base = tmp_path.resolve()
    target = base / relative
    assert _repair(service, target) is True
    assert kernel.calls == [(str(base / root), str(target))]
    assert repair.match_whitelist(target, layout).reserved is reserved
    assert service.snapshot()["repaired"] == 1


@pytest.mark.parametrize(
    "relative",
    [
        "data",
        "data/soul/Alice/core.json",
        "data/legacy/Alice/legacy_history.db",
        "data/ui_sessions.db",
        "data/backups/Touka_Backup_1.zip",
        "data/stats/Alice",
        "data/logs/x.log",
        "data/temp/docgen_output/a.pdf",
        "data/avatars/a.png",
        "data/vault/core_memory.db",
        "data/vault/core_memory_backups/b.db",
        "data/mcp_servers/.touka-selftest-1/x",
        "data/mcp_servers/managed/../../soul/x",
        "data/custom/gpt-x",
        "data/custom/gpt-x/config.json",
        "data/personal_drive/ Alice/x",
        "config/connections.json",
        "host_root/.env",
        "host_root/backups/Touka_Backup_x.zip",
        "host_root/backups/.snapshot-1/f",
        "workspace/project/main.py",
        "state/executor/ipc/token",
        "elsewhere/file",
    ],
)
def test_paths_outside_the_whitelist_are_refused_without_touching_the_kernel(tmp_path, relative):
    """Touka 自己的狀態、記憶系統的快照與升級備份、工作區與專案：一律 refused_outside。"""
    kernel = FakeKernel()
    service, _ = _service(tmp_path, kernel=kernel)
    assert _repair(service, tmp_path.resolve() / relative) is False
    assert kernel.calls == []
    counts = service.snapshot()
    assert counts["refused_outside"] == 1 and counts["repaired"] == 0


@pytest.mark.parametrize("raw", ["personal_drive/Alice/x", 7])
def test_relative_or_unusable_filenames_are_refused(tmp_path, raw):
    kernel = FakeKernel()
    service, _ = _service(tmp_path, kernel=kernel)
    assert service.repair("read_file_text", (raw, None), retry=True) is False
    assert kernel.calls == [] and service.snapshot()["refused_outside"] == 1


def test_the_protection_layer_refuses_on_its_own_inside_the_whitelist(tmp_path):
    """白名單被注入成包含受保護路徑時，保護層單獨也會拒絕。"""
    kernel = FakeKernel()
    service, _ = _service(tmp_path, kernel=kernel, protected=lambda _path: True)
    assert _repair(service, tmp_path.resolve() / "data/personal_drive/Alice/x") is False
    assert kernel.calls == []
    assert service.snapshot()["refused_protected"] == 1


@pytest.mark.parametrize(
    ("original", "resolved"),
    [
        ("data/personal_drive/Alice/link/x", "data/personal_drive/Bob/x"),
        ("data/personal_drive/Alice/link/x", "data/soul/Alice/x"),
        ("data/soul/Alice/link/x", "data/personal_drive/Alice/x"),
        ("data/personal_drive/Alice/link", "data/personal_drive"),
    ],
)
def test_original_and_resolved_paths_must_fall_in_the_same_whitelist_entry(
    tmp_path, monkeypatch, original, resolved
):
    base = tmp_path.resolve()
    real_resolve = Path.resolve
    links = {base / original: base / resolved}
    monkeypatch.setattr(
        Path, "resolve", lambda self, strict=False: links.get(self) or real_resolve(self, strict)
    )
    kernel = FakeKernel()
    service, _ = _service(tmp_path, kernel=kernel)
    assert _repair(service, base / original) is False
    assert kernel.calls == [] and service.snapshot()["refused_outside"] == 1


def test_both_filenames_of_a_two_path_error_are_tried(tmp_path):
    kernel = FakeKernel()
    service, _ = _service(tmp_path, kernel=kernel)
    base = tmp_path.resolve()
    paths = (str(base / "data/soul/x"), str(base / "data/personal_drive/Alice/y"))
    assert service.repair("copy_file", paths, retry=False) is True
    assert [target for _, target in kernel.calls] == [paths[1]]


# --- 8：頻率限制（時間窗到期自動恢復；根另有保留額度）---


def test_the_same_target_is_repaired_at_most_once_per_cooldown(tmp_path):
    kernel, clock = FakeKernel(), Clock()
    service, _ = _service(tmp_path, kernel=kernel, clock=clock)
    target = tmp_path.resolve() / "data/personal_drive/Alice/x"
    assert _repair(service, target) is True
    clock.now += repair.TARGET_COOLDOWN_SECONDS - 1
    assert _repair(service, target) is False
    clock.now += 1
    assert _repair(service, target) is True
    counts = service.snapshot()
    assert len(kernel.calls) == 2 and counts["throttled"] == 1
    assert counts["permission_relock"] is False


def test_the_global_window_runs_out_then_recovers_and_roots_keep_a_reserve(tmp_path):
    kernel, clock = FakeKernel(), Clock()
    service, layout = _service(tmp_path, kernel=kernel, clock=clock)
    drive = layout.data_root / "personal_drive" / "Alice"
    for index in range(repair.WINDOW_LIMIT):
        assert _repair(service, drive / f"f{index}") is True
    assert _repair(service, drive / "one-too-many") is False
    assert service.snapshot()["permission_relock"] is True
    # 全域額度用完時，根（W1、ST、ST/proxy）仍修得回來
    for root in sorted(repair.reserved_roots(layout)):
        assert _repair(service, root) is True
    clock.now += repair.WINDOW_SECONDS
    counts = service.snapshot()
    assert counts["permission_relock"] is False
    assert _repair(service, drive / "next-window") is True
    assert counts["throttled"] == 1


# --- 9：計數不帶路徑與代理人 ID ---


def test_counts_are_plain_numbers_without_paths_or_agent_ids(tmp_path):
    kernel = FakeKernel()
    service, layout = _service(tmp_path, kernel=kernel)
    _repair(service, layout.data_root / "personal_drive" / "Alice" / "x")
    _repair(service, layout.data_root / "soul" / "Alice" / "x")
    service.note_retry("read_file_text", succeeded=True)
    service.note_retry("read_file_text", succeeded=False)
    counts = service.snapshot()
    flags = {"permission_relock", "denied_burst", "window_resets_at"}
    assert set(counts) == {*repair.COUNTERS, *flags}
    assert (counts["attempted"], counts["repaired"], counts["refused_outside"]) == (2, 1, 1)
    assert (counts["retried"], counts["retry_failed"]) == (1, 1)
    assert "Alice" not in repr(counts) and str(tmp_path.resolve()) not in repr(counts)


def test_kernel_results_map_to_not_owner_and_nothing_changed(tmp_path):
    base = tmp_path.resolve() / "data/personal_drive/Alice"
    for outcome, name in (
        (RepairOutcome(stopped=owner_repair.NOT_OWNER), "refused_not_owner"),
        (RepairOutcome(), "nothing_changed"),
        (RepairOutcome(stopped=owner_repair.SYMLINK), "nothing_changed"),
        (RepairOutcome(missing_tail=True), "nothing_changed"),
    ):
        service, _ = _service(tmp_path, kernel=FakeKernel(outcome))
        assert _repair(service, base / "x") is False
        assert service.snapshot()[name] == 1


# --- 16：多執行緒同時呼叫 ---


def test_concurrent_calls_keep_counts_and_the_window_consistent(tmp_path):
    previous = sys.getswitchinterval()
    kernel, clock = FakeKernel(), LockCheckingClock()
    service, layout = _service(tmp_path, kernel=kernel, clock=clock)
    clock.service = service
    drive = layout.data_root / "personal_drive" / "Alice"
    threads, per_thread = 8, 40
    barrier = threading.Barrier(threads)

    def worker(index: int) -> None:
        barrier.wait()
        for item in range(per_thread):
            _repair(service, drive / f"t{index}-{item}")

    sys.setswitchinterval(1e-6)
    try:
        pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join()
    finally:
        sys.setswitchinterval(previous)
    counts = service.snapshot()
    total = threads * per_thread
    assert clock.unlocked == 0
    assert counts["attempted"] == total
    assert counts["repaired"] == len(kernel.calls) == repair.WINDOW_LIMIT
    assert counts["throttled"] == total - repair.WINDOW_LIMIT


# --- 12、15：只在 Linux 安裝 ---


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_install_does_nothing_outside_linux(monkeypatch, platform):
    monkeypatch.setattr(repair, "sys", types.SimpleNamespace(platform=platform))
    assert repair.install() is None
    assert pr._hook is None


def test_install_on_linux_sets_the_global_hook(monkeypatch, tmp_path):
    monkeypatch.setattr(repair, "sys", types.SimpleNamespace(platform="linux"))
    service, _ = _service(tmp_path)
    assert repair.install(service) is service
    assert pr._hook is service


# --- 17：kernel 沒有改動 → 裝飾器不重試（真服務＋裝飾器）---


def test_a_kernel_that_changed_nothing_is_not_retried(tmp_path, on_linux):
    service, layout = _service(tmp_path, kernel=FakeKernel(RepairOutcome()))
    set_permission_repair(service)
    body = Flaky(failures=1, error=_eacces(str(layout.data_root / "personal_drive/Alice/x")))
    with pytest.raises(PermissionError) as caught:
        _decorated(body, retry=True)()
    assert caught.value is body.error and body.runs == 1
    assert service.snapshot()["nothing_changed"] == 1


# --- 定期檢查（第二段由 monitor 呼叫）：只修根本身、沒改動不記 ---


def test_check_roots_repairs_only_the_reserved_roots_quietly(tmp_path):
    kernel, clock = FakeKernel(RepairOutcome()), Clock()
    service, layout = _service(tmp_path, kernel=kernel, clock=clock)
    assert service.check_roots() == 0
    assert sorted(target for _, target in kernel.calls) == sorted(
        str(root) for root in repair.reserved_roots(layout)
    )
    assert service.snapshot()["attempted"] == 0
    kernel.outcome = RepairOutcome(changed=1)
    clock.now += repair.WINDOW_SECONDS
    assert service.check_roots() == len(repair.reserved_roots(layout))
    assert service.snapshot()["repaired"] == len(repair.reserved_roots(layout))


# --- 21、22：被拒的記錄層級與彙總告警（附錄 §6）---

LOGGER = "src.services.isolation.repair"


def _records(caplog, minimum: int = logging.DEBUG) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER and r.levelno >= minimum]


def test_a_single_refusal_is_logged_at_debug_without_the_path(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    service, layout = _service(tmp_path)
    _repair(service, layout.data_root / "soul" / "Alice" / "x")
    records = _records(caplog)
    assert records and {r.levelno for r in records} == {logging.DEBUG}
    assert "Alice" not in caplog.text and str(tmp_path.resolve()) not in caplog.text


def test_fifty_refusals_in_a_window_warn_once_per_ten_minutes(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    clock = Clock()
    service, layout = _service(tmp_path, clock=clock)
    soul = layout.data_root / "soul" / "Alice"

    def refuse(times: int) -> list[logging.LogRecord]:
        caplog.clear()
        for index in range(times):
            _repair(service, soul / f"x{index}", method=f"m{index % 4}")
        return _records(caplog, logging.WARNING)

    assert refuse(repair.REFUSED_BURST - 1) == []
    warned = refuse(1)
    assert len(warned) == 1 and service.snapshot()["denied_burst"] is True
    assert "Alice" not in caplog.text and str(tmp_path.resolve()) not in caplog.text
    assert "data_root" in warned[0].getMessage()
    clock.now += repair.WINDOW_SECONDS
    assert service.snapshot()["denied_burst"] is False
    assert refuse(repair.REFUSED_BURST) == []
    clock.now += repair.BURST_WARN_GAP_SECONDS
    assert len(refuse(repair.REFUSED_BURST)) == 1


@pytest.mark.parametrize(
    ("outcome", "burst"),
    [
        (RepairOutcome(stopped=owner_repair.NOT_OWNER), repair.NOT_OWNER_BURST),
        (RepairOutcome(), repair.UNCHANGED_BURST),
    ],
)
def test_not_owner_and_nothing_changed_log_info_then_warn_on_a_burst(
    tmp_path, caplog, outcome, burst
):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    service, layout = _service(tmp_path, kernel=FakeKernel(outcome))
    drive = layout.data_root / "personal_drive" / "Alice"
    for index in range(burst - 1):
        _repair(service, drive / f"x{index}")
    assert {r.levelno for r in _records(caplog)} == {logging.INFO}
    _repair(service, drive / "last")
    assert _records(caplog)[-1].levelno == logging.WARNING
    assert "Alice" not in caplog.text


# ======================================================================================
# 真的 ResourceManager 方法（檔案系統呼叫換成會丟 EACCES 的替身）
# ======================================================================================


class Raiser:
    """替換 Path／shutil 的某個方法：前 ``failures`` 次丟出同一個例外物件，之後呼叫原本的。"""

    def __init__(self, original, error: BaseException, failures: int = 1) -> None:
        self.original, self.error, self.failures, self.calls = original, error, failures, 0

    def __get__(self, instance, owner=None):
        """裝在類別上時照方法綁定 ``self``（Path 的方法）。"""
        return self if instance is None else types.MethodType(self, instance)

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return self.original(*args, **kwargs)


# --- 10：豁免方法（swap 依賴錯誤原樣往上丟）根本不呼叫掛鉤 ---


@pytest.mark.parametrize("method", ["move_file", "remove_file"])
def test_exempt_resource_manager_methods_never_reach_the_hook(
    tmp_path, monkeypatch, on_linux, method
):
    src, dst = tmp_path / "a.txt", tmp_path / "b.txt"
    src.write_text("x", encoding="utf-8")
    error = _eacces(str(src))
    attribute = "replace" if method == "move_file" else "unlink"
    monkeypatch.setattr(Path, attribute, Raiser(getattr(Path, attribute), error, failures=9))
    hook = FakeHook()
    set_permission_repair(hook)
    args = (src, dst) if method == "move_file" else (src,)
    with pytest.raises(PermissionError) as caught:
        getattr(resource_manager, method)(*args)
    assert caught.value is error and hook.calls == []


# --- 20（附錄）：記憶系統的路徑在白名單外 → 同一個例外物件、只執行一次 ---


def test_refused_a_line_paths_raise_the_same_object_and_run_once(tmp_path, monkeypatch, on_linux):
    service, layout = _service(tmp_path)
    set_permission_repair(service)
    snapshot_zip = layout.env_file.parent / "backups" / "x.zip"
    error = _eacces(str(snapshot_zip))
    reader = Raiser(Path.read_bytes, error, failures=9)
    monkeypatch.setattr(Path, "read_bytes", reader)
    with pytest.raises(PermissionError) as caught:
        resource_manager.read_file_binary(snapshot_zip)
    assert caught.value is error and reader.calls == 1
    assert (caught.value.errno, caught.value.filename) == (errno.EACCES, str(snapshot_zip))

    leftover = tmp_path / "leftover"
    leftover.mkdir()
    rmtree = Raiser(shutil.rmtree, error, failures=9)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    with pytest.raises(PermissionError) as caught:
        resource_manager.remove_dir(leftover)
    assert caught.value is error and rmtree.calls == 1
    counts = service.snapshot()
    assert counts["refused_outside"] == 2 and counts["repaired"] == 0


# --- 13：真的 Soul Protection Error ---


def test_the_real_soul_protection_refusal_never_reaches_the_hook(on_linux):
    hook = FakeHook()
    set_permission_repair(hook)
    soul_file = Path(resource_manager.get_base_path()) / "soul" / "Alice" / "core.json"
    for call in (
        lambda: resource_manager.write_file_text(soul_file, "{}"),
        lambda: resource_manager.write_file_binary(soul_file, b"{}"),
        lambda: resource_manager.append_file_text(soul_file, "x"),
        lambda: resource_manager.remove_dir(soul_file.parent),
    ):
        with pytest.raises(PermissionError) as caught:
            call()
        assert caught.value.errno is None and "Soul Protection" in str(caught.value)
    assert hook.calls == [] and not soul_file.exists()


# --- 6：真的巢狀（get_uploads_path 包著 get_outputs_path）---


def test_get_uploads_path_is_repaired_once_by_the_outermost_call(
    monkeypatch, on_linux, mock_identity
):
    """內層 get_outputs_path 不各自修、不各自重試：mkdir 失敗兩次就照實往上丟。"""
    mkdir = Raiser(Path.mkdir, _eacces(), failures=2)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    hook = FakeHook()
    set_permission_repair(hook)
    with pytest.raises(PermissionError):
        resource_manager.get_uploads_path(mock_identity())
    assert [method for method, _ in hook.calls] == ["get_uploads_path"]
    assert mkdir.calls == 2 and hook.retries == [("get_uploads_path", False)]


def test_read_json_inner_calls_are_each_repaired_once(tmp_path, monkeypatch, on_linux):
    """``read_json`` 是 delegates 豁免：內層 exists／read_file_text 各自是最外層，各修一次。"""
    target = tmp_path / "x.json"
    target.write_text('{"a": 1}', encoding="utf-8")
    reader = Raiser(Path.read_text, _eacces(str(target)))
    monkeypatch.setattr(Path, "read_text", reader)
    hook = FakeHook()
    set_permission_repair(hook)
    assert resource_manager.read_json(target) == {"a": 1}
    assert [method for method, _ in hook.calls] == ["read_file_text"]
    assert reader.calls == 2


# ======================================================================================
# 第二段：monitor 的接線（status.py）——安裝、定期檢查、_state_issue、snapshot 的 repairs
# ======================================================================================


def _monitor_layout(tmp_path: Path) -> SandboxLayout:
    """``_layout`` 加上自測與健康檢查要的 kernel（兩份 launcher 內容相同＝雜湊一致）。"""
    base = tmp_path.resolve()
    for kernel in (base / "kernel", base / "app" / "ToukaProxy" / "src" / "sandbox_kernel"):
        kernel.mkdir(parents=True, exist_ok=True)
        (kernel / "launcher.py").write_text("# kernel\n", encoding="utf-8")
    (base / "data" / "mcp_servers").mkdir(parents=True, exist_ok=True)
    (base / "state").mkdir(exist_ok=True)
    return replace(_layout(tmp_path), kernel_dir=base / "kernel")


async def _selftest_ok(_kernel_dir: Path, _test_dir: Path) -> SelfTestResult:
    return SelfTestResult(ok=True, abi=7)


def _wired_monitor(layout: SandboxLayout, clock: Clock | None = None) -> IsolationMonitor:
    return IsolationMonitor(
        layout_provider=lambda: layout,
        selftest=_selftest_ok,
        clock=clock or Clock(),
        platform="linux",
        start_background=False,
    )


class SpyService(PermissionRepairService):
    """真的服務（假 kernel），另外記下定期檢查的次數與 ``repair`` 收到的方法名。"""

    def __init__(self, layout: SandboxLayout, kernel: FakeKernel | None = None) -> None:
        super().__init__(lambda: layout, repair_chain=kernel or FakeKernel(RepairOutcome()))
        self.root_checks = 0
        self.methods: list[str] = []

    def check_roots(self) -> int:
        self.root_checks += 1
        return super().check_roots()

    def repair(self, method: str, paths: tuple[object, ...], *, retry: bool) -> bool:
        self.methods.append(method)
        return super().repair(method, paths, retry=retry)


async def test_first_ensure_ready_installs_the_hook_on_the_monitor_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(repair, "sys", types.SimpleNamespace(platform="linux"))
    monitor = _wired_monitor(_monitor_layout(tmp_path))
    snap = await monitor.ensure_ready()
    service = pr._hook
    assert isinstance(service, PermissionRepairService)
    assert service._layout == monitor._current_layout  # 用 monitor 快取的版面
    assert snap.repairs is not None and snap.repairs["attempted"] == 0
    assert set(snap.repairs) == set(service.snapshot())
    await monitor.ensure_ready()
    assert pr._hook is service  # 只裝一次


async def test_ensure_ready_installs_nothing_outside_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(repair, "sys", types.SimpleNamespace(platform="win32"))
    snap = await _wired_monitor(_monitor_layout(tmp_path)).ensure_ready()
    assert pr._hook is None and snap.repairs is None


async def test_refresh_checks_the_roots_about_once_a_minute(tmp_path):
    layout, clock = _monitor_layout(tmp_path), Clock()
    monitor = _wired_monitor(layout, clock)
    spy = monitor._repair = SpyService(layout)
    await monitor.refresh()
    clock.now += status.ROOT_REPAIR_INTERVAL_SECONDS / 2
    await monitor.refresh()
    assert spy.root_checks == 1
    clock.now += status.ROOT_REPAIR_INTERVAL_SECONDS / 2
    await monitor.refresh()
    assert spy.root_checks == 2
    assert monitor.snapshot().repairs["attempted"] == 0  # 沒有改動不記錄


async def test_a_failing_periodic_check_does_not_break_the_health_check(tmp_path):
    layout = _monitor_layout(tmp_path)
    monitor = _wired_monitor(layout)
    spy = monitor._repair = SpyService(layout)
    spy.check_roots = lambda: 1 / 0
    await monitor.run_selftest("data", notify=False)
    snap = await monitor.refresh()
    assert snap.level == "landlock"


def test_state_issue_repairs_st_proxy_once_then_judges_again(tmp_path, monkeypatch):
    """ST/proxy 不可寫（``os.access`` 失敗）：先修一次 ST/proxy、再判斷一次。"""
    layout = _monitor_layout(tmp_path)
    monitor = _wired_monitor(layout)
    hook = monitor._repair = FakeHook()
    answers = iter([False, True])
    monkeypatch.setattr(status.os, "access", lambda *_args: next(answers))
    assert monitor._check_disk(None).state_issue is None
    assert hook.calls == [("state_issue", (str(layout.state_root / "proxy"),))]
    assert hook.retry_flags == [False]


@pytest.mark.parametrize("hook", [None, FakeHook(result=False), FakeHook(error=RuntimeError())])
def test_state_issue_stays_not_writable_when_the_repair_does_not_help(tmp_path, monkeypatch, hook):
    monitor = _wired_monitor(_monitor_layout(tmp_path))
    monitor._repair = hook
    monkeypatch.setattr(status.os, "access", lambda *_args: False)
    assert monitor._check_disk(None).state_issue == "not_writable"


def test_snapshot_repairs_come_from_the_service_without_paths(tmp_path):
    layout = _monitor_layout(tmp_path)
    monitor = _wired_monitor(layout)
    assert monitor.snapshot().repairs is None
    spy = monitor._repair = SpyService(layout, FakeKernel())
    spy.repair("read_file_text", (str(layout.data_root / "personal_drive/Alice/x"),), retry=True)
    repairs = monitor.snapshot().repairs
    assert repairs["repaired"] == 1 and "Alice" not in repr(repairs)
