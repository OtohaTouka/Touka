"""iso-c2：proxy 本機的 Landlock 在映像裡可用（沒有執行器、``--network none``、關 Fast Open）.

只能由 ``tests/docker/sandboxed.py`` 先開 smoke 沙盒再執行；compose 的 ``iso-landlock``（profile
``landlock``）就是這個形狀。真 monitor 要判 ``landlock``、網路設定安全（``local.net`` 是 None）、
features 含 tcp；再經真 ``RunnerBroker``／``LandlockRunner`` 跑一條代理人指令：讀得到自己的資料夾、
讀不到別人的。每一項寫一行 JSON；全部通過才 exit 0。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from src.models.identity import AgentIdentity
from src.services.execution_broker import spawn_exec
from src.services.isolation.consent import ExecutionGrant
from src.services.isolation.profiles import build_agent_command_profile, current_layout
from src.services.isolation.runners import ExecRequest, ProfileSource, RunnerBroker
from src.services.isolation.status import IsolationMonitor

from tests import network_guard

if TYPE_CHECKING:
    from src.services.isolation.profiles import SandboxProfile

OWNER, OTHER = "Alice", "Bob"
OTHER_CONTENT = "only-bob-may-read-this"
AGENT_PATH = "/usr/local/bin:/usr/bin:/bin"
COMMAND_SECONDS = 60.0


def emit(check: str, ok: bool, **detail: object) -> bool:
    """寫一行結果；回傳 ``ok``。"""
    sys.stdout.write(json.dumps({"check": check, "ok": ok, **detail}, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return ok


async def run_checks(kernel: Path) -> int:
    root = Path(tempfile.mkdtemp(prefix="touka_iso_landlock_"))
    state = root / "st"
    state.mkdir(mode=0o700)
    layout = replace(
        current_layout(), data_root=root / "data", kernel_dir=kernel, state_root=state,
        executor_dir=None, mirror_roots=(), in_container=True,
    )
    drives = layout.data_root / "personal_drive"
    for owner in (OWNER, OTHER):
        (drives / owner).mkdir(parents=True)
    (drives / OTHER / "secret.txt").write_text(OTHER_CONTENT, encoding="utf-8")
    monitor = IsolationMonitor(layout_provider=lambda: layout, start_background=False)
    snap = await monitor.ensure_ready()
    ok = snap.level == "landlock" and snap.local.net is None and "tcp" in snap.features
    if not emit("landlock_level", ok, level=snap.level, reason=snap.reason, net=snap.local.net):
        return 1
    identity = AgentIdentity.for_role(OWNER, "Owner", capabilities=[])

    async def build(location: str, executor_state: str | None) -> SandboxProfile:
        return await build_agent_command_profile(
            identity, drives / OWNER, layout=layout, location=location,
            block_ports=monitor.block_ports(location), executor_state=executor_state,
        )

    broker = RunnerBroker(monitor=monitor, layout_provider=lambda: layout)
    acquired = await broker.acquire(
        ExecutionGrant("landlock"), ProfileSource(build, "not_in_executor")
    )
    script = f"echo own > own && cat own && cat ../{OTHER}/secret.txt"
    request = ExecRequest(
        argv=("sh", "-c", script), env={"PATH": AGENT_PATH}, profile=acquired.profile
    )
    plan = acquired.runner.plan(request)
    try:
        proc = await spawn_exec(
            *plan.argv, cwd=plan.cwd, env=plan.env, pass_fds=plan.pass_fds, close_fds=True,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, new_process_group=True,
        )
    finally:
        plan.release()
    out, _ = await asyncio.wait_for(proc.communicate(), COMMAND_SECONDS)
    output = out.decode("utf-8", "replace")
    ok = output.splitlines()[:1] == ["own"] and OTHER_CONTENT not in output and proc.returncode != 0
    return 0 if emit("landlock_command", ok, level=plan.level, code=proc.returncode) else 1


def main() -> int:
    if not network_guard.is_installed():
        sys.stderr.write("run this through tests/docker/sandboxed.py (smoke sandbox first)\n")
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    return asyncio.run(run_checks(parser.parse_args(sys.argv[1:]).kernel))


if __name__ == "__main__":
    sys.exit(main())
