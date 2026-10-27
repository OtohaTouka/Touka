"""Global pytest configuration and fixtures for Touka."""

import atexit
import importlib
import logging
import os
import shutil
import sys
import tempfile
from concurrent.futures import Executor
from contextlib import suppress
from functools import partial
from pathlib import Path

import pytest

from tests import network_guard
from tests.live_api_policy import PROVIDER_CREDENTIAL_ENV_VARS
from tests.sandbox_cleanup import ReleaseStep, remove_session_sandbox

# --- 0. 外部網路封鎖（ADR-0214 第二層保險）：先於任何 src import 掛上 audit hook ---
network_guard.install()

# --- 1. 路徑掛載 (SSOT: 統一路徑導航) ---
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"
HEARTBEAT_DIR = PROJECT_ROOT / "ToukaHeartbeat"
DISPATCH_DIR = PROJECT_ROOT / "ToukaDispatch"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(MEMORY_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_SRC_DIR))
if str(HEARTBEAT_DIR) not in sys.path:
    sys.path.insert(0, str(HEARTBEAT_DIR))
# ToukaDispatch appended (lower priority to avoid config.py naming collision with heartbeat)
if str(DISPATCH_DIR) not in sys.path:
    sys.path.append(str(DISPATCH_DIR))

# --- 2. 全域沙盒化保護 (SSOT: 資料主權隔離) ---
# 必須先於任何 src import 建立；空白 sandbox env file 與後續 credential 清空會隔離
# host provider keys。
# 收尾在 mkdtemp 後立刻登記到 atexit：設定階段就結束的程序（--basetemp 被拒、--help、
# 收集錯誤）不會跑 session fixture 或 sessionfinish。釋放步驟在產品匯入後才補進清單。
SANDBOX_RELEASE_STEPS: list[ReleaseStep] = []
SANDBOX_RESIDUE_MESSAGE = "測試沙盒沒刪乾淨："
_sandbox_residue: list[Path] = []
_SANDBOX_OWNER_PID = os.getpid()


def _cleanup_session_sandbox() -> Path | None:
    """釋放握住沙盒的 handle 後刪掉它；記下刪不掉的路徑給結尾摘要。可重複呼叫。"""
    residue = remove_session_sandbox(_SESSION_TMP_ROOT, SANDBOX_RELEASE_STEPS)
    _sandbox_residue[:] = [residue] if residue else []
    return residue


def _cleanup_session_sandbox_at_exit() -> None:
    """fork 出的子程序會繼承 atexit；只有建立沙盒的程序能刪它。"""
    if os.getpid() == _SANDBOX_OWNER_PID and _cleanup_session_sandbox():
        sys.stderr.write(f"{SANDBOX_RESIDUE_MESSAGE}{_SESSION_TMP_ROOT}\n")


SESSION_TMP_DATA_ROOT = tempfile.mkdtemp(prefix="touka_test_session_")
_SESSION_TMP_ROOT = Path(SESSION_TMP_DATA_ROOT)
atexit.register(_cleanup_session_sandbox_at_exit)
_SESSION_ENV_FILE = _SESSION_TMP_ROOT / ".env"
_SESSION_ENV_FILE.write_text(
    "# pytest sandbox: live provider credentials intentionally absent\n",
    encoding="utf-8",
)
os.environ["MEMORY_APP_DATA_DIR"] = SESSION_TMP_DATA_ROOT
os.environ["SANCTUARY_DATA_PATH"] = str(_SESSION_TMP_ROOT / "data")
os.environ["TOUKA_CONFIG_DIR"] = str(_SESSION_TMP_ROOT / "config")
os.environ["TOUKA_ENV_FILE"] = str(_SESSION_ENV_FILE)
os.environ["HEARTBEAT_CONFIG_FILE"] = str(_SESSION_TMP_ROOT / "heartbeat-config.json")
os.environ["TOUKA_BOOTSTRAP_ENABLED"] = "false"
os.environ["QDRANT_FORCE_DISABLE_MMAP"] = "true"
os.environ["DISPATCH_AUTOSPAWN"] = "false"

# Heartbeat 的延後 FileHandler 不得在測試首個 log event 開啟 repo 日誌。
# 保留 pytest capture 或已存在的 logging 接線；只補沒有 handler 的情況。
if not logging.getLogger().handlers:
    logging.getLogger().addHandler(logging.NullHandler())


# 外部／計量功能在任何 src import 前強制關閉；host env 不得把 pytest 變成 live run。
def _enforce_pytest_metered_features_off() -> None:
    """Keep every automated test boundary free of live provider credentials."""
    os.environ["TOUKA_EVAL_JUDGE_ENABLED"] = "false"
    os.environ["ENABLE_DYNAMIC_MODELS"] = "false"
    for _offline_flag in ("HF_HUB_OFFLINE", "HF_DATASETS_OFFLINE", "TRANSFORMERS_OFFLINE"):
        os.environ[_offline_flag] = "1"
    network_guard.clear_proxy_environment(os.environ)
    for _credential_name in PROVIDER_CREDENTIAL_ENV_VARS:
        os.environ[_credential_name] = ""


_enforce_pytest_metered_features_off()

from pytest_basetemp_plugin import pytest_configure as pytest_configure  # noqa: E402
from src.core.bootstrap import hydrate_config_examples  # noqa: E402

# ADR-0082：測試環境絕不自動 spawn dispatch 子程序。個別整合測試以
# monkeypatch.setattr(settings, "DISPATCH_AUTOSPAWN", True) 搭配 mock 開啟。

# 在匯入任何系統模組前先確保 config 與 data 目錄已經建立以落實 Sandbox 隔離
Path(os.environ["TOUKA_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
hydrate_config_examples(PROXY_DIR / "config", Path(os.environ["TOUKA_CONFIG_DIR"]))
(Path(SESSION_TMP_DATA_ROOT) / "data" / "soul").mkdir(parents=True, exist_ok=True)

import copy  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from memory_server.factual import FactualStore  # noqa: E402
from memory_server.services.mission_lock import _reset_mission_lock_cache  # noqa: E402
from memory_server.utils import memory_worker  # noqa: E402
from src import config as runtime_config  # noqa: E402
from src.core.logging_config import close_sandbox_log_handlers  # noqa: E402
from src.main import app, protocol_manager  # noqa: E402
from src.models.identity import AgentIdentity  # noqa: E402
from src.services import destructive_ops  # noqa: E402
from src.services.audit_service import PolicyAuditService  # noqa: E402
from src.services.autonomy import autonomy_service  # noqa: E402
from src.services.identity_router import get_verified_identity  # noqa: E402
from src.services.identity_service import identity_service  # noqa: E402
from src.services.models import model_manager  # noqa: E402
from src.services.permission_service import permission_service  # noqa: E402
from src.services.resource_manager import resource_manager  # noqa: E402
from src.services.rhythm_service import rhythm_service  # noqa: E402
from src.services.skill_service import skill_service  # noqa: E402
from src.utils.async_db import session_service  # noqa: E402

# 隔離層替身（iso-c2）：模組會載入產品程式，必須等上面的沙盒環境就緒後才載入。
isolation_support = importlib.import_module("tests.support.isolation")
# 權限自癒的全域掛鉤（iso-c2 U2n）：autouse fixture 每個測試前後重設。
permission_repair = importlib.import_module("src.utils.permission_repair")

logger = logging.getLogger(__name__)

# --- 2. 全域沙盒化保護已提前移至頂部實施 ---


def _enforce_pytest_runtime_baseline() -> None:
    """Reset process, settings, and module-level provider credential state."""
    _enforce_pytest_metered_features_off()
    runtime_config.apply_runtime_env(dict.fromkeys(PROVIDER_CREDENTIAL_ENV_VARS, ""))


AUDIT_DRAIN_TIMEOUT_SECONDS = 30


def _drain_executor(executor: Executor) -> None:
    """等 executor 佇列裡的工作做完。

    直譯器結束時 concurrent.futures 已先收掉 worker（佇列已清空），submit 會丟
    RuntimeError；這時當成已清空，不算釋放失敗。
    """
    try:
        executor.submit(int).result(timeout=AUDIT_DRAIN_TIMEOUT_SECONDS)
    except RuntimeError as exc:
        if "interpreter shutdown" not in str(exc):
            raise


def _drain_policy_audit_writes() -> None:
    """等稽核背景寫入做完，免得它在刪除後把 stats/ 建回來。"""
    _drain_executor(PolicyAuditService._executor)


def _drain_memory_worker() -> None:
    """等記憶工作者做完手上的工作：等待被取消或逾時時，已開始的工作還在跑（ADR-0288）。"""
    _drain_executor(memory_worker._executor)


def _close_qdrant_clients() -> None:
    """Qdrant 本機模式握著 vault/_qdrant_db/.lock；只有載入過向量模組才可能有 client。

    不在頂部匯入向量模組：它會連帶載入 torch，每次 pytest 多約 10 秒。
    """
    vector_module = sys.modules.get("memory_server.vector")
    if vector_module is not None:
        vector_module.VectorStore.close_all_clients()


# 刪沙盒前依序釋放握住它的東西；新的 handle 來源加在這裡（單元測試釘住這五步）。
SANDBOX_RELEASE_STEPS.extend(
    [
        ("drain_policy_audit_writes", _drain_policy_audit_writes),
        ("drain_memory_worker", _drain_memory_worker),
        ("close_qdrant_clients", _close_qdrant_clients),
        ("close_sqlite_connections", FactualStore.close_all_connections),
        ("close_log_handlers", partial(close_sandbox_log_handlers, SESSION_TMP_DATA_ROOT)),
    ]
)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish() -> None:
    """Session fixture 沒跑（--collect-only、沒選到測試）時在這裡刪；結果留給結尾摘要。"""
    _cleanup_session_sandbox()


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    for residue in _sandbox_residue:
        terminalreporter.write_line(f"{SANDBOX_RESIDUE_MESSAGE}{residue}")


@pytest.fixture(scope="session", autouse=True)
def global_test_sandbox():
    """管理會話級別沙盒的生命週期。"""
    # 強制重載以確保載入正確的環境變數 (TOUKA_CONFIG_DIR)
    permission_service.reload_all()

    tmp_data_root = Path(SESSION_TMP_DATA_ROOT)
    (tmp_data_root / "config").mkdir(exist_ok=True)
    (tmp_data_root / "data" / "soul").mkdir(parents=True, exist_ok=True)

    # 正常整合測試必須跑生產決策表，不得把「設定遺失時的安全 fallback」誤當常態。
    # P4 前 sandbox 只播種 auth_policies，整套測試因此長期只覆蓋陳舊 permission fallback；
    # 只由 tracked examples 播種；禁止探查或複製開發者的私有產品設定。
    # 範本缺失由 copyfile 明確失敗；fallback 由 permission matrix 專測。
    _repo_config_dir = Path(__file__).resolve().parents[1] / "ToukaProxy" / "config"
    for _config_name in ("permissions.json", "auth_policies.json"):
        _repo_config = _repo_config_dir / f"{_config_name}.example"
        shutil.copyfile(_repo_config, tmp_data_root / "config" / _config_name)

    # ADR-0147 註記：identity_map.json 刻意**不**在此播種——setup_test_environment
    # 每測試會刪它回 DEFAULT_IDENTITY_MAP（空 tag_rules）。以空 map 為基線時，本批的
    # 接線（verify_token 帶 is_toukasoul 旗標）是**惰性 no-op**（get_role_key 需
    # tag_rules 含 is_toukasoul 才觸發），故既有測試零行為變動。需驗證類型硬綁定的
    # 測試自行以 .example 內容 seed（見 test_soul_role_binding._seed_identity_map）。

    # 沙盒外洩哨兵（AGENTS.md「沙盒隔離」機器執法）：快照 repo 根目錄現有項目，
    # session 結束時比對。任何測試在 repo 內落盤（歷史事故：fixture 被餵字面相對
    # 路徑 → 以 cwd 解析 → 技能檔寫進 repo，弄髒 git status）都會在此被抓出來。
    _repo_root_before = set(PROJECT_ROOT.iterdir())

    yield SESSION_TMP_DATA_ROOT

    # 3. 測試後清理：先跑 SANDBOX_RELEASE_STEPS（log handler、Qdrant、SQLite、稽核寫入）
    #    放掉 Windows 上會 WinError 32 的 handle，再刪；同一個函式也登記在 atexit。
    logger.info(f"🧹 Cleaning up Session Sandbox: {SESSION_TMP_DATA_ROOT}")
    _cleanup_session_sandbox()

    # 3b. 沙盒外洩哨兵：repo 根目錄不得因跑測試而多出項目（fail-loud，勿降級為 warning，
    #     否則正是「悄悄弄髒版控」這個原始問題）。只比對 session 期間的增量，故對已存在
    #     的本地未追蹤檔（.env、node_modules 等）零誤報。
    _leaked = sorted(p.name for p in set(PROJECT_ROOT.iterdir()) - _repo_root_before)
    if _leaked:
        _msg = (
            "測試在 repo 根目錄落盤，違反沙盒隔離（AGENTS.md）："
            f"{_leaked}。請改用 tmp_path 或 global_test_sandbox。"
        )
        raise AssertionError(_msg)

    # 3c. 最後一個測試之後才發生的外網違規（session teardown、背景執行緒）也不得沉默。
    _late_violations = network_guard.drain_violations()
    if _late_violations:
        raise AssertionError(network_guard.format_violations(_late_violations))


@pytest.fixture(autouse=True)
def _external_network_sentinel():
    """外網違規（含其他執行緒、被產品吞掉的例外）讓測試失敗；teardown 回報不覆蓋原失敗。"""
    yield
    violations = network_guard.drain_violations()
    if violations:
        pytest.fail(network_guard.format_violations(violations), pytrace=False)


@pytest.fixture
def mock_identity():
    """提供標準化的 AgentIdentity 工廠函數 (SSOT)。"""

    def _create_identity(
        agent_id: str = "DebugAgent", role: str = "Owner", capabilities: list[str] = None
    ):
        return AgentIdentity.for_role(
            agent_id, role, capabilities=capabilities or ["heartbeat_awakening"]
        )

    return _create_identity


@pytest.fixture
def touka_node_24_gate() -> tuple[str, str | None]:
    """Expose the separately approved exact-host gate through the test env SSOT."""
    variable_name = "TOUKA_NODE_24_EXECUTABLE"
    return variable_name, os.environ.get(variable_name)


@pytest.fixture
def touka_docker_deployment_gate() -> tuple[str, str | None]:
    """Expose explicit live Docker opt-in through the test environment SSOT."""
    variable_name = "TOUKA_RUN_DOCKER_DEPLOYMENT_TESTS"
    return variable_name, os.environ.get(variable_name)


@pytest.fixture(scope="session")
def touka_landlock_requirement() -> bool:
    """TOUKA_REQUIRE_LANDLOCK=1 時，Landlock 不可用要 fail 而不是 skip（測試不得直讀 env）。"""
    return os.environ.get("TOUKA_REQUIRE_LANDLOCK") == "1"


@pytest.fixture(scope="session")
def real_isolation():
    """退出隔離層替身，拿真的 monitor 與 runner broker（只限 Linux 隔離模組）。"""
    return isolation_support.real_isolation_handles()


@pytest.fixture(autouse=True)
def _isolation_test_doubles(request):
    """層級釘在 landlock、runner 直通，不探測也不 spawn；要求 real_isolation 的測試除外。

    用自己的 MonkeyPatch，不借共用的 monkeypatch fixture：借了會把它的建立提早到
    setup_test_environment 之前，測試自己的 patch 就會在那個 teardown 之後才還原。
    """
    isolation_support.reset_consent_ledger()
    try:
        with pytest.MonkeyPatch.context() as isolation_patch:
            if "real_isolation" not in request.fixturenames:
                isolation_support.install_isolation_doubles(isolation_patch)
            yield
    finally:
        isolation_support.reset_consent_ledger()


@pytest.fixture(autouse=True)
def _reset_permission_repair():
    """權限自癒的掛鉤每個測試前後都拿掉（設成 None）；需要的測試自己安裝，不會外洩。"""
    permission_repair.set_permission_repair(None)
    yield
    permission_repair.set_permission_repair(None)


@pytest.fixture(autouse=True)
def setup_test_environment(global_test_sandbox):
    """確保每個測試都在乾淨的環境下執行，且單例狀態已重置 (SSOT & SOLID)。"""
    protocol_manager.reopen_for_test()

    # 1. 注入環境變數 (Sandbox Isolation)
    #    1a-guard：先快照原值，teardown 還原（見步驟 3c）。此 fixture 也被
    #    test_dependency_override_* 以 __wrapped__(<自備沙盒>) 直呼，注入的路徑
    #    未必是 session 沙盒；若不還原，步驟 4 的單例重置會把「別人的沙盒路徑」
    #    快取進 skill_service._skills_dir（該欄位在 reset 時解析一次後常駐），
    #    污染緊接著執行的下一個測試（env 由它的 setup 修好，單例卻仍是舊路徑）
    #    → 技能檔會寫進 repo。還原必須早於步驟 4，否則快取的仍是被覆寫的值。
    _original_env = {
        key: os.environ.get(key)
        for key in ("TOUKA_CONFIG_DIR", "MEMORY_APP_DATA_DIR", "TOUKA_BOOTSTRAP_ENABLED")
    }
    os.environ["TOUKA_CONFIG_DIR"] = str(Path(global_test_sandbox) / "config")
    os.environ["MEMORY_APP_DATA_DIR"] = str(global_test_sandbox)
    os.environ["TOUKA_BOOTSTRAP_ENABLED"] = "false"
    _enforce_pytest_runtime_baseline()

    # 1b. 清除上一個測試在共享 session 沙盒留下的「每代理人」可變狀態（檔案系統隔離）。
    #     soul/<agent>/config.json 會讓 MonitorLayer 把後續請求誤判為 is_toukasoul=True，
    #     vault/brain_settings.json 的 active_brain 又會讓 SoulLayer 改寫 request.model；
    #     兩者疊加會把原本送往 Claude（測試已 mock）的請求改路由到通用 dispatcher，造成
    #     跨測試 race 失敗（如 test-model MODEL_NOT_FOUND）。每測試清空 → 與單獨執行一致。
    #     僅清 ephemeral 測試沙盒，絕不觸碰 repo 的 data/soul、data/legacy。
    _sandbox_root = Path(global_test_sandbox)
    shutil.rmtree(_sandbox_root / "soul", ignore_errors=True)
    with suppress(OSError):
        (_sandbox_root / "vault" / "brain_settings.json").unlink(missing_ok=True)
    # identity_map.json 同理：test_mcp_dispatch_tools 的 autouse fixture 透過
    # permission_service.update_identity() 將映射「持久化到檔案」，其中含白名單外的
    # 小寫 "base" role；permission_service 的 2 秒熱重載（_auto_reload_check）會讓
    # 後續測試的 GET /v1/identity 間歇性讀到它，POST round-trip 經 ASSIGNABLE_AGENT_ROLES 驗證
    # 即 422。刪檔 + 重載單例 → 記憶體與磁碟同步回到 DEFAULT_IDENTITY_MAP。
    with suppress(OSError):
        (Path(os.environ["TOUKA_CONFIG_DIR"]) / "identity_map.json").unlink(missing_ok=True)
    permission_service.reload_all()

    # 2. 備份 app.state.context 的存在狀態與完整內容（Context Isolation）：
    #    lifespan 可能在 fixture setup 後才建立 context；teardown 必須連同「原本不存在」
    #    一起還原，否則測試注入的 protocol_manager 會洩漏到不啟動 lifespan 的下一個測試。
    #    若原本存在，則淺快照所有 key→ref，
    #    可變註冊表額外 deepcopy（避免測試 in-place 改動洩漏到後續測試）。
    #    隨機次序測試揭露：原本只還原 3 個註冊表，tests 改的 anthropic_client /
    #    is_toukasoul / system_degraded 等 key 會洩漏污染後續測試（ADR-0078 後修）。
    context_was_present = hasattr(app.state, "context")
    original_context = None
    if context_was_present:
        ctx = app.state.context
        original_context = dict(ctx)
        for key in ["tool_registry", "plugin_registry", "memory_tool_registry"]:
            if key in ctx:
                original_context[key] = copy.deepcopy(ctx[key])

    # 2b. 快照 session_service 單例的實例 __dict__（無狀態單例，正常為空）。
    #     隨機次序測試揭露：有測試以 monkeypatch.setattr(session_service, "get_session_by_id", ...)
    #     在「實例」上設方法 → monkeypatch 還原時在實例留下 shadow 屬性，蓋過類別
    #     staticmethod，使後續測試在「類別」層的 patch 失效（pulse 測試「Session not found」）。
    original_session_service_dict = dict(session_service.__dict__)

    # 3. 備份依賴覆寫狀態，防堵 Mock 洩漏及避免全域過度清除 (Isolation SSOT)
    original_overrides = app.dependency_overrides.copy()

    # 4a. 確保每個測試開始前 FileLock 快取清空（ADR-0049）
    _reset_mission_lock_cache()

    yield

    # 3. 恢復 context 的存在狀態與完整內容。原本存在時以 clear+update 移除新增 key、
    #    還原被替換的 ref，並以 deepcopy 還原被 in-place 改動的註冊表；原本不存在時
    #    刪除測試/lifespan 後建出的屬性 → 杜絕 context-leak 次序污染。
    if context_was_present and original_context is not None:
        if not hasattr(app.state, "context"):
            app.state.context = {}
        ctx = app.state.context
        ctx.clear()
        ctx.update(original_context)
    elif not context_was_present and hasattr(app.state, "context"):
        del app.state.context

    # 3b. 還原 session_service 實例 __dict__ → 清除測試留下的方法 shadow，使類別
    #     staticmethod 重新生效（杜絕 pulse/heartbeat 一類的次序污染）。
    session_service.__dict__.clear()
    session_service.__dict__.update(original_session_service_dict)

    # 3c. 還原沙盒環境變數（必須先於步驟 4）：單例重置會就地重解析 config 路徑，
    #     若此時 env 仍是本測試自備的沙盒，路徑會被快取並外洩到下一個測試。
    for _key, _value in _original_env.items():
        if _value is None:
            os.environ.pop(_key, None)
        else:
            os.environ[_key] = _value
    _enforce_pytest_runtime_baseline()

    # 4. 重置單例服務 (Singleton Resilience)
    _reset_mission_lock_cache()  # release FileLock handles between tests (ADR-0049)
    permission_service.reset_for_test()
    resource_manager.reset_for_test()
    skill_service.reset_for_test()
    model_manager.reset_for_test()
    rhythm_service.reload_all()
    autonomy_service.reload_all()
    destructive_ops.reload_all()

    # 5. 還原依賴覆寫狀態，防止 Mock 洩漏且不影響其他隔離 Mock (SSOT)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)


@pytest.fixture
def auth_client(mock_identity):
    """提供已認證的 FastAPI TestClient (SSOT)。"""
    permission_service.reload_all()

    debug_identity = mock_identity()
    test_token = identity_service.issue_token(debug_identity)

    app.dependency_overrides[get_verified_identity] = lambda: debug_identity

    with TestClient(app) as test_client:
        test_client.headers.update(
            {"Authorization": f"Bearer {test_token}", "X-Agent-ID": debug_identity.agent_id}
        )
        yield test_client

    app.dependency_overrides.clear()
