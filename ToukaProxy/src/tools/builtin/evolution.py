"""代理人自主演化工具組 (Self-Evolution & Sandbox Tools).

本模組支撐 Agent 的「發現 -> 獲取 -> 創造」演化流水線。
提供了一套完整的演化沙盒，包含代碼生成、環境隔離、運行時監獄 (Runtime Jail) 
以及基於 HITL (Human-In-The-Loop) 的部署核准機制。
"""

import asyncio
import hashlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from functools import wraps
from typing import Any

from mcp import Client, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from src.config import get_config_dir
from src.models.identity import AgentIdentity
from src.protocols.mcp.client import McpIsolatedLaunch, connect_mcp_server
from src.protocols.mcp.environment import LaunchEnvironmentPolicy
from src.protocols.mcp.isolated_stdio import StderrLog
from src.protocols.registry import ProtocolServerConfig
from src.routers.terminal import request_approval
from src.services import tool_errors
from src.services.autonomy import autonomy_service
from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation.consent import ExecutionGrant
from src.services.isolation.evolution_command import (
    EvolutionRefused,
    EvolutionStdioLaunch,
    acquire_evolution_launch,
    evolution_config,
    evolution_grant,
    refuse_deployed,
    skill_lock,
    validate_skill_id,
)
from src.services.isolation.mcp_launch import McpLaunchInfo
from src.services.isolation.runners import (
    ISOLATION_UNAVAILABLE,
    Acquired,
    IsolationUnavailableError,
)
from src.services.isolation.status import isolation_monitor
from src.services.mcp_isolation_store import McpIsolationStore, assign, bound_of
from src.services.resource_manager import resource_manager
from src.utils import config_utils
from src.utils.config_utils import MCP_CONFIG_FILENAME
from src.utils.secrets import redact_secrets_from_content
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)

# 演化環境建置的硬逾時（秒）；在 POSIX 同時作為 RLIMIT_CPU。
_VENV_CREATE_TIMEOUT_SECONDS = 120
_PIP_INSTALL_TIMEOUT_SECONDS = 600
# 沙盒 venv 目錄（建在工具沙盒根目錄底下）。
_VENV_DIR = ".venv"
_STDERR_POLL_SECONDS = 0.01
_STDERR_READ_BYTES = 65536


def _evolution_refused(reason: str) -> str:
    suggestion = (
        "Cannot read the MCP config. Repair it before retrying. "
        if reason == "config_unreadable" else ""
    )
    return tool_errors.build_tool_error(
        tool_errors.VALIDATION_ERROR, "Evolution refused: " + reason,
        reason=reason,
        suggestion=suggestion + "Use a new skill_id, remove the deployed server in settings, "
        "or resubmit after the current operation ends.",
    )


def _skill_operation(function: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    @wraps(function)
    async def guarded(skill_id: str, *args: Any, **kwargs: Any) -> str:
        try:
            validate_skill_id(skill_id)
            async with skill_lock(skill_id, kwargs.get("_evolution_lock")) as lease:
                await asyncio.to_thread(refuse_deployed, skill_id)
                return await function(skill_id, *args, **{**kwargs, "_evolution_lock": lease})
        except EvolutionRefused as exc:
            return _evolution_refused(exc.reason)
    return guarded


@dataclass(frozen=True)
class VerifyOutcome:
    """可信的協定結果與供模型閱讀的文字分開。"""

    passed: bool
    text: str


@asynccontextmanager
async def _windows_client(
    config: ProtocolServerConfig, timeout: int, tail: list[str],
) -> AsyncIterator[Client]:
    """SDK 管理 Windows 子程序；非阻塞 pipe 只收有界、遮罩的診斷。"""
    read_fd, write_fd = os.pipe()
    stderr = StderrLog("evolution", (), lambda text: redact_secrets_from_content(text)[0])
    closing = False

    async def drain() -> None:
        while True:
            try:
                chunk = os.read(read_fd, _STDERR_READ_BYTES)
            except BlockingIOError:
                if closing:
                    return
                await asyncio.sleep(_STDERR_POLL_SECONDS)
            else:
                if not chunk:
                    return
                stderr.feed(chunk)
                if closing:
                    return
                await asyncio.sleep(0)

    reader = None
    try:
        os.set_blocking(read_fd, False)
        reader = asyncio.create_task(drain())
        params = StdioServerParameters(
            command=config.command, args=config.args, env=LaunchEnvironmentPolicy.clean(None),
        )
        async with Client(
            stdio_client(params, errlog=write_fd), mode=types.LATEST_PROTOCOL_VERSION,
            read_timeout_seconds=timeout,
        ) as client:
            raw = await client.session.send_discover(types.LATEST_PROTOCOL_VERSION)
            client.session.adopt(types.DiscoverResult.model_validate(raw))
            yield client
    finally:
        closing = True
        os.close(write_fd)
        try:
            if reader is not None:
                await reader
        finally:
            os.close(read_fd)
            stderr.close()
            tail.append(stderr.tail())


def _venv_python(skill_id: str) -> str:
    """沙盒 venv 的 Python 絕對路徑（Windows 找執行檔不看子程序 cwd，不能給相對路徑）.

    區字串（ADR-0293 P4）：只當子行程的 argv 與部署條目；POSIX 的 venv python 本來就是連結。
    """
    windows = EvolutionPathGuard.is_windows()
    folder, name = ("Scripts", "python.exe") if windows else ("bin", "python")
    venv = EvolutionPathGuard.tool_zone(skill_id).child(_VENV_DIR)
    return resource_manager.zone_text(venv.child(folder).child(name))


async def _isolation_grant(
    tool: str, args: dict[str, Any], kwargs: dict[str, Any]
) -> ExecutionGrant | None:
    """工具開頭取一次隔離 grant；合併工具傳下來的同一次呼叫的 grant 直接沿用.

    模型能給的只有 JSON，``_evolution_grant`` 不是 ``ExecutionGrant`` 物件就不採用；身分、
    session、call_id 由執行器注入（覆寫模型給的同名參數）。
    """
    shared = kwargs.get("_evolution_grant")
    if isinstance(shared, ExecutionGrant):
        return shared
    identity = kwargs.get("identity")
    return await evolution_grant(
        identity if isinstance(identity, AgentIdentity) else None,
        kwargs.get("session_id"),
        kwargs.get("_call_id"),
        tool=tool,
        args=args,
    )


def _isolation_unavailable(skill_id: str, reason: str) -> str:
    """拿不到可用的隔離 runner：演化指令沒有執行（upstream_error，原因 isolation_unavailable）."""
    return tool_errors.build_tool_error(
        tool_errors.UPSTREAM_ERROR,
        f"Evolution sandbox for '{skill_id}' was not run: "
        "no isolated execution environment is available.",
        suggestion=(
            "Do not run it another way; tell the user isolation is unavailable "
            f"({reason}) and ask them to approve it in the chat or fix the deployment."
        ),
        reason=ISOLATION_UNAVAILABLE,
        isolation_reason=reason,
    )


def _clamp_quota(requested: Any, ceiling: int) -> int:
    """Clamp a model-supplied sandbox quota to the SSOT ceiling (#9 fix).

    A model-controlled override (timeout_override / max_storage_mb) is honoured ONLY
    when it is a positive number that is STRICTER (smaller) than the SSOT ceiling.
    Anything else — None, zero, negative, non-numeric, or a value above the ceiling —
    falls back to the ceiling, so a tool call cannot raise the ceiling above the SSOT.

    Enforcement strength differs by quota: the timeout ceiling is enforced at the OS
    boundary (subprocess.run(timeout=...)), so clamping it is a hard limit. The
    storage ceiling is enforced only by the in-process JailedWriter monkeypatch and is
    best-effort — model-authored sandbox code can bypass it (os.write, child process).
    Clamping still removes the trivial "just ask for more" path; a hard storage/memory
    limit needs OS-level rlimits/cgroups (tracked separately, not part of #9).
    """
    if isinstance(requested, bool):
        return ceiling
    if isinstance(requested, (int, float)) and 0 < requested < ceiling:
        return max(1, int(requested))
    return ceiling


def _build_jail_code(
    skill_id: str,  # noqa: ARG001
    tool_name: str,
    arguments: dict[str, Any],
    target_dir: str,
    max_mb: int,
) -> str:
    """建立正式啟動方式的驗證包裝；路徑以 JSON 編碼，工具參數不嵌入程式碼。"""
    del tool_name, arguments  # 工具參數只送 MCP 請求，不再嵌進程式碼。
    safe_target_dir = json.dumps(str(target_dir))

    return f"""
import os
import sys
import runpy
import builtins
import logging
from pathlib import Path

# --- 🧱 運行時沙盒監獄 (Monkeypatch) ---
_original_open = builtins.open
_total_written = 0
MAX_BYTES = {max_mb} * 1024 * 1024

class JailedWriter:
    def __init__(self, fp):
        self.fp = fp
    def write(self, data):
        global _total_written
        size = len(data) if isinstance(data, (bytes, str)) else 0
        if _total_written + size > MAX_BYTES:
            raise PermissionError("🛡️ Sandbox Quota Exceeded: Max storage reached.")
        res = self.fp.write(data)
        _total_written += size
        return res
    def __getattr__(self, name):
        return getattr(self.fp, name)
    def __enter__(self): return self
    def __exit__(self, *args): self.fp.close()

def _jailed_open(file, mode='r', *args, **kwargs):
    # Windows 只有可繞過的寫入配額；讀取與 fd 不隔離，實體邊界由 Linux runner 提供。
    if isinstance(file, int) or not any(m in mode for m in 'wax+'):
        return _original_open(file, mode, *args, **kwargs)
    abs_path = Path(file).resolve()
    jail_root = Path({safe_target_dir}).resolve()
    if not abs_path.is_relative_to(jail_root):
        raise PermissionError(f"🛡️ Sandbox Violation: Access denied to {{abs_path}}")

    fp = _original_open(file, mode, *args, **kwargs)
    if any(m in mode for m in ['w', 'a', 'x', '+']):
        return JailedWriter(fp)
    return fp

builtins.open = _jailed_open

if __name__ == "__main__":
    # 精簡 traceback，避免 SDK 的 Rich 展開原始碼擠掉有界 stderr 中的真正錯誤。
    logging.getLogger().addHandler(logging.StreamHandler(sys.stderr))
    os.chdir({safe_target_dir})
    sys.path.insert(0, {safe_target_dir})
    runpy.run_path("server.py", run_name="__main__")
"""


@_skill_operation
async def propose_new_capability(
    skill_id: str,
    name: str,
    description: str,
    schema: dict[str, Any],
    **_kwargs: Any,
) -> str:
    """演化第一步：提案新能力."""
    EvolutionPathGuard.mkdir_tool(skill_id)
    
    spec = {
        "id": skill_id,
        "name": name,
        "description": description,
        "schema": schema,
        "status": "proposed"
    }
    
    EvolutionPathGuard.write_tool_file(
        skill_id, "spec.json", json.dumps(spec, indent=2, ensure_ascii=False)
    )
        
    return (
        f"Success: Capability '{name}' ({skill_id}) has been proposed. "
        "Next, use 'evolution_implement_and_verify' to write, install and "
        "sandbox-test the code."
    )


@_skill_operation
async def write_mcp_implementation(
    skill_id: str,
    code: str,
    dependencies: list[str] | None = None,
    **_kwargs: Any,
) -> str:
    """演化第二步：程式碼實作與環境建置."""
    # 隔離 grant 在開頭取一次（合併工具呼叫時沿用它的），建 venv 與 pip 共用
    impl_args = {"skill_id": skill_id, "code": code, "dependencies": dependencies}
    grant = await _isolation_grant("write_mcp_implementation", impl_args, _kwargs)

    # 1. 寫入原始碼
    EvolutionPathGuard.write_tool_file(skill_id, "server.py", code)

    # 2. 處理依賴
    deps = dependencies or []
    EvolutionPathGuard.write_tool_file(skill_id, "requirements.txt", "\n".join(deps))

    # 3. 建立並配置 Venv
    res_msg = "Code written to server.py.\n"

    try:
        # 阻塞子程序移出事件迴圈；timeout 同時成為 POSIX 的 RLIMIT_CPU（ADR-0076）。
        # 每條指令 spawn 前才選 runner，spawn 在 to_thread 的 worker 裡（PDEATHSIG）。
        if not EvolutionPathGuard.tool_file_exists(skill_id, _VENV_DIR):
            launch = await acquire_evolution_launch(grant, skill_id)
            await launch.run(
                skill_id,
                [sys.executable, "-m", "venv", _VENV_DIR],  # 用伺服器自己的直譯器，不用 PATH 上的
                capture_output=True,
                check=True,
                timeout=_VENV_CREATE_TIMEOUT_SECONDS,
            )
            res_msg += "✅ Virtual environment created.\n"

        # 安裝 Pip 依賴
        if deps:
            launch = await acquire_evolution_launch(grant, skill_id)
            await launch.run(
                skill_id,
                [_venv_python(skill_id), "-m", "pip", "install", "-r", "requirements.txt"],
                capture_output=True,
                text=True,
                check=True,
                timeout=_PIP_INSTALL_TIMEOUT_SECONDS,
            )
            res_msg += f"✅ Dependencies installed: {', '.join(deps)}\n"
        else:
            res_msg += "ℹ️ No extra dependencies required.\n"

    except EvolutionRefused as exc:
        return _evolution_refused(exc.reason)
    except IsolationUnavailableError as exc:
        return _isolation_unavailable(skill_id, exc.reason)
    except Exception as e:
        logger.exception("🔥 Environment setup failed")
        return f"Error during environment setup: {str(e)}"
    else:
        return (
            f"Development Success:\n{res_msg}\n"
            "Next, use 'verify_mcp_locally' to test your tool."
        )


@_skill_operation
async def verify_mcp_locally(
    skill_id: str, tool_name: str, arguments: dict[str, Any], **kwargs: Any,
) -> str:
    """舊入口保留文字輸出；合併流程只採用內部的 passed 欄位。"""
    return (await _verify_mcp(skill_id, tool_name, arguments, **kwargs)).text


async def _verify_mcp(
    skill_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    **kwargs: Any,
) -> VerifyOutcome:
    """演化第三步：沙盒測試與運行時監獄."""
    verify_args = {"skill_id": skill_id, "tool_name": tool_name, "arguments": arguments}
    grant = await _isolation_grant("verify_mcp_locally", verify_args, kwargs)
    target_dir = EvolutionPathGuard.get_tool_root(skill_id)

    # --- 獲取資源限制 (SSOT Alignment) ---
    # #9 fix: model-supplied overrides may only tighten, never exceed, the SSOT
    # sandbox ceiling (see _clamp_quota) — previously `kwargs.get(...) or ssot` let
    # the model pass an arbitrarily large value to escape the quota.
    limits = autonomy_service.config.get("sandbox_limits", {})
    timeout_sec = _clamp_quota(
        kwargs.get("timeout_override"), limits.get("execution_timeout_sec", 30)
    )
    max_mb = _clamp_quota(kwargs.get("max_storage_mb"), limits.get("max_storage_mb", 50))
    # ADR-0076: the SSOT memory ceiling is now OS-enforced (RLIMIT_AS) on POSIX.
    memory_mb = limits.get("max_memory_mb", 256)

    # --- [注入運行時監獄 (Runtime Jail)] --- C6 fix: use _build_jail_code()
    jail_code = _build_jail_code(skill_id, tool_name, arguments, target_dir, max_mb)
    EvolutionPathGuard.write_tool_file(skill_id, "test_wrapper.py", jail_code)

    tail: list[str] = []
    passed = False
    try:
        launch = await acquire_evolution_launch(grant, skill_id)
        wrapper = EvolutionPathGuard.tool_zone(skill_id).child("test_wrapper.py")
        argv = (_venv_python(skill_id), resource_manager.zone_text(wrapper))
        config = ProtocolServerConfig(command=argv[0], args=list(argv[1:]))
        isolated = None
        if not EvolutionPathGuard.is_windows():
            profile = replace(
                launch.profile,
                rlimits=EvolutionPathGuard.rlimit_values(memory_mb, max_mb, timeout_sec),
            )
            preexec = EvolutionPathGuard._build_rlimit_preexec(  # noqa: SLF001
                memory_mb, max_mb, timeout_sec,
            )
            stdio = EvolutionStdioLaunch(
                info=McpLaunchInfo("self", "evolution", "ok", "launched", launch.runner.level),
                acquired=Acquired(launch.runner, profile), argv=argv,
                preexec_fn=preexec,
                timeout=timeout_sec,
            )
            isolated = McpIsolatedLaunch(
                stdio, skill_id, isolation_monitor.report_failure,
                on_eof="close", stderr_tail=tail.append,
                sanitize=lambda text: redact_secrets_from_content(text)[0],
            )
        async with asyncio.timeout(timeout_sec), AsyncExitStack() as stack:
            if isolated is None:
                client = await stack.enter_async_context(_windows_client(config, timeout_sec, tail))
            else:
                client, connection = await connect_mcp_server(
                    config, isolated=isolated, read_timeout_seconds=timeout_sec,
                )
                await stack.enter_async_context(connection)
            result = await client.session.call_tool(tool_name, arguments)
            passed = not result.is_error
            payload = {
                "status": "success" if passed else "error",
                "data": result.model_dump(mode="json", by_alias=True),
            }
    except EvolutionRefused as exc:
        return VerifyOutcome(False, _evolution_refused(exc.reason))
    except IsolationUnavailableError as exc:
        return VerifyOutcome(False, _isolation_unavailable(skill_id, exc.reason))
    except Exception as exc:
        passed = False
        while isinstance(exc, BaseExceptionGroup):
            exc = exc.exceptions[0]
        payload = {"status": "error", "message": type(exc).__name__}
    finally:
        try:
            EvolutionPathGuard.delete_tool_file(skill_id, "test_wrapper.py")
        except OSError:  # 刪不掉只記 log，不蓋掉驗證結果（ADR-0293 Q3）
            logger.warning("⚠️ Could not remove test_wrapper.py of '%s'", skill_id, exc_info=True)
    return VerifyOutcome(
        passed,
        f"Test Result for '{tool_name}':\n{json.dumps(payload, ensure_ascii=False)}\n"
        "stderr (last 4 KiB, masked):\n" + "".join(tail),
    )


def _deployment_files(skill_id: str) -> list[bytes]:
    """部署快照：路上或檔案本身是連結、讀不到都是 ``skill_changed``（原因留在 ``__cause__``）."""
    tool = EvolutionPathGuard.tool_zone(skill_id)
    try:
        return [
            resource_manager.zone_read_binary(tool.child(name))
            for name in ("server.py", "requirements.txt")
        ]
    except OSError as exc:
        raise EvolutionRefused("skill_changed") from exc


def _approval_digest(contents: list[bytes]) -> tuple[bytes, ...]:
    return tuple(hashlib.sha256(content).digest() for content in contents)


def _deployment_config(skill_id: str) -> dict[str, Any]:
    document = evolution_config()
    if skill_id in document.get("mcpServers", {}):
        raise EvolutionRefused("name_taken")
    return document


def _save_deployment(skill_id: str, digest: tuple[bytes, ...], entry: dict[str, Any]) -> None:
    """最後核對與兩份設定寫入在同一 worker 完成，中途不讓出同技能鎖。"""
    data = _deployment_config(skill_id)
    refuse_deployed(skill_id)
    if not digest or digest != _approval_digest(_deployment_files(skill_id)):
        raise EvolutionRefused("skill_changed")
    record = assign(skill_id, "evolution", bound_of(entry), fresh=True)
    McpIsolationStore(get_config_dir()).mutate(record)
    data.setdefault("mcpServers", {})[skill_id] = entry
    config_utils.save_config_json(MCP_CONFIG_FILENAME, data)


@_skill_operation
async def request_mcp_creation(
    skill_id: str,
    **kwargs: Any,
) -> str:
    """演化第四步：核准創造並部署."""
    agent_id = kwargs.get("agent_id", "Unknown")

    # 本工具自帶 skill_creation_approval_request 審批卡
    # （呈現 source_code 供審），已於 tool_executor 標記 _SELF_APPROVING_TOOLS 跳過
    # broker 雙重審批。互動會話走下方自帶審批卡；headless（heartbeat／派工，無人可審
    # 熱部署）→ 立即 fail-closed，**不**呼叫 request_approval 空等 180s（比照
    # request_skill_install；H1 headless 語意保持）。
    if not kwargs.get("_interactive", False):
        return (
            "Error: Deploying a new capability requires an interactive session (a human "
            "to review the generated source code and approve the hot-deploy); it cannot "
            "run in an unattended (heartbeat/dispatch) turn. Ask the owner to approve it "
            "from a chat session."
        )

    if not EvolutionPathGuard.tool_file_exists(skill_id, "server.py"):
        return "Error: No implementation found. Run 'write_mcp_implementation' first."
        
    try:
        await asyncio.to_thread(_deployment_config, skill_id)
        source_code = EvolutionPathGuard.read_tool_file(skill_id, "server.py")
        if source_code is None:
            return _evolution_refused("skill_changed")
        digest = ()
        try:
            contents = await asyncio.to_thread(_deployment_files, skill_id)
            source_code = contents[0].decode("utf-8")
            digest = _approval_digest(contents)
        except EvolutionRefused as exc:
            # 預覽後檔案消失仍保留身分審批；沒有完整快照的核准永遠不能部署。
            if not isinstance(exc.__cause__, FileNotFoundError):
                raise
        spec_raw = EvolutionPathGuard.read_tool_file(skill_id, "spec.json") or "{}"
        spec = json.loads(spec_raw)
            
        # 1. 發起 HITL 審核
        exec_id = f"create_{skill_id}_{int(get_current_epoch())}"
        
        # DA19 fix: request_approval expects `identity: AgentIdentity`, NOT `agent_id`.
        # The old `agent_id=` kwarg raised TypeError (swallowed by the except below),
        # so this tool ALWAYS failed and could never deploy. Prefer the dispatcher's
        # real identity if present; otherwise build a minimal one (only agent_id is used).
        identity = kwargs.get("identity") or AgentIdentity(agent_id=agent_id)
        approved, _payload = await request_approval(
            exec_id=exec_id,
            identity=identity,
            command=f"Create Capability: {spec.get('name', skill_id)}",
            type="skill_creation_approval_request",
            extra_data={
                "skill_id": skill_id,
                "name": spec.get("name"),
                "description": spec.get("description"),
                "source_code": source_code,
                "is_creation": True
            },
            # H2：審批請求同步推進當前聊天 SSE（無通道時 fast-reject、不再空等）
            emit=kwargs.get("_emit_event"),
        )
        
        if not approved:
            return f"Rejected: User denied the creation of capability '{skill_id}'."
            
        # 2. 熱部署 (使用 config_utils 讀寫設定)；command＝建置與驗證用的同一支 venv Python
        server = EvolutionPathGuard.tool_zone(skill_id).child("server.py")
        entry = {
            "command": _venv_python(skill_id),
            "args": [resource_manager.zone_text(server)],
            "enabled": True
        }
        
        await asyncio.to_thread(_save_deployment, skill_id, digest, entry)

        # 3. 動態掛載
        pm = (kwargs.get("_ctx") or {}).get("protocol_manager")
        started = bool(pm and await pm.add_server(skill_id, ProtocolServerConfig(**entry)))
        state = (
            "started" if started
            else "saved; not started, check MCP settings for isolation status"
        )
        return f"Evolution Success: Capability '{spec.get('name')}' {state}."

    except EvolutionRefused as exc:
        return _evolution_refused(exc.reason)
    except Exception as e:
        logger.exception("🔥 Creation approval error")
        return f"Error: {str(e)}"


# ── 演化管線 4→2（提案 §7 最後一項；狀態機驗序）────────────────────────────────
# 舊四步保留為可執行 shim（不再廣告）；新兩顆為薄編排層——零重複邏輯，
# 沙盒／監獄／HITL 部署審核全部沿用既有實作。

# write_mcp_implementation 的舊導覽尾句：合併輸出必須剝除（指向未廣告的舊名
# ＝幻覺誘因）；shim 直呼（legacy 流程）時保留原樣。
_LEGACY_WRITE_TAIL = "\nNext, use 'verify_mcp_locally' to test your tool."
# 委派時剝除的重疊鍵（模型幻覺多給也不炸 TypeError；schema 具名參數為準）。
_MERGED_PARAM_KEYS = frozenset({
    "skill_id", "code", "dependencies", "test_tool_name", "test_arguments",
    "submit_for_deploy", "tool_name", "arguments", "name", "description", "schema",
})


def _clean_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """剝除與委派參數重疊的鍵，保留注入脈絡（identity／agent_id／配額覆寫等）。"""
    return {k: v for k, v in kwargs.items() if k not in _MERGED_PARAM_KEYS}


async def evolution_define(
    skill_id: str,
    name: str,
    description: str,
    schema: dict[str, Any],
    **kwargs: Any,
) -> str:
    """演化第一段：提案新能力（委派既有 propose_new_capability）。"""
    return await propose_new_capability(
        skill_id=skill_id,
        name=name,
        description=description,
        schema=schema,
        **_clean_kwargs(kwargs),
    )


@_skill_operation
async def evolution_implement_and_verify(
    skill_id: str,
    code: str,
    test_tool_name: str,
    test_arguments: dict[str, Any],
    dependencies: list[str] | None = None,
    submit_for_deploy: bool = False,
    **kwargs: Any,
) -> str:
    """演化第二段：實作＋沙盒驗證一次完成（狀態機驗序），選擇性提交部署.

    順序保證：
      1. 未 ``evolution_define``（無 spec.json）→ 可讀 validation_error、不動任何檔案。
      2. 環境建置失敗 → 立即回報、不執行驗證。
      3. ``submit_for_deploy=True`` 僅在**本次驗證通過**時觸發既有 HITL 部署
         （request_mcp_creation：使用者審核 → 熱部署）；驗證未過絕不部署。
      4. 隔離 grant 在開頭取一次，建 venv、pip、驗證共用；拿不到隔離就不執行（結構化錯誤）。
    """
    grant = await _isolation_grant(
        "evolution_implement_and_verify",
        {
            "skill_id": skill_id,
            "code": code,
            "dependencies": dependencies,
            "test_tool_name": test_tool_name,
            "test_arguments": test_arguments,
        },
        kwargs,
    )
    if not EvolutionPathGuard.tool_file_exists(skill_id, "spec.json"):
        return tool_errors.build_tool_error(
            tool_errors.VALIDATION_ERROR,
            f"No proposal found for '{skill_id}' — the evolution pipeline is "
            "define → implement_and_verify.",
            suggestion=(
                "Call evolution_define(skill_id, name, description, schema) first."
            ),
            param="skill_id",
        )

    # 同一次呼叫的 grant 交給兩個步驟（模型給的同名值會被這個物件蓋掉）
    clean = {**_clean_kwargs(kwargs), "_evolution_grant": grant}

    impl = str(
        await write_mcp_implementation(
            skill_id=skill_id, code=code, dependencies=dependencies, **clean
        )
    )
    if impl.startswith("Error") or tool_errors.is_tool_error(impl):
        return impl  # 環境建置失敗或沒有隔離 → 不驗證（狀態機）
    impl = impl.replace(_LEGACY_WRITE_TAIL, "").rstrip()

    outcome = await _verify_mcp(
            skill_id=skill_id,
            tool_name=test_tool_name,
            arguments=test_arguments,
            **clean,
    )
    verify = outcome.text
    if tool_errors.is_tool_error(verify):
        return verify  # 驗證時拿不到隔離：沒有執行，也不部署
    passed = outcome.passed
    sections = [impl, verify.rstrip()]

    if submit_for_deploy:
        if passed:
            sections.append(
                str(await request_mcp_creation(skill_id=skill_id, **clean))
            )
        else:
            sections.append(
                "🛑 Deployment not requested: verification did not pass — "
                "fix the code and re-run."
            )
    elif passed:
        sections.append(
            "Verification passed. Re-run with submit_for_deploy=true to request "
            "deployment (user approval required)."
        )
    return "\n\n".join(sections)
