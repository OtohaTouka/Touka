"""刪除靈魂前清掉它在隔離暫存區的 HOME／TMP：同 ID 的新靈魂不沿用舊檔案（ADR-0272 已知限制 26）.

- proxy 那一層（``ST/proxy/agents/<id>``，proxy 自己的 volume）：proxy 直接整個刪掉；路徑上有連結就
  拒絕，不跟過去。
- 執行器那一層（``<state_root>/agents/<id>/{home,tmp}``，只掛在執行器）：proxy 碰不到，走既有的
  START（kind agents、owner 是這個 ID、cwd 是它的 personal_drive）跑固定的清除指令；Landlock 只給它
  自己的 home、tmp 與 personal_drive。不是代理人的程式，所以不需要同意；只用執行器、不降級。
- 部署預期是執行器時，執行器不能用、清不乾淨或逾時都拋 ``HomeWipeError``，呼叫端擋下刪除。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from memory_server.utils.zone_paths import HandoffKind

from src.services.execution_broker import spawn_exec
from src.services.isolation.profiles import (
    ProfileError,
    build_agent_command_profile,
    current_layout,
    state_dirs,
)
from src.services.isolation.runners import (
    ExecRequest,
    IsolationUnavailableError,
    ProfileSource,
    executor_max_seconds,
    runner_broker,
)
from src.services.isolation.status import isolation_monitor
from src.services.process_registry import (
    reap_cancelled_process,
    reap_process_tree,
    terminate_process_tree,
)
from src.services.resource_manager import resource_manager
from src.utils.isolation_policy import is_safe_segment

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity
    from src.services.isolation.profiles import Location, SandboxProfile

logger = logging.getLogger(__name__)

WIPE_SECONDS = 60
# 只清內容（home／tmp 本身由執行器每次 START 前建）：先補回擁有者權限再刪，最後確認是空的
WIPE_SCRIPT = (
    'for d in "$@"; do [ -d "$d" ] || continue; '
    '/bin/chmod -R u+rwx -- "$d" 2>/dev/null; '
    '/usr/bin/find "$d" -mindepth 1 -delete 2>/dev/null; '
    'left=$(/usr/bin/find "$d" -mindepth 1 -print -quit) || exit 3; [ -z "$left" ] || exit 3; done'
)


class HomeWipeError(Exception):
    """清不掉；``reason`` 只是代碼，不帶路徑."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def wipe_agent_homes(identity: AgentIdentity) -> None:
    """先清執行器那一層（可能拒絕），再清 proxy 那一層；任何一處清不掉就拋 ``HomeWipeError``."""
    if not is_safe_segment(identity.agent_id):
        return  # profile 一律拒絕這種名稱，不會有家目錄
    snapshot = await isolation_monitor.ensure_ready()
    if snapshot.expected == "executor":
        await _wipe_in_executor(identity)
    _remove_proxy_state(identity)


async def _profile(
    identity: AgentIdentity, drive: Path, location: Location, executor_state: str | None
) -> SandboxProfile:
    return await build_agent_command_profile(
        identity,
        drive,
        layout=current_layout(),
        location=location,
        block_ports=isolation_monitor.block_ports(location),
        executor_state=executor_state,
    )


async def _wipe_in_executor(identity: AgentIdentity) -> None:
    drive = resource_manager.soul_drive(identity)  # 個人空間（ADR-0293）
    if drive is None:
        raise HomeWipeError("unsafe_owner")
    issue = isolation_monitor.executor_scope_issue(identity, None)
    # START 與 relay 的 cwd 交給接收端（G1）；路上有連結就拒絕，spawn 之後就收回
    with contextlib.ExitStack() as handoff:
        try:
            cwd = handoff.enter_context(
                resource_manager.zone_handoff(drive, HandoffKind.SUBPROCESS_CWD)
            )
            acquired = await runner_broker.acquire_maintenance(
                ProfileSource(partial(_profile, identity, Path(cwd)), issue)
            )
            resource_manager.zone_make_dir(drive)  # 刪除靈魂接著會整個刪掉
            env = acquired.profile.env_set
            argv = ("/bin/sh", "-c", WIPE_SCRIPT, "touka-home-wipe", env["HOME"], env["TMPDIR"])
            request = ExecRequest(
                argv=argv, env={}, profile=acquired.profile,
                max_seconds=executor_max_seconds(WIPE_SECONDS),
            )
            plan = acquired.runner.plan(request)
        except (IsolationUnavailableError, ProfileError) as exc:
            raise HomeWipeError(getattr(exc, "reason", None) or "isolation_unavailable") from exc
        except OSError as exc:  # 含個人空間路上有連結
            raise HomeWipeError("drive_unavailable") from exc
        try:
            proc = await spawn_exec(
                *plan.argv, new_process_group=True, cwd=str(plan.cwd), env=plan.env,
                pass_fds=plan.pass_fds, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            acquired.runner.failure(plan.read_status())  # 沒啟動：狀態 fd 的讀端照樣關掉
            raise HomeWipeError("spawn_failed") from exc
        finally:
            plan.release()
    try:
        code = await asyncio.wait_for(proc.wait(), WIPE_SECONDS)
    except TimeoutError as exc:
        await reap_process_tree(proc, terminate_process_tree)
        raise HomeWipeError("timeout") from exc
    except asyncio.CancelledError:  # 請求被取消（關機等）也收掉 relay，執行器跟著收遠端
        await reap_cancelled_process(proc, terminate_process_tree)
        raise
    finally:
        failure = acquired.runner.failure(plan.read_status())
    if failure is not None:
        isolation_monitor.report_failure(failure.code)
        raise HomeWipeError(failure.code)
    if code != 0:
        raise HomeWipeError(f"exit_{code}")


def _remove_proxy_state(identity: AgentIdentity) -> None:
    """``ST/proxy/agents/<id>`` 整個刪掉；ST 沒設定就沒有東西可清."""
    layout = current_layout()
    if layout.state_root is None:
        return
    try:
        home, _tmp = state_dirs(layout, "proxy", "agents", identity.agent_id, create=False)
    except ProfileError as exc:
        raise HomeWipeError("unsafe_owner") from exc
    owner = home.parent
    if owner.is_symlink() or owner.is_junction() or owner.resolve() != owner:
        logger.warning("Sandbox home of %s is a link; not following it", identity.agent_id)
        raise HomeWipeError("proxy_state_link")
    try:
        resource_manager.remove_dir(owner, unlock=True)  # 唯讀、被拿掉權限的也刪
    except OSError as exc:
        raise HomeWipeError("proxy_state_remove_failed") from exc
