"""ADR-0293 Z8：區內路徑的靜態檢查（輔助絆線；主防線是 ``ZonePath`` 型別與 R13、R14）.

只解析原始碼（錨點的資料夾名稱從 ``zone_paths`` 讀）。範圍是產品原始碼根
（``test_ci_pr_checks.PRODUCT_SOURCE_ROOTS``）加 ``ToukaProxy/scripts``；T3 與絆線的呼叫者限制掃
``tests/``。解析失敗就紅；只看語法樹，所以 ``# noqa`` 一類註解不影響結果。

- T1：舊產生函式不能再拿個人空間；``get_outputs_path`` 一類名稱不能再出現。
- T2：回傳值可能是區內路徑的函式，回傳標註要寫出 ``ZonePath``。同一個函式內追資料流（指定、
  walrus、拆解、``or``／``and``、條件運算式、``child``／``join_rel``／``parent`` 的鏈、``self.``
  屬性、容器與 dataclass 建構參數），跨函式以名稱遞移（``from x import f as g`` 照別名；同名的
  保守地當成同一個）。
- T3：測試不能替換區內產生函式、data 根與 ``zone_paths`` 的任何模組層名稱（直接呼叫
  改根、交接登記與觀察者的函式也算）；只有明列的 fixture 與明列的例外。
- G1：轉出、整個錨點、錨點名稱、交接登記與 ``zone_abs`` 的呼叫點只在明列的「模組:函式」，雙向相等。
- G2：錨點的資料夾名稱只在 ``zone_paths`` 與明列的「模組:函式:名稱」，次數也釘住。
- 其他：產品不用 ``os.PathLike`` 判斷；測試的區例外紀錄與絆線的內部只有明列的測試檔能碰；
  ``lint_ssot.py`` 的 I/O 白名單釘住。
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from memory_server.utils import zone_paths
from memory_server.utils.zone_paths import Anchor, anchor_dirname
from src.services.resource_manager import resource_manager as rm

from tests.lint.test_ci_pr_checks import PRODUCT_SOURCE_ROOTS

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (*PRODUCT_SOURCE_ROOTS, "ToukaProxy/scripts")
_SKIP_PARTS = frozenset({"tests", "__pycache__", ".venv", "node_modules"})
ZONE_PATHS = "ToukaMemory/src/memory_server/utils/zone_paths.py"
THIS_FILE = "tests/lint/test_zone_path_guard.py"
# 掃描清單一定要有的檔案（範圍設錯時先紅，不會變成什麼都沒掃）
CANARIES = frozenset(
    {
        ZONE_PATHS,
        "ToukaMemory/src/memory_server/utils/no_follow.py",
        "ToukaProxy/src/services/resource_manager.py",
        "ToukaProxy/src/tools/terminal.py",
        "ToukaProxy/scripts/manage_backup.py",
        "ToukaHeartbeat/permission_init.py",
        "ToukaDispatch/server.py",
    }
)
# lambda 不另成一個 scope：本體算在外層（寫成 lambda 就看不到的話，任何規則都能這樣繞過）
_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


# --- 讀原始碼 -------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    rel: str
    module: str
    tree: ast.Module


def module_name(rel: str) -> str:
    """repo 相對路徑 → 執行時的模組名稱（``src.…``、``memory_server.…``、``scripts.…``）."""
    parts = rel.removesuffix(".py").split("/")
    if parts[:2] in (["ToukaProxy", "src"], ["ToukaProxy", "scripts"]):
        parts = parts[1:]
    elif parts[:2] == ["ToukaMemory", "src"]:
        parts = parts[2:]
    elif parts[0] in ("ToukaHeartbeat", "ToukaDispatch"):
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _parse(path: Path) -> Source:
    rel = path.relative_to(ROOT).as_posix()
    return Source(rel, module_name(rel), ast.parse(path.read_text(encoding="utf-8"), filename=rel))


@cache
def product_sources() -> tuple[Source, ...]:
    found = []
    for scan in SCAN_ROOTS:
        for path in sorted((ROOT / scan).rglob("*.py")):
            if not _SKIP_PARTS & set(path.relative_to(ROOT).parts):
                found.append(_parse(path))
    return tuple(found)


@cache
def suite_sources() -> tuple[Source, ...]:
    paths = sorted((ROOT / "tests").rglob("*.py"))
    return tuple(_parse(p) for p in paths if "__pycache__" not in p.parts)


def test_the_scan_covers_the_known_files():
    assert {source.rel for source in product_sources()} >= CANARIES
    assert THIS_FILE in {source.rel for source in suite_sources()}


# --- 函式與它自己的節點 -----------------------------------------------------


@dataclass(frozen=True)
class Function:
    module: str
    qualname: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    owner: str | None  # 所在類別的 qualname（方法才有）

    @property
    def key(self) -> str:
        return f"{self.module}:{self.qualname}"


def functions(module: str, tree: ast.Module) -> Iterator[Function]:
    """模組裡所有函式與方法（含巢狀；qualname 照 Python 的寫法）."""

    def visit(node: ast.AST, prefix: str, owner: str | None) -> Iterator[Function]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}{child.name}"
                yield Function(module, qualname, child, owner)
                yield from visit(child, f"{qualname}.<locals>.", None)
            elif isinstance(child, ast.ClassDef):
                qualname = f"{prefix}{child.name}"
                yield from visit(child, f"{qualname}.", qualname)
            else:
                yield from visit(child, prefix, owner)

    yield from visit(tree, "", None)


def own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """``node`` 自己的節點（不進巢狀函式；lambda 與類別本體算外層的，方法不算）."""
    pending = list(ast.iter_child_nodes(node))
    while pending:
        child = pending.pop()
        yield child
        if isinstance(child, ast.ClassDef):
            pending.extend(c for c in child.body if not isinstance(c, _NESTED[:2]))
        elif not isinstance(child, _NESTED):
            pending.extend(ast.iter_child_nodes(child))


def scopes(source: Source) -> Iterator[tuple[str, list[ast.AST]]]:
    """（「模組:函式」, 自己的節點）：模組層是 ``<module>``."""
    top = [n for n in source.tree.body if not isinstance(n, _NESTED[:2])]
    module_nodes: list[ast.AST] = []
    for node in top:
        module_nodes.append(node)
        module_nodes.extend(own_nodes(node))
    yield f"{source.module}:<module>", module_nodes
    for fn in functions(source.module, source.tree):
        yield fn.key, list(own_nodes(fn.node))


def module_aliases(tree: ast.Module) -> dict[str, str]:
    """``from x import f as g``：模組裡 ``g`` 就是 ``f``."""
    return {
        alias.asname: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname
    }


def called_name(func: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(func, ast.Name):
        return aliases.get(func.id, func.id)
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def docstring_nodes(tree: ast.Module) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


# --- T1 舊產生函式 ----------------------------------------------------------

OLD_VAULT_GETTERS = frozenset({"get_vault_path", "get_vault_path_for_agent"})
PERSONAL_VAULT = "personal"
# 第一個參數不是常數的呼叫：只能在這些「模組:函式」（今天沒有）
OLD_VAULT_DYNAMIC_CALLERS: frozenset[str] = frozenset()
REMOVED_NAMES = ("get_outputs_path", "get_uploads_path", "get_physical_path")


def _vault_argument(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    return next((k.value for k in call.keywords if k.arg == "vault"), None)


def t1_findings(sources: Iterable[Source]) -> tuple[list[str], set[str]]:
    """（第一個參數是 ``"personal"`` 的呼叫位置, 第一個參數不是常數的「模組:函式」）."""
    constant_personal: list[str] = []
    dynamic: set[str] = set()
    for source in sources:
        aliases = module_aliases(source.tree)
        for key, nodes in scopes(source):
            for node in nodes:
                if not isinstance(node, ast.Call):
                    continue
                if called_name(node.func, aliases) not in OLD_VAULT_GETTERS:
                    continue
                vault = _vault_argument(node)
                if not isinstance(vault, ast.Constant):
                    dynamic.add(key)
                elif vault.value == PERSONAL_VAULT:
                    constant_personal.append(f"{source.rel}:{node.lineno}")
    return constant_personal, dynamic


def test_old_getters_no_longer_give_the_personal_space():
    constant_personal, dynamic = t1_findings(product_sources())
    assert constant_personal == []
    assert dynamic == OLD_VAULT_DYNAMIC_CALLERS


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('def f(rm, i):\n    return rm.get_vault_path("personal", i)\n', (["m.py:2"], set())),
        ('cb = lambda rm: rm.get_vault_path("personal")\n', (["m.py:1"], set())),
        ("def f(rm, v):\n    g = lambda: rm.get_vault_path(v)\n", ([], {"m:f"})),
    ],
)
def test_t1_finds_the_old_getter_in_functions_and_lambdas(text, expected):
    assert t1_findings([Source("m.py", "m", ast.parse(text))]) == expected


def test_removed_getters_do_not_come_back():
    pattern = re.compile(r"\b(" + "|".join(REMOVED_NAMES) + r")\b")
    found = [
        f"{source.rel}: {match}"
        for source in (*product_sources(), *suite_sources())
        if source.rel != THIS_FILE
        for match in pattern.findall((ROOT / source.rel).read_text(encoding="utf-8"))
    ]
    assert found == []
    assert [name for name in REMOVED_NAMES if hasattr(rm, name)] == []


# --- T2 回傳區內路徑的函式要標註 ---------------------------------------------

# RM 的 Z5 產生函式與 zone_paths 的建構方式（遞移計算從這裡開始）
ZONE_SEEDS = frozenset(
    {
        "agent_drive",
        "agent_drive_for",
        "soul_drive",
        "custom_drive",
        "outputs_dir",
        "uploads_dir",
        "anchor_root",
        "zone_rel_from_abs",
        "agent_root",
        "ZonePath",
    }
)
# 在區內值上接的方法與屬性：結果還是區內值
_CHAIN_CALLS = frozenset({"child", "join_rel"})
_CHAIN_ATTRS = frozenset({"parent"})


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(t) for t in target.elts))
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return set()


def _self_attrs(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        return {target.attr} if target.value.id == "self" else set()
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_self_attrs(t) for t in target.elts))
    return set()


def _assignments(fn: Function) -> Iterator[tuple[ast.AST, ast.expr]]:
    """（指定目標, 值）：一般指定、標註指定與 walrus."""
    for node in own_nodes(fn.node):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield target, node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            yield node.target, node.value


class ZoneFlow:
    """哪些函式的回傳值可能是區內路徑（同一個函式內的資料流，跨函式以名稱遞移）."""

    def __init__(self, sources: Iterable[tuple[str, ast.Module]]) -> None:
        trees = list(sources)
        self.containers = container_classes(tree for _module, tree in trees)
        self.functions = [
            (module_aliases(tree), fn) for module, tree in trees for fn in functions(module, tree)
        ]
        self.names: set[str] = set(ZONE_SEEDS)
        self.self_attrs: dict[tuple[str, str], set[str]] = {}
        self.producers: dict[str, Function] = {}
        self._solve()

    def _solve(self) -> None:
        changed = True
        while changed:
            changed = False
            for aliases, fn in self.functions:
                local = self._locals(aliases, fn)
                if fn.owner is not None:
                    found = {
                        attr
                        for target, value in _assignments(fn)
                        if self.is_zone(aliases, fn, value, local)
                        for attr in _self_attrs(target)
                    }
                    attrs = self.self_attrs.setdefault((fn.module, fn.owner), set())
                    if not found <= attrs:
                        attrs |= found
                        changed = True
                if fn.key not in self.producers and self._returns_zone(aliases, fn, local):
                    self.producers[fn.key] = fn
                    # child、join_rel、parent 只在接在區內值後面時才算（不以名稱擴散）
                    if fn.node.name not in _CHAIN_CALLS | _CHAIN_ATTRS:
                        self.names.add(fn.node.name)
                    changed = True

    def unannotated(self) -> list[str]:
        """回傳區內值、但回傳標註沒寫出 ``ZonePath`` 的函式（「模組:函式」）."""
        return sorted(
            key
            for key, fn in self.producers.items()
            if fn.node.returns is None or "ZonePath" not in ast.unparse(fn.node.returns)
        )

    def _locals(self, aliases: dict[str, str], fn: Function) -> set[str]:
        local: set[str] = set()
        while True:
            before = len(local)
            for target, value in _assignments(fn):
                if self.is_zone(aliases, fn, value, local):
                    local |= _target_names(target)
            if len(local) == before:
                return local

    def _returns_zone(self, aliases: dict[str, str], fn: Function, local: set[str]) -> bool:
        return any(
            isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom))
            and node.value is not None
            and self.is_zone(aliases, fn, node.value, local)
            for node in own_nodes(fn.node)
        )

    def is_zone(
        self, aliases: dict[str, str], fn: Function, node: ast.expr, local: set[str]
    ) -> bool:
        """``node`` 的值可能是區內路徑（或裝著區內路徑的容器）."""

        def zone(child: ast.expr) -> bool:
            return self.is_zone(aliases, fn, child, local)

        if isinstance(node, ast.Call):
            name = called_name(node.func, aliases)
            if name in self.names:
                return True
            chained = isinstance(node.func, ast.Attribute) and node.func.attr in _CHAIN_CALLS
            if chained and zone(node.func.value):  # type: ignore[union-attr]
                return True
            arguments = [*node.args, *(k.value for k in node.keywords)]
            return name in self.containers and any(zone(a) for a in arguments)
        if isinstance(node, ast.Attribute):
            if node.attr in _CHAIN_ATTRS and zone(node.value):
                return True
            is_self = isinstance(node.value, ast.Name) and node.value.id == "self"
            owner = (fn.module, fn.owner) if fn.owner is not None else None
            return is_self and owner is not None and node.attr in self.self_attrs.get(owner, set())
        if isinstance(node, ast.Name):
            return node.id in local
        if isinstance(node, ast.BoolOp):
            return any(zone(v) for v in node.values)
        if isinstance(node, ast.IfExp):
            return zone(node.body) or zone(node.orelse)
        if isinstance(node, (ast.Await, ast.NamedExpr, ast.Starred, ast.Subscript)):
            return zone(node.value)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return any(zone(e) for e in node.elts)
        if isinstance(node, ast.Dict):
            return any(zone(v) for v in node.values if v is not None)
        return False


def container_classes(trees: Iterable[ast.Module]) -> frozenset[str]:
    """dataclass 與具名 tuple（建構參數裡有區內值時，整個值也算）."""
    found: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            decorators = {
                ast.unparse(d).split("(")[0].rsplit(".", 1)[-1] for d in node.decorator_list
            }
            bases = {ast.unparse(b).rsplit(".", 1)[-1] for b in node.bases}
            if "dataclass" in decorators or "NamedTuple" in bases:
                found.add(node.name)
    return frozenset(found)


@cache
def product_flow() -> ZoneFlow:
    return ZoneFlow((source.module, source.tree) for source in product_sources())


def test_functions_returning_a_zone_path_say_so():
    assert product_flow().unannotated() == []


def _flow(**modules: str) -> ZoneFlow:
    return ZoneFlow((name, ast.parse(text)) for name, text in modules.items())


# 每一種寫法一個例子：``f`` 的回傳值是區內值、沒有標註，檢查要報
FLOW_FORMS = {
    "direct": "def f(rm, i):\n    return rm.agent_drive(i)\n",
    "assign": "def f(rm, i):\n    d = rm.agent_drive(i)\n    return d\n",
    "annotated_assign": "def f(rm, i):\n    d: object = rm.agent_drive(i)\n    return d\n",
    "walrus": "def f(rm, i):\n    if (d := rm.agent_drive(i)) is not None:\n        return d\n",
    "unpack": "def f(rm, i):\n    d, n = rm.agent_drive(i), 1\n    return d\n",
    "or": "def f(rm, i, other):\n    return other or rm.agent_drive(i)\n",
    "and": "def f(rm, i):\n    return i and rm.agent_drive(i)\n",
    "conditional": "def f(rm, i):\n    return rm.agent_drive(i) if i else None\n",
    "child": "def f(rm, i):\n    return rm.agent_drive(i).child('a')\n",
    "join_rel": "def f(rm, i):\n    d = rm.agent_drive(i)\n    return d.join_rel('a/b')\n",
    "parent": "def f(rm, i):\n    return rm.agent_drive(i).parent\n",
    "await": "async def f(rm, i):\n    return await rm.agent_drive(i)\n",
    "yield": "def f(rm, i):\n    yield rm.agent_drive(i)\n",
    "tuple": "def f(rm, i):\n    return rm.agent_drive(i), 1\n",
    "list": "def f(rm, i):\n    return [rm.agent_drive(i)]\n",
    "dict": "def f(rm, i):\n    return {'d': rm.agent_drive(i)}\n",
    "dataclass": (
        "from dataclasses import dataclass\n@dataclass\nclass Box:\n    d: object\n"
        "def f(rm, i):\n    return Box(rm.agent_drive(i))\n"
    ),
    "named_tuple": (
        "from typing import NamedTuple\nclass Box(NamedTuple):\n    d: object\n"
        "def f(rm, i):\n    return Box(d=rm.agent_drive(i))\n"
    ),
    "transitive": (
        "def g(rm, i):\n    return rm.agent_drive(i)\ndef f(rm, i):\n    return g(rm, i)\n"
    ),
    "constructed": "def f(anchor, parts):\n    return ZonePath(anchor, parts)\n",
    "noqa": "def f(rm, i):\n    return rm.agent_drive(i)  # noqa\n",
}


@pytest.mark.parametrize("form", sorted(FLOW_FORMS))
def test_each_way_of_passing_a_zone_value_along_is_caught(form):
    assert "m:f" in _flow(m=FLOW_FORMS[form]).unannotated()


def test_a_self_attribute_set_in_one_method_counts_in_another():
    flow = _flow(
        m=(
            "class C:\n"
            "    def __init__(self, rm, i):\n        self.d = rm.agent_drive(i)\n"
            "    def g(self):\n        return self.d\n"
        )
    )
    assert flow.unannotated() == ["m:C.g"]


def test_names_follow_imports_across_modules():
    flow = _flow(
        a="def g(rm, i):\n    return rm.agent_drive(i)\n",
        b="from a import g as h\ndef f(rm, i):\n    return h(rm, i)\n",
        c="import a as m\ndef f(rm, i):\n    return m.g(rm, i)\n",
    )
    assert flow.unannotated() == ["a:g", "b:f", "c:f"]


@pytest.mark.parametrize(
    "text",
    [
        "def f(rm, i) -> ZonePath | None:\n    return rm.agent_drive(i)\n",
        "def f(rm, i) -> Optional[ZonePath]:\n    return rm.agent_drive(i)\n",
        "def f(rm, i) -> tuple[ZonePath, int]:\n    return rm.agent_drive(i), 1\n",
        "def f(rm, i) -> bool:\n    return rm.agent_drive(i) is None\n",
        "def f(rm, i) -> str:\n    return rm.agent_drive(i).display()\n",
        # 名稱叫 child 的函式不讓所有 .child() 都變成區內值
        "def child(x):\n    return x\ndef f(p):\n    return p.child('a')\n",
    ],
)
def test_annotated_or_unrelated_returns_are_not_reported(text):
    assert _flow(m=text).unannotated() == []


# --- G1 轉出、整個錨點與錨點名稱的呼叫點 -------------------------------------

G1_CALLABLES = frozenset(
    {
        "zone_handoff",
        "anchor_root",
        "rename_agent_container",
        "remove_custom_model_dir",
        "anchor_dirname",
        "_register_handoff",
        "_release_handoff",
    }
)
G1_METHODS = frozenset({"literal", "resolved"})  # ZoneRoots 的方法：只認屬性寫法
G1_ATTRS = frozenset({"zone_abs"})  # 區的例外帶的實體路徑
_ATTRIBUTE_BUILTINS = frozenset({"getattr", "hasattr", "setattr", "delattr"})
_G1_ALL = G1_CALLABLES | G1_METHODS | G1_ATTRS
# 「模組:函式」→（名稱與次數, 種類與何時釋放）
G1_SITES: dict[str, tuple[dict[str, int], str]] = {
    "src.routers.observability:get_memory_health": (
        {"anchor_root": 1},
        "整個錨點：健康檢查只看個人空間的根存不存在",
    ),
    "src.services.isolation.home_wipe:_wipe_in_executor": (
        {"zone_handoff": 1},
        "SUBPROCESS_CWD：執行器裡清空家目錄的行程，行程結束時釋放",
    ),
    "src.services.isolation.status:IsolationMonitor._agent_drive_in_executor": (
        {"anchor_dirname": 1},
        "錨點名稱：對照執行器掛的資料夾（不碰檔案）",
    ),
    "src.services.resource_manager:ResourceManager._zone_target": (
        {"resolved": 1},
        "RM 區內方法共用的解析",
    ),
    "src.services.resource_manager:ResourceManager.zone_rel_from_abs": (
        {"literal": 1, "resolved": 1},
        "RM：絕對路徑換回區內段落",
    ),
    "src.services.resource_manager:ResourceManager.zone_handoff": (
        {"_register_handoff": 1, "_release_handoff": 1},
        "交接登記表的登記點（R7）：離開時取消",
    ),
    "src.services.resource_manager:ResourceManager.rename_agent_container": (
        {"resolved": 1},
        "RM：改名整個代理人資料夾（R10）",
    ),
    "src.services.resource_manager:ResourceManager.remove_custom_model_dir": (
        {"resolved": 1},
        "RM：刪除整個自訂模型資料夾（R10）",
    ),
    "src.services.review.evidence:_run_cmd": (
        {"zone_handoff": 1},
        "SUBPROCESS_CWD：互審驗證指令的 cwd，行程結束時釋放",
    ),
    "src.services.zone_file_response:_close": (
        {"_release_handoff": 1},
        "PROC_FD：回應送完或失敗時取消登記",
    ),
    "src.services.zone_file_response:ZoneFileResponse.__init__": (
        {"_register_handoff": 1},
        "PROC_FD：Linux 以 /proc/self/fd 回檔的唯一登記點",
    ),
    "src.services.zone_file_response:PathZoneFileResponse.__call__": (
        {"zone_handoff": 1},
        "WINDOWS_FILE_RESPONSE：Windows 回檔，送完時釋放",
    ),
    "src.tools.terminal:_execute_core": (
        {"zone_handoff": 1},
        "SUBPROCESS_CWD：終端指令的 cwd，行程結束時釋放",
    ),
    "src.utils.custom_io:delete_custom_dir": (
        {"remove_custom_model_dir": 1},
        "刪除自訂模型（人類發起）",
    ),
    "src.utils.custom_io:move_custom_dir": (
        {"rename_agent_container": 1},
        "自訂模型改名與回滾（人類發起）",
    ),
    "src.utils.permission_repair:permission_repair.<locals>.decorate.<locals>.wrapper": (
        {"getattr:zone_abs": 1},
        "權限自癒讀例外帶的實體路徑（不回給模型）",
    ),
    "memory_server.utils.no_follow:ZoneLinkRefusedError.__init__": (
        {"zone_abs": 1},
        "區的例外記下實體路徑（只給權限自癒）",
    ),
    "memory_server.utils.no_follow:zone_errors": (
        {"hasattr:zone_abs": 1, "zone_abs": 1},
        "OSError 轉成區的例外時保留實體路徑",
    ),
    "memory_server.utils.zone_paths:ensure_anchor": ({"literal": 1}, "定義處"),
    "memory_server.utils.zone_paths:_anchor_bases": ({"literal": 1}, "定義處"),
    "scripts.manage_backup:<module>": (
        {"anchor_dirname": 2},
        "備份腳本：另外不跟隨連結地備份與還原的兩個代理人資料夾",
    ),
    "scripts.manage_backup:_write_agent_folders": (
        {"resolved": 2},
        "備份腳本：解析後的錨點（人做的連結照跟隨，R9）",
    ),
}


def _g1_names(nodes: Iterable[ast.AST], aliases: dict[str, str]) -> Counter[str]:
    found: Counter[str] = Counter()
    for node in nodes:
        if isinstance(node, ast.Attribute) and node.attr in _G1_ALL:
            found[node.attr] += 1
        elif isinstance(node, ast.Name) and aliases.get(node.id, node.id) in G1_CALLABLES:
            found[aliases.get(node.id, node.id)] += 1
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _ATTRIBUTE_BUILTINS
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _G1_ALL
        ):
            found[f"{node.func.id}:{node.args[1].value}"] += 1
    return found


def g1_sites(sources: Iterable[Source]) -> dict[str, dict[str, int]]:
    found = {}
    for source in sources:
        aliases = module_aliases(source.tree)
        for key, nodes in scopes(source):
            names = _g1_names(nodes, aliases)
            if names:
                found[key] = dict(names)
    return found


def test_handoffs_whole_anchors_and_anchor_names_are_used_only_where_listed():
    assert g1_sites(product_sources()) == {key: names for key, (names, _why) in G1_SITES.items()}


def test_every_g1_site_says_why():
    assert [key for key, (_names, why) in G1_SITES.items() if not why.strip()] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("def f(rm, zp):\n    return rm.zone_handoff(zp, k)\n", {"m:f": {"zone_handoff": 1}}),
        ("def f(rm):\n    g = rm.zone_handoff\n", {"m:f": {"zone_handoff": 1}}),
        (
            "from z import anchor_root as a\ndef f(x):\n    return a(x)\n",
            {"m:f": {"anchor_root": 1}},
        ),
        ("def f(exc):\n    return getattr(exc, 'zone_abs')\n", {"m:f": {"getattr:zone_abs": 1}}),
        ("def f(roots, a):\n    return roots.resolved(a)\n", {"m:f": {"resolved": 1}}),
        ("x = anchor_dirname(A)\n", {"m:<module>": {"anchor_dirname": 1}}),
        # lambda 本體算在外層
        (
            "opener = lambda p: zp._register_handoff(p, k)\n",
            {"m:<module>": {"_register_handoff": 1}},
        ),
        (
            "def f(rm):\n    return lambda zp: rm.zone_handoff(zp, k)\n",
            {"m:f": {"zone_handoff": 1}},
        ),
        # 叫 resolved 的變數不算
        ("def f(p):\n    resolved = p\n    return resolved\n", {}),
    ],
)
def test_g1_finds_every_way_of_reaching_a_listed_name(text, expected):
    assert g1_sites([Source("m.py", "m", ast.parse(text))]) == expected


# --- G2 錨點名稱 --------------------------------------------------------------

PERSONAL_NAME = anchor_dirname(Anchor.PERSONAL_DRIVE)
CUSTOM_NAME = anchor_dirname(Anchor.CUSTOM_DRIVE)
DRIVE_NAME = zone_paths._CUSTOM_DRIVE_DIR
CUSTOM_AND_DRIVE = f"{CUSTOM_NAME}+{DRIVE_NAME}"
# 「模組:函式:名稱」→（次數, 理由）
G2_SITES: dict[str, tuple[int, str]] = {
    f"src.routers.souls:create_soul:{PERSONAL_NAME}": (
        1,
        "建立靈魂失敗時要回滾的清單標籤（不是路徑）",
    ),
    f"src.sandbox_kernel.spec:<module>:{PERSONAL_NAME}": (
        1,
        "隔離核心只用標準函式庫、不能匯入 zone_paths；判斷資料根形狀用（Z10 核對）",
    ),
    f"src.services.isolation.executor_session:<module>:{PERSONAL_NAME}": (
        1,
        "執行器的版面（標準函式庫，Z10 核對）",
    ),
    f"src.services.isolation.repair:reserved_roots:{PERSONAL_NAME}": (
        1,
        "權限自癒的 W1 根（Z10 核對）",
    ),
    f"src.services.isolation.status:expected_executor_roots:{PERSONAL_NAME}": (
        1,
        "執行器應該掛的根（Z10 核對）",
    ),
    f"src.utils.mcp_category:<module>:{DRIVE_NAME}": (1, "MCP 伺服器分類的關鍵字（不是路徑）"),
    f"src.utils.mcp_origin:personal_drive_root:{PERSONAL_NAME}": (
        1,
        "代理人影響得到的位置（Z10 雙向核對）",
    ),
    f"src.utils.soul_io:<module>:{PERSONAL_NAME}": (1, "靈魂儲存的三個 base 名稱"),
    f"src.utils.soul_io:remove_soul_storage_dir:{PERSONAL_NAME}": (
        1,
        "個人空間這個 base 改走區內方法（_remove_soul_drive）",
    ),
    f"permission_init:<module>:{PERSONAL_NAME}": (
        1,
        "Heartbeat 容器的權限初始化（標準函式庫，Z10 核對）",
    ),
}


def _segments(nodes: Iterable[ast.AST], docstrings: set[int]) -> Counter[str]:
    found: Counter[str] = Counter()
    for node in nodes:
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            if id(node) in docstrings:
                continue
            value = node.value
            text = value.decode("latin-1") if isinstance(value, bytes) else value
            found.update(text.replace("\\", "/").split("/"))
    return found


def g2_sites(sources: Iterable[Source]) -> dict[str, int]:
    found = {}
    for source in sources:
        if source.rel == ZONE_PATHS:
            continue
        docstrings = docstring_nodes(source.tree)
        for key, nodes in scopes(source):
            segments = _segments(nodes, docstrings)
            for name in (PERSONAL_NAME, DRIVE_NAME):
                if segments[name]:
                    found[f"{key}:{name}"] = segments[name]
            if segments[CUSTOM_NAME] and segments[DRIVE_NAME]:
                found[f"{key}:{CUSTOM_AND_DRIVE}"] = 1
    return found


def test_anchor_folder_names_appear_only_where_listed():
    assert g2_sites(product_sources()) == {key: n for key, (n, _why) in G2_SITES.items()}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("p = root / 'personal_drive' / x\n", {"m:<module>:personal_drive": 1}),
        ("def f(x):\n    return f'{x}/personal_drive/a'\n", {"m:f:personal_drive": 1}),
        ("def f(x):\n    return get_custom_dir(x) / 'drive'\n", {"m:f:drive": 1}),
        (
            "def f(x):\n    return x / 'custom' / i / 'drive'\n",
            {"m:f:drive": 1, f"m:f:{CUSTOM_AND_DRIVE}": 1},
        ),
        ("def f(x):\n    return x == b'personal_drive'\n", {"m:f:personal_drive": 1}),
        (
            "key = lambda a: os.path.join(d, 'personal_drive', a)\n",
            {"m:<module>:personal_drive": 1},
        ),
        ("def f():\n    '''personal_drive/x'''\n", {}),
    ],
)
def test_g2_finds_anchor_names_in_every_kind_of_literal(text, expected):
    assert g2_sites([Source("m.py", "m", ast.parse(text))]) == expected


# --- 其他 ---------------------------------------------------------------------


def _annotation_nodes(tree: ast.Module) -> set[int]:
    roots: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
            roots.append(node.returns)
        elif isinstance(node, ast.arg) and node.annotation or isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
    return {id(n) for root in roots for n in ast.walk(root)}


def test_product_code_does_not_decide_by_pathlike():
    """區內值不是 ``os.PathLike``：拿它判斷會把區內值和一般路徑混在一起（只准出現在型別標註）."""
    found = []
    for source in product_sources():
        annotations = _annotation_nodes(source.tree)
        for node in ast.walk(source.tree):
            named = (isinstance(node, ast.Attribute) and node.attr == "PathLike") or (
                isinstance(node, ast.Name) and node.id == "PathLike"
            )
            if named and id(node) not in annotations:
                found.append(f"{source.rel}:{node.lineno}")
    assert found == []


# 區例外紀錄（R13）與絆線（Z9）的內部：只有這些測試檔能碰，不然就是讓測試不紅的開關
RECORDS_PUBLIC = frozenset({"expect_zone_refusal", "raises_zone"})
RECORDS_INTERNAL_USERS = frozenset(
    {
        "tests/conftest.py",
        "tests/support/zone_tripwire.py",
        "tests/unit/test_zone_records.py",
        # 直接呼叫 conftest 的 session hook：換掉 remaining，不取走別的測試還沒結算的紀錄
        "tests/unit/test_session_sandbox_cleanup.py",
    }
)
TRIPWIRE_USERS = frozenset(
    {
        "tests/conftest.py",
        "tests/unit/test_zone_tripwire.py",
        "tests/lint/test_ci_pr_checks.py",  # 只讀 FULL_RUN_ENV
    }
)
SELF_TEST_TAKER = "tests/unit/test_zone_tripwire.py"


def _imports(tree: ast.Module) -> Iterator[tuple[str, str]]:
    """（模組, 名稱）：``from m import n`` 與 ``import m``（名稱是空字串）."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                yield node.module, alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, ""


def _mentions(source: Source, module: str) -> set[str]:
    """``source`` 用到 ``tests.support.<module>`` 的哪些名稱（整個模組的用法記成 ``*``）."""
    dotted = f"tests.support.{module}"
    found: set[str] = set()
    for imported, name in _imports(source.tree):
        if imported == dotted:
            found.add(name or "*")
        elif imported == "tests.support" and name == module:
            found.add("*")
    for node in ast.walk(source.tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, (ast.Name, ast.Attribute)):
            owner = node.value.id if isinstance(node.value, ast.Name) else node.value.attr
            if owner == module:
                found.add(node.attr)
        if isinstance(node, ast.Constant) and node.value == dotted:
            found.add("*")  # importlib.import_module("tests.support.…")
        if (
            isinstance(node, ast.Call)
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and _last_name(node.args[0]) == module
        ):
            found.add(str(node.args[1].value))  # monkeypatch.setattr(x.<module>, "名稱", …)
    return found


def _last_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def test_only_listed_tests_reach_into_the_zone_records_and_the_tripwire():
    internal = {
        source.rel
        for source in suite_sources()
        if _mentions(source, "zone_records") - RECORDS_PUBLIC - {"*"}
        and source.rel != "tests/support/zone_records.py"
    }
    assert internal <= RECORDS_INTERNAL_USERS
    tripwire = {
        source.rel
        for source in suite_sources()
        if _mentions(source, "zone_tripwire") and source.rel != "tests/support/zone_tripwire.py"
    }
    assert tripwire == TRIPWIRE_USERS
    takers = {
        source.rel
        for source in suite_sources()
        if "take_records_for_self_test" in _mentions(source, "zone_tripwire")
    }
    assert takers == {SELF_TEST_TAKER}


LINT_SSOT = ROOT / "scripts/lint_ssot.py"
# lint_ssot 以檔名（不含資料夾）放行 I/O 一類規則：新增一個同名檔就會被放行，所以整份釘住
LINT_SSOT_WHITELIST = frozenset(
    {
        "config.py",
        "bootstrap.py",
        "time_utils.py",
        "conftest.py",
        "update_docs.py",
        "export_schema.py",
        "resource_manager.py",
        "config_utils.py",
        "terminal.py",
        "soul_io.py",
        "evolution_security.py",
        "custom_io.py",
        "core_file_ops.py",
        "no_follow.py",
        "zone_paths.py",
        "test_ssot_guard_path.py",
        "test_ssot_guard_io.py",
        "test_ssot_guard_local_import.py",
        "smoke_mcp_lifecycle.py",
        "smoke_io_prison.py",
        "smoke_ldh_handshake.py",
        "smoke_mission_awareness.py",
        "smoke_group_dispatcher.py",
        "smoke_soul_recovery.py",
        "smoke_agent_interaction.py",
        "test_api_schema_sync.py",
        "update_openapi.py",
        "smoke_autonomy_enforcement.py",
        "run_all_smoke_tests.py",
        "smoke_chat.py",
        "smoke_frontend_persistence.py",
        "smoke_memory_tools.py",
        "smoke_model_governance.py",
        "smoke_pulse_rhythm.py",
        "manage_backup.py",
        "awaken.py",
        "test_lint_ssot.py",
        "logging_config.py",
        "test_h3_h4_security.py",
        "test_dispatch_server_tools.py",
        "test_mcp_dispatch_tools.py",
        "test_mission_manager_bugfixes.py",
        "test_dispatch_session.py",
        "test_dispatch_mission_token.py",
        "test_dispatch_hooks.py",
        "test_dispatch_board_integrity.py",
        "test_dispatch_content_sanitization.py",
        "test_heartbeat_config_dir_mount.py",
        "test_docker_compose_config.py",
        "test_env_persistence_anchor.py",
    }
)


def test_the_lint_ssot_whitelist_is_pinned():
    tree = ast.parse(LINT_SSOT.read_text(encoding="utf-8"))
    values = [
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "WHITELIST_FILES" for t in node.targets)
    ]
    assert values == [LINT_SSOT_WHITELIST]


# --- T3 測試不能替換區內產生函式與 data 根 -----------------------------------

# RM 的 Z5 方法：本體呼叫過它們的函式也不能替換
Z5_METHODS = frozenset(
    {"agent_drive", "agent_drive_for", "soul_drive", "custom_drive", "outputs_dir", "uploads_dir"}
)
# data 根的所有覆寫方式：設定與環境變數、Proxy 與 Memory 的取得函式、RM 的根來源、模組層的
# resource_manager 與 Memory 的 resource proxy
DATA_ROOT_NAMES = frozenset(
    {
        "MEMORY_APP_DATA_DIR",
        "TOUKA_SANDBOX_STATE_DIR",
        "get_app_data_dir",
        "_get_app_data_dir",
        "get_base_path",
        "set_roots_provider",
        "resource_manager",
        "get_resource_proxy",
        "_RESOURCE_PROXY_CACHE",
    }
)
# zone_paths 的模組層名稱裡和別的模組撞名、替換也不影響區判斷的
ZONE_PATHS_GENERIC_NAMES = frozenset({"logger"})
# 直接呼叫就會改區判斷、交接或觀察者的函式（不必經 monkeypatch）
T3_DIRECT_CALLS = frozenset(
    {
        "set_roots_provider",
        "_register_handoff",
        "_release_handoff",
        "add_observer",
        "remove_observer",
    }
)
_PATCHERS = frozenset({"patch", "object", "dict", "multiple"})
_ENV_SELECTION = "測 Memory 選哪個 .env：data 根的環境變數有沒有設是這個測試的輸入"
_DATA_ROOT_PARSING = "測 Memory 解析 data 根的環境變數本身（Docker 下的絕對與相對值）"
# 「測試模組:函式」→（次數, 理由）；其他測試換 data 根一律用 ``zone_data_root`` 或
# ``tests.support.zone_roots.point_zone_roots``
_ZONE_SELF_TEST = "測 zone_paths 自己：根的來源、交接登記表或觀察者本身就是被測的東西"
_OBSERVE = "測試期間加上、結束時拿掉的觀察者，只記下區的事件，不改判斷"
_ALIAS = "模擬另一個掛載路徑（同一個檔案身分）：這台機器不能做 bind mount"
T3_ALLOWED: dict[str, tuple[int, str]] = {
    "tests.conftest:<module>": (
        2,
        "session 沙盒的 data 根（匯入任何產品程式之前）；R13 的區例外紀錄掛成觀察者",
    ),
    "tests.conftest:setup_test_environment": (1, "每個測試開始前把 data 根還原成 session 沙盒"),
    "tests.support.zone_roots:point_zone_roots": (
        3,
        "換 data 根的唯一方式（zone_data_root 與 Linux 隔離測試用）：設定、環境變數、state 根",
    ),
    # 下面幾個測的是 data 根的解析本身：環境變數有沒有設、設成什麼就是輸入
    "tests.unit.test_memory_env_selection:"
    "test_absent_or_empty_explicit_keeps_existing_initial_discovery": (1, _ENV_SELECTION),
    "tests.unit.test_memory_env_selection:"
    "test_explicit_environment_never_uses_implicit_discovery": (2, _ENV_SELECTION),
    "tests.unit.test_memory_env_selection:test_no_explicit_keeps_bounded_three_level_fallback": (
        1,
        _ENV_SELECTION,
    ),
    "tests.unit.test_smoke_production_repairs:"
    "test_memory_app_data_dir_keeps_docker_default_for_relative_override": (1, _DATA_ROOT_PARSING),
    "tests.unit.test_smoke_production_repairs:"
    "test_memory_app_data_dir_preserves_explicit_absolute_override_in_docker": (
        1,
        _DATA_ROOT_PARSING,
    ),
    # 下面是 zone_paths 的模組層名稱與直接呼叫（ADR-0293 DEV-B 第一輪）
    "tests.unit.test_memory_scrub_endpoint_security:"
    "test_e15_another_reset_is_refused_while_a_scrub_or_reset_runs": (
        1,
        "重置 primitives 自己的 ``_identity``（查註冊身分），和 zone_paths 的同名、不是區判斷",
    ),
    "tests.unit.test_no_follow_zone:events": (2, _OBSERVE),
    "tests.unit.test_no_follow_zone:test_iter_regular_files_uses_the_registered_roots": (
        2,
        "測走訪用的是登記的根：換成這個測試的根，再拿掉看它拒絕",
    ),
    "tests.unit.test_no_follow_zone:test_walk_pruned_knows_an_anchor_by_identity": (1, _ALIAS),
    "tests.unit.test_resource_manager_string_zone:"
    "test_a_drive_met_during_the_walk_refuses_the_whole_delete_and_archive": (
        1,
        "關掉事前的包含判斷，證明走訪途中遇到區根仍整個拒絕（第二道檢查）",
    ),
    "tests.unit.test_resource_manager_zone:events": (2, _OBSERVE),
    "tests.unit.test_zone_paths:_same_identity": (1, _ALIAS),
    "tests.unit.test_zone_paths:events": (2, _OBSERVE),
    "tests.unit.test_zone_paths:test_current_roots_comes_only_from_the_provider": (
        2,
        _ZONE_SELF_TEST,
    ),
    "tests.unit.test_zone_paths:test_handoff_registry_compares_normalized_paths": (
        2,
        _ZONE_SELF_TEST,
    ),
    "tests.unit.test_zone_paths:test_handoff_registry_counts_and_releases": (5, _ZONE_SELF_TEST),
    "tests.unit.test_zone_paths:test_observer_errors_do_not_reach_the_caller": (
        2,
        _ZONE_SELF_TEST,
    ),
    "tests.unit.test_zone_tripwire:test_an_error_in_the_hook_is_recorded": (
        1,
        "絆線自己匯入的 ``current_roots`` 換成會丟例外的，測 hook 出錯也記一筆",
    ),
}


def _literal(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_environ(node: ast.expr) -> bool:
    return ast.unparse(node) in ("os.environ", "environ")


def _patched_names(call: ast.Call) -> list[str]:
    """這個呼叫替換的名稱（monkeypatch、mock.patch 與 mocker 的各種寫法）."""
    func, args = call.func, call.args
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    chain = ast.unparse(func)
    found: list[str] = []
    if name in ("setattr", "delattr") and args:
        text = _literal(args[0])
        if text is not None and "." in text:
            found.append(text.rsplit(".", 1)[-1])  # 點分路徑
        elif len(args) >= 2 and _literal(args[1]) is not None:
            found.append(_literal(args[1]))  # type: ignore[arg-type]
    elif name in ("setenv", "delenv") and args and _literal(args[0]) is not None:
        found.append(_literal(args[0]))  # type: ignore[arg-type]
    elif name in ("setitem", "delitem") and len(args) >= 2 and _is_environ(args[0]):
        if _literal(args[1]) is not None:
            found.append(_literal(args[1]))  # type: ignore[arg-type]
    elif name in _PATCHERS and "patch" in chain:
        if name == "patch" and args and _literal(args[0]) is not None:
            found.append(_literal(args[0]).rsplit(".", 1)[-1])  # type: ignore[union-attr]
        elif name == "object" and len(args) >= 2 and _literal(args[1]) is not None:
            found.append(_literal(args[1]))  # type: ignore[arg-type]
        elif name in ("dict", "multiple"):
            for value in args[1:]:
                if isinstance(value, ast.Dict):
                    found.extend(t for k in value.keys if (t := _literal(k)) is not None)
            found.extend(k.arg for k in call.keywords if k.arg and k.arg not in ("clear", "new"))
    return found


def _assigned_names(node: ast.AST) -> list[str]:
    """直接指定或刪除：``os.environ["名稱"] = …``、``obj.名稱 = …``、``del os.environ[…]``."""
    if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    elif isinstance(node, ast.Delete):
        targets = node.targets
    else:
        return []
    found = []
    for target in targets:
        if isinstance(target, ast.Subscript) and _is_environ(target.value):
            if (text := _literal(target.slice)) is not None:
                found.append(text)
        elif isinstance(target, ast.Attribute) and not (
            isinstance(target.value, ast.Name) and target.value.id == "self"
        ):
            found.append(target.attr)
    return found


def zone_paths_module_names() -> frozenset[str]:
    """``zone_paths.py`` 模組層定義的所有名稱（含私有的根、身分、交接與觀察者狀態）."""
    tree = next(s.tree for s in product_sources() if s.rel == ZONE_PATHS)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(n.id for n in targets if isinstance(n, ast.Name))
    return frozenset(names - ZONE_PATHS_GENERIC_NAMES)


@cache
def t3_protected() -> frozenset[str]:
    """區內產生函式（T2 的集合）、回傳標註有 ``ZonePath`` 的、呼叫過 Z5 方法的、data 根，加上
    ``zone_paths`` 的模組層名稱."""
    names = set(product_flow().names) | DATA_ROOT_NAMES | zone_paths_module_names()
    for source in product_sources():
        aliases = module_aliases(source.tree)
        for fn in functions(source.module, source.tree):
            if fn.node.returns is not None and "ZonePath" in ast.unparse(fn.node.returns):
                names.add(fn.node.name)
            calls = (n for n in own_nodes(fn.node) if isinstance(n, ast.Call))
            if any(called_name(c.func, aliases) in Z5_METHODS for c in calls):
                names.add(fn.node.name)
    return frozenset(names)


def t3_sites(sources: Iterable[Source], protected: frozenset[str]) -> dict[str, int]:
    found: Counter[str] = Counter()
    for source in sources:
        for key, nodes in scopes(source):
            for node in nodes:
                names = _patched_names(node) if isinstance(node, ast.Call) else []
                if isinstance(node, ast.Call):
                    called = called_name(node.func, {})
                    names += [called] if called in T3_DIRECT_CALLS else []
                names += _assigned_names(node)
                found[key] += sum(1 for name in names if name in protected)
    return {key: n for key, n in found.items() if n}


def test_tests_do_not_replace_zone_producers_or_the_data_root():
    sites = t3_sites(suite_sources(), t3_protected())
    assert sites == {key: n for key, (n, _why) in T3_ALLOWED.items()}


def test_t3_covers_the_producers_and_the_data_root():
    protected = t3_protected()
    assert protected >= Z5_METHODS | DATA_ROOT_NAMES | T3_DIRECT_CALLS
    assert {"_agent_artifact_root", "_get_mission_path", "_own_drive"} <= protected
    state = {"_roots_provider", "_identity", "_handoffs", "_observers", "zone_of", "contains_zone"}
    assert state | {"current_roots", "is_handed_off"} <= protected
    assert "logger" not in protected


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ('def t(m):\n    m.setattr(obj, "agent_drive", f)\n', "agent_drive"),
        ('def t(m):\n    m.setattr("src.x.agent_drive", f)\n', "agent_drive"),
        ('def t(m):\n    m.delattr(obj, "get_base_path")\n', "get_base_path"),
        ('def t(m):\n    m.setenv("MEMORY_APP_DATA_DIR", x)\n', "MEMORY_APP_DATA_DIR"),
        ('def t(m):\n    m.delenv("MEMORY_APP_DATA_DIR")\n', "MEMORY_APP_DATA_DIR"),
        ('def t(m):\n    m.setitem(os.environ, "MEMORY_APP_DATA_DIR", x)\n', "MEMORY_APP_DATA_DIR"),
        ('def t():\n    with patch("src.x.resource_manager"):\n        pass\n', "resource_manager"),
        ('def t():\n    with mock.patch.object(rm, "soul_drive"):\n        pass\n', "soul_drive"),
        (
            'def t():\n    patch.dict(os.environ, {"MEMORY_APP_DATA_DIR": x})\n',
            "MEMORY_APP_DATA_DIR",
        ),
        ("def t():\n    patch.dict(os.environ, MEMORY_APP_DATA_DIR=x)\n", "MEMORY_APP_DATA_DIR"),
        ('def t():\n    patch.multiple("src.x", get_resource_proxy=f)\n', "get_resource_proxy"),
        ('def t(mocker):\n    mocker.patch("src.x.get_app_data_dir")\n', "get_app_data_dir"),
        ('def t(mocker):\n    mocker.patch.object(rm, "uploads_dir")\n', "uploads_dir"),
        ('def t():\n    os.environ["MEMORY_APP_DATA_DIR"] = x\n', "MEMORY_APP_DATA_DIR"),
        ('def t():\n    del os.environ["MEMORY_APP_DATA_DIR"]\n', "MEMORY_APP_DATA_DIR"),
        ("def t():\n    settings.MEMORY_APP_DATA_DIR = x\n", "MEMORY_APP_DATA_DIR"),
        ("def t():\n    module.resource_manager = fake\n", "resource_manager"),
        ("def t():\n    set_roots_provider(lambda: roots)\n", "set_roots_provider"),
        ("def t():\n    zp._register_handoff(p, k)\n", "_register_handoff"),
        ("def t(m):\n    f = lambda: m.setattr(zp, \"_identity\", g)\n", "_identity"),
    ],
)
def test_t3_finds_every_way_of_replacing_a_name(text, name):
    assert t3_sites([Source("m.py", "m", ast.parse(text))], frozenset({name})) == {"m:t": 1}


@pytest.mark.parametrize(
    "text",
    [
        'def t(m):\n    m.setattr(rm, "exists", f)\n',  # 不在清單上的名稱
        'def t(m):\n    m.setenv("TOUKA_ENV_FILE", x)\n',
        "def t(self):\n    self.resource_manager = rm\n",  # 測試類別自己的屬性
        'def t():\n    value = os.environ["MEMORY_APP_DATA_DIR"]\n',  # 只是讀
    ],
)
def test_t3_leaves_other_names_and_plain_reads_alone(text):
    assert t3_sites([Source("m.py", "m", ast.parse(text))], t3_protected()) == {}
