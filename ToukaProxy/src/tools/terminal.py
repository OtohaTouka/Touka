"""終端機指令執行閘道 (Terminal Command Gateway).

負責在 Agent 的私有沙盒 (personal_drive) 中執行物理系統指令。
支援 Windows (PowerShell) 與 Linux (WSL Bash) 環境切換，具備路徑自動轉譯與
人機協商 (Human-In-The-Loop) 安全審核機制。

ADR-0119 遞延項落地：``bash`` → ``execute_command`` **非同步審批**——CONFIRM 不再
阻塞 ReAct 迴圈（原同步等待最多 180 秒），改為立即回傳 ``approval_pending``（含
execution_id），審批等待與核准後執行移入背景任務；``get_command_result`` 取回結果
（依 agent 隔離）。``bash`` 保留為 shim（可執行、不再廣告、同享非同步語義）。
審批端點／SSE／前端（/terminal/approve、bash_approval_request 事件）完全不變。
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from src.config import UI_DEFAULT_URL, get_app_data_dir
from src.models.identity import AgentIdentity  # noqa: TC001, TC002
from src.services import tool_errors
from src.services.evolution_security import EvolutionPathGuard
from src.services.execution_broker import spawn_exec
from src.services.isolation.agent_command import (
    CommandScope,
    command_scope,
    prepare_command,
    take_grant,
)
from src.services.isolation.policy import session_project_root
from src.services.isolation.runners import (
    ISOLATION_UNAVAILABLE,
    IsolationUnavailableError,
    executor_max_seconds,
)
from src.services.permission_service import permission_service
from src.services.process_registry import (
    process_registry,
    reap_cancelled_process,
    reap_process_tree,
    terminate_process_tree,
)
from src.tools.execution_context import APPROVED_BY_AUTO_REVIEWER
from src.utils.command_safety import (
    RUNNABLE_STRATEGIES,
    effective_shell,
    has_chaining_metacharacters,
)
from src.utils.isolation_policy import IsolationLevel
from src.utils.time_utils import get_current_epoch
from src.utils.tool_events import tool_stream_event

logger = logging.getLogger(__name__)


def _has_chaining_metacharacters(command: str) -> bool:
    """C5 串接字元偵測（SSOT：``src.utils.command_safety``）。"""
    return has_chaining_metacharacters(command)


def _classify_command_strategy(
    command: str,
    identity: AgentIdentity,
    auth_level: str = "Manual",
    isolation: IsolationLevel | None = None,
) -> str:
    """終端指令政策（SSOT：``permission_service.classify_command``，含 C5 升級）。

    ``isolation`` 是這次執行取回的 grant 層級；沒給才退回代理人層級（全域預設）。
    """
    return permission_service.classify_command(command, identity, auth_level, isolation=isolation)


def _get_agent_drive_path(identity: AgentIdentity) -> Path:
    """獲取 Agent 的專屬硬碟物理路徑.

    Args:
        identity: 經驗證的身分對象.

    Returns:
        Path: 解析後的絕對路徑實體.
    """
    try:
        from src.services.resource_manager import resource_manager  # noqa: LOCAL_IMPORT_BAN
        path_str = resource_manager.get_vault_path("personal", identity)
        if path_str:
            return Path(path_str).resolve()
    except (ImportError, Exception):  # noqa: S110
        logger.debug("resource_manager unavailable, using fallback drive path")
    
    # Fallback to default structure
    base_data_dir = get_app_data_dir()
    return (Path(base_data_dir) / "personal_drive" / identity.agent_id).resolve()


def win_to_wsl_path(win_path: str) -> str:
    r"""將 Windows 絕對路徑轉譯為 WSL 格式的 Linux 路徑.

    例如: 'C:\\Workspace' -> '/mnt/c/Workspace'

    Args:
        win_path (str): 原始 Windows 路徑字串.

    Returns:
        str: 轉譯後的 WSL 相容路徑.
    """
    path = win_path.replace("\\", "/")
    if len(path) > 1 and path[1] == ":":
        drive = path[0].lower()
        rest = path[2:]
        return f"/mnt/{drive}{rest}"
    return path


def _is_touka_ui_available() -> bool:
    """診斷 ToukaUI 是否有活躍的 SSE 訂閱者.

    Returns:
        bool: 若有至少一個訂閱者則為 True.
    """
    try:
        from src.routers.terminal import sse_subscribers
        return len(sse_subscribers) > 0
    except (ImportError, AttributeError):
        return False


# ── 非同步審批狀態（行程內；重啟即清） ────────────────────────────────────────
# {execution_id: {status, agent_id, command, result, _task}}
# status: approval_pending → running → done | rejected | error
_ASYNC_RESULTS: dict[str, dict[str, Any]] = {}
_MAX_ASYNC_RESULTS = 100
_RESULT_POLL_INTERVAL = 0.05
_MAX_WAIT_SECONDS = 60

# P2b 長指令 during 監控：
_FOREGROUND_TIMEOUT = 30      # 前景 inline 執行硬逾時（秒；行為不變）
_BACKGROUND_TIMEOUT = 600     # 背景執行 backstop 硬逾時（秒）＝兩層看門狗的最後防線
_STALL_THRESHOLD_S = 20       # 閒置（無新輸出）達此秒數 → 諮詢訊號 "stalled"
_RUNAWAY_BYTES = 256 * 1024   # 累積輸出達此位元組 → 諮詢訊號 "runaway_output"
_PARTIAL_TAIL_CHARS = 2000    # get_command_result 回傳的部分輸出尾段長度


def _tail(text: str, limit: int = _PARTIAL_TAIL_CHARS) -> str:
    """回傳字串尾段（部分輸出過長時只給末尾，前面標省略）。"""
    if len(text) <= limit:
        return text
    return "…(truncated)…\n" + text[-limit:]


def reset_async_state_for_test() -> None:
    """測試隔離：取消未完成的背景任務並清空結果存放區。"""
    for record in _ASYNC_RESULTS.values():
        task = record.get("_task")
        if task is not None and not task.done():
            task.cancel()
    _ASYNC_RESULTS.clear()


def _remember_result(exec_id: str, record: dict[str, Any]) -> None:
    """寫入結果存放區並維持上限（FIFO 淘汰最舊）。"""
    _ASYNC_RESULTS[exec_id] = record
    while len(_ASYNC_RESULTS) > _MAX_ASYNC_RESULTS:
        oldest = next(iter(_ASYNC_RESULTS))
        _ASYNC_RESULTS.pop(oldest, None)


def _pending_payload(exec_id: str, command: str, note: str) -> str:
    """approval_pending 的結構化回覆（非錯誤；executor 不注入時間尾綴）。"""
    return json.dumps(
        {
            "status": "approval_pending",
            "execution_id": exec_id,
            "command": command,
            "message": note,
            "next_step": (
                f"You may continue with other work; call "
                f"get_command_result(execution_id='{exec_id}') later to fetch the outcome."
            ),
        },
        ensure_ascii=False,
    )


def _running_handle_payload(exec_id: str, command: str) -> str:
    """背景執行的 running handle（P2b；非錯誤——executor 不注入時間尾綴）。"""
    return json.dumps(
        {
            "status": "running",
            "execution_id": exec_id,
            "command": command,
            "message": "Started in the background; it is running now and did NOT block you.",
            "next_step": (
                f"Call get_command_result(execution_id='{exec_id}') to see progress and "
                f"partial output (with stall/runaway signals), or "
                f"stop_command(execution_id='{exec_id}') to terminate it. Nothing is auto-killed."
            ),
        },
        ensure_ascii=False,
    )


def _isolation_unavailable(command: str, reason: str) -> str:
    """拿不到可用的隔離 runner：指令沒有執行（upstream_error，原因 isolation_unavailable）。"""
    return tool_errors.build_tool_error(
        tool_errors.UPSTREAM_ERROR,
        f"Command was not run: no isolated execution environment is available: {command}",
        suggestion=(
            "Do not run it another way; tell the user isolation is unavailable "
            f"({reason}) and ask them to approve it in the chat or fix the deployment."
        ),
        reason=ISOLATION_UNAVAILABLE,
        isolation_reason=reason,
    )


def _running_signals(record: dict[str, Any]) -> list[str]:
    """由 record 計算諮詢訊號（P2b）——**僅供模型判斷、絕不自動 kill**。

    - ``stalled``：閒置（無新輸出）≥ 門檻 → 疑似掛住／等輸入。
    - ``runaway_output``：累積輸出 ≥ 門檻 → 疑似無窮迴圈狂噴。
    """
    now = get_current_epoch()
    signals: list[str] = []
    last_out = record.get("last_output_at", record.get("started_at", now))
    if now - last_out >= _STALL_THRESHOLD_S:
        signals.append("stalled")
    output = record.get("output", "")
    if len(output.encode("utf-8", "ignore")) >= _RUNAWAY_BYTES:
        signals.append("runaway_output")
    return signals


def _running_status_payload(execution_id: str, record: dict[str, Any]) -> str:
    """Running 狀態的結構化回覆：部分輸出＋諮詢訊號＋耗時／閒置秒數（P2b）。"""
    now = get_current_epoch()
    started = record.get("started_at", now)
    last_out = record.get("last_output_at", started)
    return json.dumps(
        {
            "status": "running",
            "execution_id": execution_id,
            "command": record.get("command", ""),
            "partial_output": _tail(record.get("output", "")),
            "signals": _running_signals(record),
            "elapsed_seconds": int(now - started),
            "idle_seconds": int(now - last_out),
            "message": (
                "Still running; nothing is auto-killed. You may keep waiting, poll again "
                f"later, or call stop_command(execution_id='{execution_id}') if the signals "
                "suggest it is stuck (stalled) or looping (runaway_output)."
            ),
        },
        ensure_ascii=False,
    )


async def _execute_core(
    command: str,
    env: str,
    drive_path: Path,
    session_id: str | None,
    emit: Any = None,
    call_id: str | None = None,
    timeout: int = _FOREGROUND_TIMEOUT,
    record: dict[str, Any] | None = None,
    *,
    scope: CommandScope | None = None,
) -> tuple[int, str, int]:
    """實體指令執行核心（非阻塞 async 子進程：可被 /stop 硬終止）。

    ``timeout`` 為硬逾時秒數（前景 30、背景 backstop 600）。``record`` 非 None（背景
    模式）時逐行寫入 ``output``／``last_output_at`` 並存 ``_proc``，供
    get_command_result 回部分輸出＋停滯偵測、stop_command 硬終止。
    spawn 前才依 ``scope`` 選隔離 runner（沒有 scope＝層級 none、沒有同意）；拿不到就拋
    ``IsolationUnavailableError``，不執行。回傳 ``(exit_code, output, duration_ms)``。
    """
    t_start = get_current_epoch()
    # Secret-leakage fix: child shells must NOT inherit Touka credentials
    # (TOUKA_AUTH_SECRET / DISPATCH_TOKEN / HEARTBEAT_TOKEN / provider API keys).
    # Reuse the sandbox SSOT clean-env (strips secret-named + runtime-control vars).
    clean_env = EvolutionPathGuard.get_clean_env()

    # 依平台/環境決定實際命令（三分支收斂為單一 async exec 呼叫）
    shell = effective_shell(env)
    if shell == "bash":
        cmd_args = ["/bin/bash", "-c", command]
    elif shell == "powershell":
        cmd_args = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command,
        ]
    elif shell == "wsl":
        cmd_args = ["wsl", "--cd", win_to_wsl_path(str(drive_path)), "bash", "-c", command]
    else:
        return (-1, f"Error: Unknown environment '{env}'. Supported: 'powershell', 'wsl'.", 0)
    prepared = await prepare_command(
        scope, cmd_args, clean_env, drive_path, max_seconds=executor_max_seconds(timeout)
    )
    plan = prepared.plan

    output_parts: list[str] = []
    exit_code = 0
    proc = None
    try:
        # P5d：以新行程群組啟動，讓逾時／`/stop` 能連坐殺掉衍生的孫進程（避免殘留孤兒）。
        # 群組 kwargs 由 ExecutionBroker 單一入口提供（ADR-0239）。
        try:
            proc = await spawn_exec(
                *plan.argv,
                new_process_group=True,
                cwd=None if shell == "wsl" else str(plan.cwd),  # wsl 以 --cd 指定工作目錄
                env=plan.env,
                pass_fds=plan.pass_fds,
                stdin=asyncio.subprocess.DEVNULL,  # 不繼承 proxy 的 stdin（TTY 時可 TIOCSTI）
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        finally:
            plan.release()  # spawn 成功或失敗都關掉 runner 為這次開的 fd
        # 登記到「執行中子進程登記表」，讓 /stop 能硬終止這個正在跑的指令
        process_registry.register(session_id, proc)
        # P2b：背景模式把 proc 存進 record，讓 stop_command 能硬終止「這一筆」執行
        if record is not None:
            record["_proc"] = proc

        async def _run() -> None:
            """逐行讀取 stdout（含合併的 stderr），即時 live 串流並收集完整輸出。"""
            assert proc is not None and proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                output_parts.append(text)
                # P2b：逐行 emit tool_stream，前端 ToolStepTracker 即時 append
                if emit and call_id:
                    await emit(tool_stream_event(call_id=call_id, chunk=text))
                # P2b：背景模式逐行更新 record，供 get_command_result 回部分輸出＋停滯偵測
                if record is not None:
                    record["output"] = "".join(output_parts)
                    record["last_output_at"] = get_current_epoch()
            await proc.wait()

        try:
            await asyncio.wait_for(_run(), timeout=timeout)
        finally:
            process_registry.unregister(session_id, proc)
        exit_code = proc.returncode if proc.returncode is not None else -1
    except asyncio.CancelledError:
        # 呼叫端取消（/stop、背景任務收束）時子進程不能被遺棄：在執行緒裡連坐殺整個行程
        # 群組並有界等待回收（Windows taskkill 是同步的，不能卡事件迴圈）。
        if proc is not None and proc.returncode is None:
            await reap_cancelled_process(proc, terminate_process_tree)
        prepared.report()  # 狀態 fd 的讀端照樣讀完關掉
        raise
    except TimeoutError:
        if proc is not None:
            # P5d：連坐殺整個行程群組（含孫進程）；在執行緒裡殺、有界等待回收，被取消時照樣拋出
            await reap_process_tree(proc, terminate_process_tree)
        output_parts.append(f"\nError: Command timed out after {timeout} seconds.")
        exit_code = -1
    except Exception as e:
        logger.exception("🔥 Terminal execution exception")
        output_parts.append(f"Unexpected Error: {str(e)}")
        exit_code = -1

    output = "".join(output_parts)
    prepared.report()
    hint = prepared.port_hint(exit_code, f"{command}\n{output}")
    if hint:
        output += f"\n{hint}\n"
    duration_ms = int((get_current_epoch() - t_start) * 1000)
    return (exit_code, output, duration_ms)


def _push_result_safely(
    exec_id: str,
    identity: AgentIdentity,
    command: str,
    exit_code: int,
    output: str,
    duration_ms: int,
) -> None:
    """廣播結果 SSE（Terminal 面板）；失敗僅記 debug。"""
    try:
        from src.routers.terminal import push_bash_result
        push_bash_result(exec_id, identity, command, exit_code, output, duration_ms)
    except (ImportError, Exception):
        logger.debug("Failed to push bash_result to SSE")


async def _approve_then_execute(
    exec_id: str,
    command: str,
    env: str,
    identity: AgentIdentity,
    drive_path: Path,
    session_id: str | None,
    scope: CommandScope,
) -> None:
    """背景任務：等待審批（沿用既有 request_approval），核准才執行並存結果。"""
    record = _ASYNC_RESULTS.get(exec_id)
    if record is None:  # 已被淘汰／重置
        return
    try:
        import src.routers.terminal as terminal_router
        approved, _payload = await terminal_router.request_approval(
            exec_id, identity, command
        )
        if not approved:
            record["status"] = "rejected"
            record["result"] = tool_errors.build_tool_error(
                tool_errors.PERMISSION_DENIED,
                f"Command was rejected by the user or the approval timed out: {command}",
                suggestion=(
                    "Do not retry the same command; explain the situation to the user "
                    "or propose a safer alternative."
                ),
                execution_id=exec_id,
            )
            _push_result_safely(exec_id, identity, command, -1, "rejected or timed out", 0)
            return

        record["status"] = "running"
        exit_code, output, duration_ms = await _execute_core(
            command, env, drive_path, session_id, scope=scope
        )
        _push_result_safely(exec_id, identity, command, exit_code, output, duration_ms)
        header = f"[EXIT_CODE: {exit_code}]\n"
        record["status"] = "done"
        record["result"] = _wrap_output(
            command,
            header + output,
            exit_code=exit_code,
            duration_ms=duration_ms,
            dangerous=True,
            _approved=True,
        )
    except asyncio.CancelledError:
        raise
    except IsolationUnavailableError as exc:
        record["status"] = "error"
        record["result"] = _isolation_unavailable(command, exc.reason)
    except Exception as exc:
        logger.exception("🔥 Async approval/execution failed for %s", exec_id)
        record["status"] = "error"
        record["result"] = tool_errors.from_exception(exc, "async command execution")


async def _run_background(
    exec_id: str,
    command: str,
    env: str,
    identity: AgentIdentity,
    drive_path: Path,
    session_id: str | None,
    dangerous: bool = False,
    scope: CommandScope | None = None,
) -> None:
    """背景任務：執行長指令、逐步更新 record（不阻塞；backstop 硬逾時 600s＝最後防線）。

    dangerous：透傳危險旗標至完成卡 ``_wrap_output``——互動 CONFIRM 經統一 broker 預核准
    後的背景執行（ADR-0143 H2 × ADR-0125）須於完成卡保留 ⚠️ [危險] 徽章（觀測面誠實、
    與前景/legacy 路徑一致；審批已於 tool_executor Gate-2 前置取得，不影響執法）。
    """
    record = _ASYNC_RESULTS.get(exec_id)
    if record is None:  # 已被淘汰／重置
        return
    try:
        exit_code, output, duration_ms = await _execute_core(
            command,
            env,
            drive_path,
            session_id,
            timeout=_BACKGROUND_TIMEOUT,
            record=record,
            scope=scope,
        )
        if record.get("status") == "stopped":
            return  # 已被 stop_command 終止並定案 → 勿覆寫
        _push_result_safely(exec_id, identity, command, exit_code, output, duration_ms)
        header = f"[EXIT_CODE: {exit_code}]\n"
        record["status"] = "done"
        record["result"] = _wrap_output(
            command,
            header + output,
            exit_code=exit_code,
            duration_ms=duration_ms,
            dangerous=dangerous,
            _approved=True,
        )
    except asyncio.CancelledError:
        raise
    except IsolationUnavailableError as exc:
        record["status"] = "error"
        record["result"] = _isolation_unavailable(command, exc.reason)
    except Exception as exc:
        logger.exception("🔥 Background execution failed for %s", exec_id)
        if record.get("status") != "stopped":
            record["status"] = "error"
            record["result"] = tool_errors.from_exception(exc, "background command execution")


async def execute_command(
    command: str,
    identity: AgentIdentity,
    env: str = "powershell",
    background: bool = False,
    **kwargs: Any,
) -> str:
    """終端機指令執行（Identity & Policy Aware、**非同步審批**）.

    安全指令（ALLOW）立即執行並回傳結果；危險指令（CONFIRM）**不阻塞**——立即
    回傳 ``approval_pending``（含 execution_id），審批與執行移入背景任務，之後以
    ``get_command_result`` 取回。DENY（含未知決策）與「無 UI 相容模式」回結構化 permission_denied。

    Args:
        command: 要執行的完整的 Shell 指令.
        identity: 執行者的 Agent 身份物件.
        env: 執行環境類型 ('powershell' 或 'wsl')。預設為 'powershell'.
        background: True＝長指令背景執行、立即回 running handle 不阻塞（P2b）。
        **kwargs (Any): 其他參數，包含 is_toukasoul (核心標記) 與 is_custom.

    Returns:
        str: 執行結果 Markdown、approval_pending JSON 或結構化錯誤.
    """
    drive_path = _get_agent_drive_path(identity)
    drive_path.mkdir(parents=True, exist_ok=True)

    # --- 隔離：grant 用 call_id 與正規化 subject 取回；專案根只來自 session（不讀 project_id）---
    session_id = kwargs.get("session_id")
    grant = take_grant(
        identity,
        session_id,
        kwargs.get("_call_id"),
        tool="execute_command",
        args={"command": command, "env": env},
    )
    project_root = await session_project_root(kwargs.get("_ctx") or {}, session_id)
    scope = await command_scope(identity, project_root, grant)

    # --- 🛡️ 統一安全政策核查 (SSOT Permission Gate) ---
    auth_level = kwargs.get("auth_level", "Manual")
    # 獲取安全決策 (ALLOW, CONFIRM, DENY) — C5: metachar escalation included
    strategy = _classify_command_strategy(
        command, identity, auth_level, isolation=scope.grant.policy_level
    )

    # 只有 ALLOW／CONFIRM 可往下走；DENY 與任何未知值一律拒絕（直接呼叫端不經 Gate 2）。
    # HUMAN_ONLY（硬地板，ADR-0246）只認 executor 經人類審批卡核准後設下的可信
    # _hitl_preapproved；審查員核准（U2c-3）不得滿足 HUMAN_ONLY。
    # REVIEW（Auto）：人類卡核准，或 executor 設下的可信 _approved_by（審查員放行）才跑；
    # 這裡只重新分類（地板、黑名單、模式），不再呼叫審查員。
    preapproved = kwargs.get("_hitl_preapproved") is True
    reviewer_approved = (
        strategy == "REVIEW" and kwargs.get("_approved_by") == APPROVED_BY_AUTO_REVIEWER
    )
    human_approved = strategy in ("HUMAN_ONLY", "REVIEW") and preapproved
    if strategy not in RUNNABLE_STRATEGIES and not (human_approved or reviewer_approved):
        return tool_errors.build_tool_error(
            tool_errors.PERMISSION_DENIED,
            f"Command blocked by security policy (dangerous operation): {command}",
            suggestion="Use the file verbs (read/write/edit) or propose a safer command.",
            reason="policy_block",
        )

    dangerous = strategy != "ALLOW"
    exec_id = str(uuid.uuid4())[:8]

    # 1. 廣播 bash_start 事件 (UI 同步；事件名沿用、前端零改動)
    try:
        from src.routers.terminal import push_bash_start
        push_bash_start(exec_id, identity, command, dangerous)
    except (ImportError, Exception):
        logger.warning(f"Failed to broadcast bash_start for {exec_id}")

    # 2. 危險指令 → 非同步審核（不阻塞 ReAct 迴圈）
    # H2（hitl-unification）：互動會話的 CONFIRM 已由統一 broker（聊天內審批卡）
    # 在 Gate-2 取得人類核准並帶 _hitl_preapproved=True 進來——跳過本段自帶審批
    # （遺留 ToukaUI 通道），直接依 background/前景正常執行；dangerous 標記保留
    # （bash_start 事件與輸出 meta 的事實）。headless 情境維持遺留通道語意。
    if dangerous and not (kwargs.get("_hitl_preapproved") or reviewer_approved):
        if scope.grant.policy_level == "none":
            # 舊通道的卡片顯示不出「未隔離」、也記不了同意：none 時直接拒絕
            return _isolation_unavailable(command, "legacy_approval_channel")
        if not _is_touka_ui_available():
            return tool_errors.build_tool_error(
                tool_errors.PERMISSION_DENIED,
                f"Dangerous command requires human approval, but no ToukaUI is "
                f"connected to approve it: {command}",
                suggestion=(
                    f"Ask the user to run it manually in a terminal, or open "
                    f"ToukaUI ({UI_DEFAULT_URL}) to enable the approval flow."
                ),
                reason="ui_unavailable",
            )

        record: dict[str, Any] = {
            "status": "approval_pending",
            "agent_id": identity.agent_id,
            "command": command,
            "result": None,
        }
        _remember_result(exec_id, record)
        record["_task"] = asyncio.create_task(
            _approve_then_execute(exec_id, command, env, identity, drive_path, session_id, scope)
        )
        return _pending_payload(
            exec_id,
            command,
            "Dangerous command is awaiting human approval in ToukaUI; "
            "it has NOT run yet.",
        )

    # 3. 安全指令 + background：背景執行、立即回 running handle（不阻塞長指令）
    if background:
        now = get_current_epoch()
        bg_record: dict[str, Any] = {
            "status": "running",
            "agent_id": identity.agent_id,
            "command": command,
            "result": None,
            "output": "",
            "started_at": now,
            "last_output_at": now,
            "_proc": None,
        }
        _remember_result(exec_id, bg_record)
        bg_record["_task"] = asyncio.create_task(
            _run_background(
                exec_id, command, env, identity, drive_path, session_id, dangerous, scope
            )
        )
        return _running_handle_payload(exec_id, command)

    # 4. 安全指令（前景）：立即執行（行為與舊 bash 相同）
    try:
        exit_code, output, duration_ms = await _execute_core(
            command,
            env,
            drive_path,
            session_id,
            emit=kwargs.get("_emit_event"),
            call_id=kwargs.get("_call_id"),
            scope=scope,
        )
    except IsolationUnavailableError as exc:
        return _isolation_unavailable(command, exc.reason)
    _push_result_safely(exec_id, identity, command, exit_code, output, duration_ms)
    header = f"[EXIT_CODE: {exit_code}]\n"
    return _wrap_output(
        command,
        header + output,
        exit_code=exit_code,
        duration_ms=duration_ms,
        dangerous=dangerous,
        _approved=True,
    )


async def get_command_result(
    execution_id: str,
    wait_seconds: int = 0,
    **kwargs: Any,
) -> str:
    """取回非同步指令的審批／執行結果（依 agent 隔離）.

    Args:
        execution_id: execute_command 回傳的執行識別碼.
        wait_seconds: 最多等待秒數（0=只查詢不等待；上限 60）.
        **kwargs (Any): 注入脈絡（identity 用於結果隔離）.

    Returns:
        str: 完成＝執行結果；等待中＝結構化 status JSON；拒絕/未知＝結構化錯誤.
    """
    identity = kwargs.get("identity")
    agent_id = getattr(identity, "agent_id", None)
    record = _ASYNC_RESULTS.get(execution_id)
    if record is None or record.get("agent_id") != agent_id:
        # 跨身分不洩漏存在性：一律 not found
        return tool_errors.build_tool_error(
            tool_errors.RESOURCE_NOT_FOUND,
            f"No pending or finished command found for execution_id '{execution_id}'.",
            suggestion="Use the execution_id returned by execute_command in this session.",
            resource_type="command_execution",
        )

    # 有界等待（避免又變回阻塞工具：上限 60 秒、預設不等待）
    remaining = min(max(int(wait_seconds), 0), _MAX_WAIT_SECONDS)
    polls = int(remaining / _RESULT_POLL_INTERVAL)
    for _ in range(polls):
        if record["status"] not in ("approval_pending", "running"):
            break
        await asyncio.sleep(_RESULT_POLL_INTERVAL)

    status = record["status"]
    if status == "approval_pending":
        return _pending_payload(
            execution_id, record["command"], "Still awaiting human approval in ToukaUI."
        )
    if status == "running":
        return _running_status_payload(execution_id, record)
    return str(record.get("result") or "")


async def stop_command(execution_id: str, **kwargs: Any) -> str:
    """模型面終止一筆執行中／待審批的指令（P2b；掛既有 terminate_process_tree、依 agent 隔離）.

    兩層看門狗的「模型 peek-and-decide」出口：模型看了 get_command_result 的訊號後，
    **自行決定**是否終止（系統絕不自動 kill）。

    Args:
        execution_id: execute_command 回傳的執行識別碼.
        **kwargs (Any): 注入脈絡（identity 用於隔離）.

    Returns:
        str: 結構化成功（已終止／已完成無需終止）或結構化 resource_not_found（未知／跨身分）.
    """
    identity = kwargs.get("identity")
    agent_id = getattr(identity, "agent_id", None)
    record = _ASYNC_RESULTS.get(execution_id)
    if record is None or record.get("agent_id") != agent_id:
        # 跨身分不洩漏存在性：一律 not found
        return tool_errors.build_tool_error(
            tool_errors.RESOURCE_NOT_FOUND,
            f"No running command found for execution_id '{execution_id}'.",
            suggestion="Use the execution_id returned by execute_command in this session.",
            resource_type="command_execution",
        )

    status = record.get("status")
    if status not in ("running", "approval_pending"):
        # 已完成／被拒／已停止：無需終止（結構化成功、附最終狀態）
        return tool_errors.build_tool_success(
            f"Command '{execution_id}' is already '{status}'; nothing to stop.",
            execution_id=execution_id,
            final_status=status,
        )

    # 先同步定案為 stopped（附已收集的部分輸出），再硬終止——避免背景任務覆寫。
    record["status"] = "stopped"
    record["result"] = tool_errors.build_tool_success(
        f"Command stopped by request: {record.get('command', '')}",
        preview=_tail(record.get("output", "")) or None,
        execution_id=execution_id,
    )
    task = record.get("_task")
    if task is not None and not task.done():
        # 待審批：取消審批等待；執行中：收束背景任務。先取消（不讓出事件迴圈），
        # 背景任務就不會在下面等殺行程時把已定案的 stopped 覆寫成 done。
        task.cancel()
    proc = record.get("_proc")
    if proc is not None:
        # 連坐殺行程樹（含孫進程）；移到執行緒，Windows 同步 taskkill 不卡事件迴圈
        await asyncio.to_thread(terminate_process_tree, proc)
    return record["result"]


async def bash(
    command: str,
    identity: AgentIdentity,
    env: str = "powershell",
    **kwargs: Any,
) -> str:
    """舊名 shim（雙軌）：委派 :func:`execute_command`（同享非同步審批語義）。"""
    return await execute_command(command=command, identity=identity, env=env, **kwargs)


def _wrap_output(
    command: str,
    output: str,
    exit_code: int = 0,
    duration_ms: int = 0,
    dangerous: bool = False,
    _approved: bool = True,
) -> str:
    """包裝視覺化的 Markdown 輸出 (支援摺疊細節).

    Args:
        command (str): 原指令.
        output (str): 執行輸出內容.
        exit_code (int): 結束代碼.
        duration_ms (int): 總耗時.
        dangerous (bool): 是否為危險指令.
        _approved (bool): 是否已核准執行.

    Returns:
        str: 格式化後的 Markdown 字串.
    """
    status_icon = "✅" if exit_code == 0 else "❌"
    danger_badge = " ⚠️ [危險]" if dangerous else ""
    summary = f"{status_icon}{danger_badge} bash: `{command}` (exit {exit_code}, {duration_ms}ms)"

    return (
        f"<details>\n"
        f"<summary>🖥️ {summary}</summary>\n\n"
        f"```\n{output.strip()}\n```\n\n"
        f"</details>"
    )


# --- [Schema 定義] ---

EXECUTE_COMMAND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "execute_command",
        "description": (
            "指令執行：在個人硬碟沙盒中執行 PowerShell 或 WSL 指令。安全指令立即"
            "執行並回結果；危險指令回 approval_pending（含 execution_id）——不會"
            "卡住你，可先做其他事，之後用 get_command_result 取結果（核准與否由"
            "使用者在 ToukaUI 決定）。長時間指令（測試套件／建置）設 background=true "
            "於背景執行、立即回 running（含 execution_id）不阻塞你——之後用 "
            "get_command_result 看進度與部分輸出、必要時 stop_command 終止。何時不要"
            "用：單純讀寫檔案請優先用檔案動詞（read／write／edit 等——更安全、可審計、免核准）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要執行的完整指令字串"},
                "env": {
                    "type": "string",
                    "enum": ["powershell", "wsl"],
                    "default": "powershell",
                    "description": "執行環境",
                },
                "background": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "長時間指令設 true：背景執行、立即回 running handle 不阻塞；"
                        "用 get_command_result 追蹤、stop_command 終止"
                    ),
                },
            },
            "required": ["command"],
        },
    },
}

GET_COMMAND_RESULT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_command_result",
        "description": (
            "取回 execute_command 的非同步／背景結果：尚待核准＝approval_pending、執行中"
            "＝running（回 partial_output 部分輸出＋signals 諮詢訊號 stalled/runaway_output"
            "＋elapsed/idle 秒數——據此判斷是否 stop_command，系統不會自動 kill）、完成＝"
            "完整輸出、被拒＝permission_denied。預設立即回覆；wait_seconds 可有界等待"
            "（上限 60 秒）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "execution_id": {
                    "type": "string",
                    "description": "execute_command 回傳的執行識別碼",
                },
                "wait_seconds": {
                    "type": "integer",
                    "description": "最多等待秒數（0=只查詢；上限 60）",
                },
            },
            "required": ["execution_id"],
        },
    },
}

STOP_COMMAND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "stop_command",
        "description": (
            "終止一筆執行中或待審批的指令（用 execute_command 回的 execution_id）。"
            "用於 get_command_result 顯示 stalled（停滯）或 runaway_output（狂噴、疑似"
            "無窮迴圈）時由你決定收手；已完成的指令則回報無需終止。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "execution_id": {
                    "type": "string",
                    "description": "要終止的執行識別碼",
                },
            },
            "required": ["execution_id"],
        },
    },
}

# 舊名 shim 的形狀文件（雙軌：可執行、不再廣告——見 schemas.LEGACY_SHIM_SCHEMAS）。
BASH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "（已由 execute_command 取代）指令執行：在個人硬碟沙盒中執行 "
            "PowerShell 或 WSL 指令。"
        ),
        "parameters": EXECUTE_COMMAND_SCHEMA["function"]["parameters"],
    },
}
