# ruff: noqa: T201
"""Touka Architectural SSOT Guard.

Ensures code adheres to layer sovereignty, identity mandates, and resource prison rules.
Supports pluggable rules and optimized path filtering.
"""
import ast
import re
import stat
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterator  # noqa: TC003
from pathlib import Path

REPO_TESTS_DIR = Path(__file__).absolute().parent.parent / "tests"


def _is_top_level_tests_path(filename: str) -> bool:
    """只認 repo 頂層的 tests/；判斷不跟隨連結，比對不上就照舊當成可能是業務層。"""
    path = Path(filename)
    if path.is_absolute():
        return path.absolute().is_relative_to(REPO_TESTS_DIR)
    return path.parts[:1] == ("tests",)


# --- 基礎介面定義 ---


class BaseRule(ABC):
    """SSOT 檢查規則基類。"""

    def __init__(
        self,
        filename: str,
        is_whitelisted: bool = False,
        ignored_lines: dict[int, set[str]] = None,
    ) -> None:
        """初始化規則。

        Args:
            filename: 正在檢查的檔案名稱。
            is_whitelisted: 是否在全域白名單中。
            ignored_lines: 行號對應的被忽略規則 ID 集合。
        """
        self.filename = Path(filename).name
        self.full_path = str(filename)
        self.is_whitelisted = is_whitelisted
        self.ignored_lines = ignored_lines or {}
        self.violations = []

        # 判定是否為核心邏輯層；repo 頂層 tests/ 一律不算（ADR-0278）
        in_tests = _is_top_level_tests_path(filename)
        self.is_service = not in_tests and any(
            x in self.full_path
            for x in ["services", "tools", "routers", "memory_server", "awaken.py", "service"]
        )
        self.is_middleware_or_router = not in_tests and any(
            x in self.full_path for x in ["middleware", "routers", "handlers", "awaken.py"]
        )

    def add_violation(self, node: ast.AST, rule_id: str, msg: str) -> None:
        """記錄違規項。"""
        line = getattr(node, "lineno", 0)
        
        # 檢查是否被單行 noqa 註解忽略
        if line in self.ignored_lines and rule_id in self.ignored_lines[line]:
            return

        self.violations.append({
            "line": line,
            "rule": rule_id,
            "msg": f"🚫 [{rule_id}] {msg}"
        })

    # 以下為 AST 勾子，子類別視需要覆寫

    def check_Call(self, node: ast.Call) -> None:  # noqa: B027
        """檢查函式呼叫。"""
        ...

    def check_Attribute(self, node: ast.Attribute) -> None:  # noqa: B027
        """檢查屬性存取。"""
        ...

    def check_Import(self, node: ast.Import) -> None:  # noqa: B027
        """檢查匯入語句。"""
        ...

    def check_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: B027
        """檢查從模組匯入。"""
        ...

    def check_Constant(self, node: ast.Constant) -> None:  # noqa: B027
        """檢查常數值。"""
        ...

    def check_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: B027
        """檢查函式定義。"""
        ...

    def check_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: B027
        """檢查型別標註賦值 (如類別欄位)。"""
        ...

    def check_Raise(self, node: ast.Raise) -> None:  # noqa: B027
        """檢查 raise 語句。"""
        ...

    def check_Compare(self, node: ast.Compare) -> None:  # noqa: B027
        """檢查比較運算。"""
        ...

    @abstractmethod
    def get_rule_id(self) -> str:
        """獲取規則唯一識別碼。"""
        pass


# --- 具體規則實作 ---

class PathLiteralRule(BaseRule):
    """[PATH_LITERAL_SSOT] 檢查物理路徑硬編碼。"""

    PATH_PATTERN = re.compile(r'^[a-zA-Z]:[/\\]|/(app|home|root|tmp)/')
    SAFE_STRINGS = {
        "utf-8", "utf8", "ascii", "latin-1",
        ".py", ".json", ".md", ".db", ".txt",
        "%Y-%m-%d", "%H:%M:%S",
        "white-space: pre-wrap"
    }

    def get_rule_id(self) -> str:
        """獲取規則 ID。"""
        return "PATH_LITERAL_SSOT"

    def check_Constant(self, node: ast.Constant) -> None:
        """檢查字串常數是否包含絕對路徑。"""
        if self.is_whitelisted or not isinstance(node.value, str):
            return

        val = node.value
        if val in self.SAFE_STRINGS or val.startswith(("/v1", "http")):
            return

        if self.PATH_PATTERN.search(val):
            self.add_violation(
                node, self.get_rule_id(),
                f"偵測到硬編碼物理路徑 '{val}'。請使用 ResourceManager 動態解析。"
            )


class IOPrisonRule(BaseRule):
    """[IO_PRISON_VIOLATION] 檢查業務層是否越權執行 IO。"""

    BANNED_IO_CALLS = {
        "open", "read_text", "write_text", "remove", "mkdir", "rmdir",
        "rename", "unlink", "symlink", "link", "chmod"
    }

    def get_rule_id(self) -> str:
        """獲取規則 ID。"""
        return "IO_PRISON_VIOLATION"

    def check_Call(self, node: ast.Call) -> None:
        """檢查是否調用被禁止的 IO 函式。"""
        if self.is_whitelisted:
            return

        if not self.is_service and not self.is_middleware_or_router:
            return

        func_name = None
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        if func_name in self.BANNED_IO_CALLS:
            self.add_violation(
                node, self.get_rule_id(),
                f"業務層嚴禁調用 '{func_name}'。請強制透過 ResourceManager 進行檔案操作。"
            )


class TimeSovereigntyRule(BaseRule):
    """[TIME_SSOT_VIOLATION] 檢查是否違規獲取系統時間。"""

    BANNED_TIME_CALLS = {
        "time.time", "datetime.now", "datetime.today",
        "datetime.utcnow", "date.today", "time.localtime", "time.gmtime"
    }
    BANNED_TIME_NAMES = {"time", "now", "utcnow", "today"}
    TIME_WHITELIST = {
        "time_utils.py", "conftest.py", "config_utils.py",
        "run_all_smoke_tests.py", "manage_backup.py", "awaken.py"
    }

    def get_rule_id(self) -> str:
        """獲取規則 ID。"""
        return "TIME_SSOT_VIOLATION"

    def check_Call(self, node: ast.Call) -> None:
        """檢查是否調用被禁止的時間 API。"""
        if self.filename in self.TIME_WHITELIST or self.filename.startswith("smoke_"):
            return

        call_name = ""
        if isinstance(node.func, ast.Name):
            call_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                call_name = f"{node.func.value.id}.{node.func.attr}"
            else:
                call_name = node.func.attr

        if call_name in self.BANNED_TIME_CALLS or call_name in self.BANNED_TIME_NAMES:
            self.add_violation(
                node, self.get_rule_id(),
                f"嚴禁直接調用 '{call_name}'，全系統時間獲取必須透過 src.utils.time_utils。"
            )


class EnvSovereigntyRule(BaseRule):
    """[ENV_SSOT_VIOLATION] 禁止直接存取環境變數或調用系統操作 API。

    涵蓋兩類禁令（由 AGENTS.md 第 18 節規定）：
    1. 環境變數存取（全域）：os.environ, os.getenv 等
    2. 系統操作 API（限 /src/ 業務層）：os.system, os.chdir, sys.path.append
    """

    ENV_WHITELIST = {
        "config.py", "main.py", "bootstrap.py", "terminal.py", "conftest.py",
        "config_utils.py", "run_all_smoke_tests.py", "test_static_analysis.py",
        "manage_backup.py", "awaken.py", "guard_dependencies.py",
        "guard_module_boundaries.py",
        "test_module_isolation.py", "test_server_lifecycle.py",
        "test_h3_h4_security.py",  # 需要直接操作 os.environ 驗證環境變數過濾邏輯
        "sandbox_env.py",  # live smoke 基礎：import src 前注入 sandbox env（ADR-0103）
    }
    BANNED_ENV_NAMES = {"getenv", "environ"}

    def get_rule_id(self) -> str:
        """獲取規則 ID。"""
        return "ENV_SSOT_VIOLATION"

    def _is_env_violation(self, name: str) -> bool:
        return (
            name in {"os.getenv", "os.environ.get", "os.environ"}
            or name in self.BANNED_ENV_NAMES
        )

    def check_Call(self, node: ast.Call) -> None:
        """檢查是否調用環境變數獲取函式。"""
        if self.filename in self.ENV_WHITELIST:
            return

        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            func_name = f"{node.func.value.id}.{node.func.attr}"

        if self._is_env_violation(func_name):
            self.add_violation(
                node, self.get_rule_id(),
                f"嚴禁直接調用 '{func_name}'，請將配置遷移至 src.config。"
            )

        # ── 系統操作 API 禁令（限 /src/ 業務層，與 PRINT_BAN 相同路徑過濾）──
        if self.is_whitelisted:
            return
        if "/src/" not in self.full_path and "\\src\\" not in self.full_path:
            return

        _BANNED_SYS_APIS = frozenset({"os.system", "os.chdir"})
        if func_name in _BANNED_SYS_APIS:
            self.add_violation(
                node, self.get_rule_id(),
                f"嚴禁在業務邏輯中調用 '{func_name}'。"
                "進程與系統操作僅限引導者白名單（bootstrap.py 等）。",
            )
            return

        # sys.path.append：AST = Call(func=Attr(Attr(Name('sys'),'path'),'append'))
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "path"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "sys"
        ):
            self.add_violation(
                node, self.get_rule_id(),
                "嚴禁在業務邏輯中調用 'sys.path.append()'。"
                "路徑操作僅限引導者白名單（ADR-0017）。",
            )

    def check_Attribute(self, node: ast.Attribute) -> None:
        """檢查是否存取 os.environ。"""
        if self.filename in self.ENV_WHITELIST:
            return
        attr_name = ""
        if isinstance(node.value, ast.Name):
            attr_name = f"{node.value.id}.{node.attr}"
        if attr_name == "os.environ":
            self.add_violation(node, self.get_rule_id(), "嚴禁直接存取 'os.environ'。")

    def check_ImportFrom(self, node: ast.ImportFrom) -> None:
        """檢查是否直接導入環境變數相關名稱。"""
        if self.filename in self.ENV_WHITELIST:
            return
        if node.module == "os":
            for alias in node.names:
                if alias.name in self.BANNED_ENV_NAMES:
                    self.add_violation(
                        node, self.get_rule_id(), f"嚴禁導入 'os.{alias.name}'。"
                    )


class ErrorSovereigntyRule(BaseRule):
    """[ERROR_SSOT_VIOLATION] 檢查錯誤碼主權。"""

    def get_rule_id(self) -> str:
        """獲取規則 ID。"""
        return "ERROR_SSOT_VIOLATION"

    def check_Call(self, node: ast.Call) -> None:
        """檢查 HTTPException 是否包含硬編碼字串。"""
        if self.filename == "awaken.py":
            return
            
        call_name = ""
        if isinstance(node.func, ast.Name):
            call_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            call_name = node.func.attr

        if call_name in {"HTTPException", "ToukaException", "raise_error"}:
            for keyword in node.keywords:
                if (
                    keyword.arg == "detail"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    self.add_violation(
                        node, self.get_rule_id(), f"嚴禁在 {call_name} 中使用硬編碼錯誤字串。"
                    )

            if call_name == "HTTPException":
                self.add_violation(
                    node, 
                    self.get_rule_id(), 
                    "偵測到原生 HTTPException。建議遷移至 ToukaException。"
                )


class IdentitySovereigntyRule(BaseRule):
    """[IDENTITY_SSOT / CONTEXT_SSOT / PAC_CHECK] 檢查身分物件化與策略執法。"""

    BANNED_ID_PARAMS = {"agent_id", "soul_id", "model_id", "target_id", "source_id"}

    def get_rule_id(self) -> str:
        """獲取規則 ID。"""
        return "IDENTITY_SSOT"

    def check_FunctionDef(self, node: ast.FunctionDef) -> None:
        """檢查函式簽章是否符合身份與上下文規範。"""
        # Identity Check
        if self.is_service or self.is_middleware_or_router:
            for arg in node.args.args:
                if (
                    arg.arg in self.BANNED_ID_PARAMS
                    and isinstance(arg.annotation, (ast.Name, ast.BinOp, ast.Subscript))
                ):
                    # 檢查是否包含 str 型別
                    annotation_str = ast.unparse(arg.annotation)
                    if "str" in annotation_str and "AgentIdentity" not in annotation_str:
                        msg = (
                            f"函式 '{node.name}' 嚴禁使用"
                            f" '{arg.arg}: {annotation_str}'。請傳遞 AgentIdentity 物件。"
                        )
                        self.add_violation(arg, "IDENTITY_SSOT", msg)

        # Context Check
        if self.is_middleware_or_router:
            for arg in node.args.args:
                if (
                    arg.arg == "ctx"
                    and arg.annotation
                    and isinstance(arg.annotation, ast.Name)
                    and arg.annotation.id == "dict"
                ):
                    self.add_violation(
                        arg, "CONTEXT_SSOT", f"函式 '{node.name}' 嚴禁使用 'ctx: dict'。"
                    )

    def check_AnnAssign(self, node: ast.AnnAssign) -> None:
        """檢查類別欄位是否違反身分物件化 (Pydantic models etc.)。"""
        if not self.is_service and not self.is_middleware_or_router:
            return

        if (
            isinstance(node.target, ast.Name)
            and node.target.id in self.BANNED_ID_PARAMS
            and node.annotation
        ):
            annotation_str = ast.unparse(node.annotation)
            if "str" in annotation_str and "AgentIdentity" not in annotation_str:
                msg = (
                    f"欄位 '{node.target.id}' 嚴禁使用 '{annotation_str}'。"
                    "請使用 AgentIdentity 物件。"
                )
                self.add_violation(node, "IDENTITY_SSOT", msg)

    def check_Compare(self, node: ast.Compare) -> None:
        """檢查是否手動檢查 auth_level。"""
        # PAC Manual Check
        is_auth_level_check = False
        if (
            isinstance(node.left, ast.Call)
            and isinstance(node.left.func, ast.Attribute)
            and node.left.func.attr == "get"
            and len(node.left.args) > 0
            and isinstance(node.left.args[0], ast.Constant)
            and node.left.args[0].value == "auth_level"
        ) or (
            isinstance(node.left, ast.Subscript)
            and isinstance(node.left.slice, ast.Constant)
            and node.left.slice.value == "auth_level"
        ):
            is_auth_level_check = True

        if is_auth_level_check:
            self.add_violation(
                node, 
                "PAC_MANUAL_LEVEL_CHECK", 
                "嚴禁手動檢查 'auth_level'，請封裝至 AuthPolicies 中。"
            )


class ApiPrefixSovereigntyRule(BaseRule):
    """[API_PREFIX_SSOT] 檢查 API 前綴與路徑一致性。"""

    def get_rule_id(self) -> str:
        """獲取規則 ID。"""
        return "API_PREFIX_SSOT"

    def check_Call(self, node: ast.Call) -> None:
        """檢查 APIRouter 定義與端點裝飾器。"""
        # 1. 檢查 APIRouter 實例化
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        if func_name == "APIRouter":
            has_prefix = any(kw.arg == "prefix" for kw in node.keywords)
            has_tags = any(kw.arg == "tags" for kw in node.keywords)

            if not has_prefix:
                self.add_violation(node, self.get_rule_id(), "APIRouter 必須包含 'prefix' 參數。")
            if not has_tags:
                self.add_violation(node, self.get_rule_id(), "APIRouter 必須包含 'tags' 參數。")

            for kw in node.keywords:
                is_prefix = kw.arg == "prefix"
                is_const = isinstance(kw.value, ast.Constant)
                if is_prefix and is_const and isinstance(kw.value.value, str):
                    prefix_val = kw.value.value
                    if prefix_val.startswith("/v1"):
                        self.add_violation(
                            node,
                            self.get_rule_id(),
                            f"APIRouter 嚴禁硬編碼 '/v1' 前綴 ('{prefix_val}')，"
                            "請在 main.py 中統一掛載。"
                        )

        # 2. 檢查端點裝飾器 (Trailing Slash Check)
        is_route = isinstance(node.func, ast.Attribute) and node.func.attr in {
            "get", "post", "put", "patch", "delete"
        }
        if is_route and len(node.args) > 0 and isinstance(node.args[0], ast.Constant):
            path_val = node.args[0].value
            if isinstance(path_val, str) and path_val != "/" and path_val.endswith("/"):
                self.add_violation(
                    node,
                    self.get_rule_id(),
                    f"端點 '{path_val}' 嚴禁以斜線結尾。請落實無斜線韌性。"
                )


class LocalImportRule(BaseRule):
    """[LOCAL_IMPORT_BAN] 禁止在函式體內使用區域匯入。

    AGENTS.md 規定：所有依賴必須在模組頂部定義，以確保測試時可精確進行 Patch 與 Mock。
    """

    RULE_ID = "LOCAL_IMPORT_BAN"

    def get_rule_id(self) -> str:
        """獲取規則 ID。"""
        return self.RULE_ID

    def check_FunctionDef(self, node: ast.FunctionDef) -> None:
        """檢查函式直接子語句中的 import。不遞迴進子函式（由 visitor 單獨訪問）。"""
        if self.is_whitelisted:
            return
        for stmt in node.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                import_repr = self._fmt_module(stmt)
                self.add_violation(
                    stmt,
                    self.RULE_ID,
                    f"Local import `{import_repr}` inside `{node.name}()`. "
                    "Move to module top-level for precise Patch/Mock.",
                )

    @staticmethod
    def _fmt_module(node: ast.Import | ast.ImportFrom) -> str:
        """格式化 import 語句為可讀字串。"""
        if isinstance(node, ast.Import):
            return ", ".join(a.name for a in node.names)
        module = node.module or "."
        names = ", ".join(a.name for a in node.names)
        return f"from {module} import {names}"


# --- 日誌主權規則 ---


class PrintBanRule(BaseRule):
    """[PRINT_BAN] 業務層嚴禁直接調用 print()。

    正確做法：在模組頂部宣告 `logger = logging.getLogger(__name__)`，
    透過結構化日誌輸出，確保日誌可被過濾、路由至 log file 及監控系統。
    """

    def get_rule_id(self) -> str:
        """回傳規則識別碼。"""
        return "PRINT_BAN"

    def check_Call(self, node: ast.Call) -> None:
        """檢查 print() 函式呼叫。"""
        if self.is_whitelisted:
            return
        # 僅對 src/ 目錄下的業務核心代碼執行（排除測試、腳本、診斷工具等）
        if "/src/" not in self.full_path and "\\src\\" not in self.full_path:
            return
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            self.add_violation(
                node,
                self.get_rule_id(),
                "業務層嚴禁直接調用 print()。"
                "請改用 logger = logging.getLogger(__name__)。",
            )


class BasicConfigBanRule(BaseRule):
    """[BASICCONFIG_BAN] 嚴禁在非初始化檔案中調用 logging.basicConfig()。

    日誌初始化的唯一授權入口為 src/core/logging_config.py（ToukaProxy 的 setup_logging()）。
    其他容器（ToukaHeartbeat/awaken.py）已列入 WHITELIST_FILES 豁免。
    """

    def get_rule_id(self) -> str:
        """回傳規則識別碼。"""
        return "BASICCONFIG_BAN"

    def check_Call(self, node: ast.Call) -> None:
        """檢查 logging.basicConfig() 呼叫。"""
        if self.is_whitelisted:
            return
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logging"
            and node.func.attr == "basicConfig"
        ):
            self.add_violation(
                node,
                self.get_rule_id(),
                "嚴禁在業務檔案中調用 logging.basicConfig()。"
                "請透過 src/core/logging_config.py 的 setup_logging() 統一管理日誌初始化。",
            )


class RaiseHardcodedStringRule(BaseRule):
    """[RAISE_STRING_BAN] 業務層嚴禁對 ValueError/RuntimeError/TypeError 使用硬編碼字串。

    請改用 ToukaException(ErrorCode.XXX)。

    合規寫法：
      raise ToukaException(ErrorCode.VALIDATION_ERROR)
      raise ToukaException(ErrorCode.MODEL_NOT_FOUND, detail_override=f"Model {m} not found")
    """

    # 禁止對這些例外使用硬編碼字串訊息
    BANNED_EXCEPTIONS: frozenset[str] = frozenset({"ValueError", "RuntimeError", "TypeError"})

    def get_rule_id(self) -> str:
        """回傳規則識別碼。"""
        return "RAISE_STRING_BAN"

    def check_Raise(self, node: ast.Raise) -> None:
        """檢查 raise 語句中的硬編碼字串。"""
        # 1. 僅對 src/ 目錄下的業務核心代碼執行（排除 tests/、scripts/ 等）
        if "/src/" not in self.full_path and "\\src\\" not in self.full_path:
            return
        if self.is_whitelisted:
            return

        exc = node.exc
        if not isinstance(exc, ast.Call):
            return

        # 2. 識別例外類別名稱
        func = exc.func
        if isinstance(func, ast.Name):
            exc_name = func.id
        elif isinstance(func, ast.Attribute):
            exc_name = func.attr
        else:
            return

        if exc_name not in self.BANNED_EXCEPTIONS:
            return

        # 3. 必須有位置參數
        if not exc.args:
            return

        first_arg = exc.args[0]
        is_hardcoded_string = isinstance(first_arg, ast.Constant) and isinstance(
            first_arg.value, str
        )
        is_fstring = isinstance(first_arg, ast.JoinedStr)

        if is_hardcoded_string or is_fstring:
            self.add_violation(
                node,
                self.get_rule_id(),
                f"業務層嚴禁對 `{exc_name}` 使用硬編碼字串或 f-string 訊息。"
                "請改用 `raise ToukaException(ErrorCode.XXX)` "
                "或 `raise ToukaException(ErrorCode.XXX, detail_override='...')`。",
            )


class BcryptImportRule(BaseRule):
    """[BCRYPT_IMPORT_SSOT] 禁止繞過 AuthService 直接使用 bcrypt 或 passlib。

    ADR-0028 / 禁令 4：密碼雜湊必須維持 SHA256 → Bcrypt 預雜湊鏈以支援長密碼。
    bcrypt 原生 72-byte 截斷限制若被繞過，長密碼將錯誤通過驗證。

    唯一授權匯入點：ToukaProxy/src/services/auth_service.py
    """

    BANNED_TOP_LEVEL: frozenset[str] = frozenset({"bcrypt", "passlib"})
    AUTHORIZED_FILES: frozenset[str] = frozenset({"auth_service.py"})

    def get_rule_id(self) -> str:
        """回傳規則識別碼。"""
        return "BCRYPT_IMPORT_SSOT"

    def check_Import(self, node: ast.Import) -> None:
        """檢查 import bcrypt / import passlib 的非授權使用。"""
        if self.filename in self.AUTHORIZED_FILES:
            return
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in self.BANNED_TOP_LEVEL:
                self.add_violation(
                    node,
                    self.get_rule_id(),
                    f"禁止直接匯入 '{alias.name}'。"
                    "密碼雜湊必須透過 AuthService.hash_password() 執行，"
                    "以維持 SHA256 → Bcrypt 預雜湊鏈（ADR-0028）。",
                )

    def check_ImportFrom(self, node: ast.ImportFrom) -> None:
        """檢查 from bcrypt import ... / from passlib.* import ... 的非授權使用。"""
        if self.filename in self.AUTHORIZED_FILES:
            return
        if node.module and node.module.split(".")[0] in self.BANNED_TOP_LEVEL:
            self.add_violation(
                node,
                self.get_rule_id(),
                f"禁止從 '{node.module}' 直接匯入。"
                "密碼雜湊必須透過 AuthService.hash_password() 執行，"
                "以維持 SHA256 → Bcrypt 預雜湊鏈（ADR-0028）。",
            )


class Sqlite3ImportRule(BaseRule):
    """[SQLITE3_IMPORT_BAN] 禁止直接匯入 sqlite3，必須透過授權的非同步資料庫代理層。

    ADR-0030：直接使用 sqlite3 繞過非同步佇列與物理鎖定，
    可能導致並發寫入競態與身分追蹤失效。

    授權匯入點：
      - ToukaProxy → async_db.py（非同步 SSOT，含佇列鎖定）
      - ToukaMemory → factual.py, legacy_store.py（模組自有 SSOT）
    """

    BANNED_TOP_LEVEL: frozenset[str] = frozenset({"sqlite3"})
    AUTHORIZED_FILES: frozenset[str] = frozenset({
        "async_db.py",
        "factual.py",
        "legacy_store.py",
    })

    def get_rule_id(self) -> str:
        """回傳規則識別碼。"""
        return "SQLITE3_IMPORT_BAN"

    def check_Import(self, node: ast.Import) -> None:
        """檢查 import sqlite3 的非授權使用。"""
        if self.filename in self.AUTHORIZED_FILES:
            return
        for alias in node.names:
            if alias.name.split(".")[0] in self.BANNED_TOP_LEVEL:
                self.add_violation(
                    node,
                    self.get_rule_id(),
                    f"禁止直接匯入 '{alias.name}'。"
                    "ToukaProxy 業務層請使用 async_db.SessionService；"
                    "ToukaMemory 請使用模組自有 SSOT（ADR-0030）。",
                )

    def check_ImportFrom(self, node: ast.ImportFrom) -> None:
        """檢查 from sqlite3 import ... 的非授權使用。"""
        if self.filename in self.AUTHORIZED_FILES:
            return
        if node.module and node.module.split(".")[0] in self.BANNED_TOP_LEVEL:
            self.add_violation(
                node,
                self.get_rule_id(),
                f"禁止從 '{node.module}' 直接匯入（ADR-0030）。"
                "請透過授權的非同步資料庫代理層存取。",
            )


class FileLockAsyncRule(BaseRule):
    """[FILELOCK_ASYNC_SSOT] 禁止在服務層直接 import 同步 FileLock。

    原因：FileLock 預設 thread_local=True，在 run_in_executor 中 acquire()
    後從主執行緒 release() 會因 thread-local context 不同而靜默失敗，
    導致 OS 層 byte-range lock 永遠未釋放（ADR-0049）。
    服務層必須改用 AsyncFileLock（thread_local=False 為預設，acquire/release 均為 async）。
    例外：直接在本規則檔案中的 noqa 豁免標記。
    """

    def get_rule_id(self) -> str:
        """回傳規則識別碼。"""
        return "FILELOCK_ASYNC_SSOT"

    def check_ImportFrom(self, node: ast.ImportFrom) -> None:
        """偵測 from filelock import FileLock 的非授權使用。"""
        if not self.is_service:
            return
        if node.module != "filelock":
            return
        for alias in node.names:
            if alias.name == "FileLock":
                self.add_violation(
                    node,
                    self.get_rule_id(),
                    "禁止在服務層 import 同步 FileLock；"
                    "請改用 AsyncFileLock（thread_local=False 為預設，ADR-0049）。",
                )

    def check_Call(self, node: ast.Call) -> None:
        """偵測 `import filelock; filelock.FileLock(...)` 的屬性呼叫繞道（0049⑤ 盲點）。"""
        if not self.is_service:
            return
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "FileLock"
            and isinstance(func.value, ast.Name)
            and func.value.id == "filelock"
        ):
            self.add_violation(
                node,
                self.get_rule_id(),
                "禁止在服務層以 filelock.FileLock(...) 建同步鎖（Import+Call 繞道）；"
                "請改用 AsyncFileLock（ADR-0049）。",
            )


# --- 主引擎 (Dispatcher) ---

class SSOTGuard(ast.NodeVisitor):
    """Touka SSOT 法律執行官。負責調度各類 Rule 進行檢查。"""

    WHITELIST_FILES = {
        "config.py", "bootstrap.py", "time_utils.py",
        "conftest.py", "update_docs.py", "export_schema.py",
        "resource_manager.py", "config_utils.py", "terminal.py",
        "soul_io.py", "evolution_security.py", "custom_io.py",
        "core_file_ops.py", "test_ssot_guard_path.py",
        "test_ssot_guard_io.py", "test_ssot_guard_local_import.py", "smoke_mcp_lifecycle.py",
        "smoke_io_prison.py", "smoke_ldh_handshake.py",
        "smoke_mission_awareness.py", "smoke_group_dispatcher.py",
        "smoke_soul_recovery.py", "smoke_agent_interaction.py",
        "test_api_schema_sync.py", "update_openapi.py",
        "smoke_autonomy_enforcement.py", "run_all_smoke_tests.py",
        "smoke_chat.py", "smoke_frontend_persistence.py",
        "smoke_memory_tools.py", "smoke_model_governance.py",
        "smoke_pulse_rhythm.py", "manage_backup.py", "awaken.py",
        "test_lint_ssot.py",
        "logging_config.py",   # 日誌配置 SSOT 入口，合法使用 logging.basicConfig()
        "test_h3_h4_security.py",  # 需要直接操作 os.environ 以驗證環境變數過濾邏輯
        "test_dispatch_server_tools.py",  # ToukaDispatch 測試：tmp_path IO + 延遲匯入
        "test_mcp_dispatch_tools.py",  # Dispatch 整合測試：延遲匯入 AgentIdentity
        "test_mission_manager_bugfixes.py",  # MissionManager 修復驗證：延遲匯入 server 模組
        # SessionManager + Handshake TDD：DISPATCH_TOKEN 設定後延遲匯入 server
        "test_dispatch_session.py",
        # ADR-0064 mission_token TDD：DISPATCH_TOKEN 設定後延遲匯入 server
        "test_dispatch_mission_token.py",
        "test_dispatch_hooks.py",  # ADR-0067 auto-invoke TDD：DISPATCH_TOKEN 設定後延遲匯入 server
        # DA #13/#14 看板完整性 TDD：DISPATCH_TOKEN 設定後延遲匯入 server
        "test_dispatch_board_integrity.py",
        # DA #11 內容消毒 TDD：DISPATCH_TOKEN 設定後延遲匯入 server
        "test_dispatch_content_sanitization.py",
        # ADR-0150：compose 容器路徑 /app/config（PATH literal 即測試意圖）
        "test_heartbeat_config_dir_mount.py",
        # ADR-0150：touka-init 備妥 /app/heartbeat/config（PATH literal 即測試意圖）
        "test_docker_compose_config.py",
        # ADR-0151：.env 錨定 /app/host_root＋幽靈檔守衛（PATH literal 即測試意圖）
        "test_env_persistence_anchor.py",
    }

    def __init__(self, filename: str, ignored_lines: dict[int, set[str]] = None) -> None:
        """初始化守衛引擎。"""
        self.filename = Path(filename).name
        is_whitelisted = self.filename in self.WHITELIST_FILES
        self.ignored_lines = ignored_lines or {}

        # 註冊所有檢查規則
        self.rules: list[BaseRule] = [
            PathLiteralRule(filename, is_whitelisted, self.ignored_lines),
            IOPrisonRule(filename, is_whitelisted, self.ignored_lines),
            TimeSovereigntyRule(filename, is_whitelisted, self.ignored_lines),
            EnvSovereigntyRule(filename, is_whitelisted, self.ignored_lines),
            ErrorSovereigntyRule(filename, is_whitelisted, self.ignored_lines),
            IdentitySovereigntyRule(filename, is_whitelisted, self.ignored_lines),
            ApiPrefixSovereigntyRule(filename, is_whitelisted, self.ignored_lines),
            LocalImportRule(filename, is_whitelisted, self.ignored_lines),
            PrintBanRule(filename, is_whitelisted, self.ignored_lines),
            BasicConfigBanRule(filename, is_whitelisted, self.ignored_lines),
            RaiseHardcodedStringRule(filename, is_whitelisted, self.ignored_lines),
            BcryptImportRule(filename, is_whitelisted, self.ignored_lines),
            Sqlite3ImportRule(filename, is_whitelisted, self.ignored_lines),
            FileLockAsyncRule(filename, is_whitelisted, self.ignored_lines),
        ]

    @property
    def violations(self) -> list[dict]:
        """彙整所有規則的違規項。"""
        all_v = []
        for rule in self.rules:
            all_v.extend(rule.violations)
        # 依行號排序
        return sorted(all_v, key=lambda x: x["line"])

    # --- AST 遍歷與分發 ---

    def visit_Call(self, node: ast.Call) -> None:
        """分發 Call 節點。"""
        for rule in self.rules:
            rule.check_Call(node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """分發 Attribute 節點。"""
        for rule in self.rules:
            rule.check_Attribute(node)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        """分發 Import 節點。"""
        for rule in self.rules:
            rule.check_Import(node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """分發 ImportFrom 節點。"""
        for rule in self.rules:
            rule.check_ImportFrom(node)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        """分發 Constant 節點。"""
        for rule in self.rules:
            rule.check_Constant(node)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        """分發 Raise 節點。"""
        for rule in self.rules:
            rule.check_Raise(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """分發 FunctionDef 節點。"""
        for rule in self.rules:
            rule.check_FunctionDef(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """分發 AsyncFunctionDef 節點。"""
        # 異步函式共用 FunctionDef 的檢查邏輯
        for rule in self.rules:
            rule.check_FunctionDef(node)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        """分發 Compare 節點。"""
        for rule in self.rules:
            rule.check_Compare(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """分發 AnnAssign 節點。"""
        for rule in self.rules:
            rule.check_AnnAssign(node)
        self.generic_visit(node)


def _is_source_path(path: Path) -> bool:
    """Exclude data lexically and reject every link ancestor without following it."""
    absolute = path.absolute()
    if any(part.casefold() == "data" for part in absolute.parts):
        return False
    for component in (*reversed(absolute.parents), absolute):
        metadata = component.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            return False
    return True


def run_guard(target_path_str: str) -> list[tuple[Path, list[dict]]]:
    """執行全域 SSOT 守衛檢查。"""
    all_violations = []
    target_path = Path(target_path_str)

    # 優化的過濾清單
    IGNORED_DIRS = {
        ".venv",
        "venv",
        "env",
        ".cache",
        "node_modules",
        "site-packages",
        "__pycache__",
        ".pytest_cache",
        ".git",
        "violations",
        ".claude",
        "data",  # 資料保護與 worktree 副本排除
        ".toukadev",  # ToukaDev 本機工作區（lessons／worktrees）；只比對名稱，勿改成路徑逐段比對
    }

    def get_files(path: Path) -> Iterator[Path]:
        """遞迴獲取 Python 檔案，避開被忽略的目錄。"""
        if path.name.casefold() in IGNORED_DIRS or not _is_source_path(path):
            return
        if path.is_file():
            yield path
            return

        for p in path.iterdir():
            if p.name.casefold() in IGNORED_DIRS or not _is_source_path(p):
                continue
            if p.is_dir():
                yield from get_files(p)
            elif p.suffix == ".py":
                yield p

    def parse_noqa_comments(content: str) -> dict[int, set[str]]:
        """解析內容中的 # noqa: RULE_ID 註解。"""
        ignored = {}
        for i, line in enumerate(content.splitlines(), 1):
            match = re.search(r'#\s*noqa:\s*([A-Za-z0-9_,\s]+)', line)
            if match:
                rules = {r.strip() for r in match.group(1).split(',')}
                ignored[i] = rules
        return ignored

    for py_file in get_files(target_path):
        with py_file.open(encoding="utf-8") as f:
            try:
                content = f.read()
                ignored_lines = parse_noqa_comments(content)
                tree = ast.parse(content)
                visitor = SSOTGuard(str(py_file), ignored_lines)
                visitor.visit(tree)
                if visitor.violations:
                    all_violations.append((py_file, visitor.violations))
            except Exception as e:
                print(f"Error parsing {py_file}: {e}")

    return all_violations


if __name__ == "__main__":
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    target = sys.argv[1] if len(sys.argv) > 1 else "."
    print(f"🔍 Touka Architectural SSOT Guard 正在啟動... (目標: {target})")
    results = run_guard(target)

    if not results:
        print("✅ 恭喜！未發現違反架構 SSOT 的代碼。")
        sys.exit(0)
    else:
        print(f"🚨 發現 {len(results)} 個檔案違反架構主權規範：\n")
        total = 0
        for file, violations in results:
            print(f"📄 {file}")
            for v in violations:
                print(f"  [L{v['line']}] {v['msg']}")
                total += 1
        print(f"\n❌ 總計發現 {total} 處法規違規。")
        sys.exit(1)
