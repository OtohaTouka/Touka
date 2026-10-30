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
  改根、交接登記與觀察者的函式也算），交出區字串與 ST 的函式和 TF 的兩份名單也不能換；只有明列的
  fixture 與明列的例外。
- G1：轉出、整個錨點（鍵帶錨點名稱）、錨點名稱、交接登記與 ``zone_abs`` 的呼叫點只在明列的
  「模組:函式」，雙向相等；``zone_text`` 與 TF 的產生函式、接收函式的呼叫點照解析到的函式計次。
- G2：錨點的資料夾名稱（含 ``docgen_output``、``sandbox_state``、``managed``）只在 ``zone_paths``
  與明列的「模組:函式:名稱」，次數也釘住。
- G3：``ZonePath``、``agent_root`` 只在 ``zone_paths`` 與 RM 引用（標註、型別判斷與 import 不算）；
  ``.__class__`` 與 ``type(…)(…)`` 逐一釘住。
- TF：區字串（``zone_text``）在函式內與跨函式的流向由程式算到不動點；算出的產生函式與接收函式
  分別等於釘住的名單，解析不到又同名的呼叫列進同名不同物。
- G4：區字串只能去列名的去處（``G4_DESTINATIONS``）或明列的「模組:函式」出口（``G4_ALLOWED``），
  存到函式外也算出口。
- 其他：產品不用 ``os.PathLike`` 判斷；測試的區例外紀錄與絆線的內部只有明列的測試檔能碰；
  ``lint_ssot.py`` 的 I/O 白名單與白名單檔名在 repo 裡對到的檔都釘住。
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest
from memory_server.utils import zone_paths
from memory_server.utils.zone_paths import Anchor, anchor_dirname
from src.services.resource_manager import resource_manager as rm

from tests.lint.test_ci_pr_checks import PRODUCT_SOURCE_ROOTS
from tests.lint.test_tests_readme_listing import git_ls_files

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
        # ADR-0293 PR 2：區字串、cwd 交接、ST 的根與 P7 的存取函式
        "zone_text",
        "handoff_cwd",
        "sandbox_state_root",
        "sandbox_state_proxy",
        "sandbox_state_dir",
        "proxy_state_zone",
        "proxy_state_owner",
    }
)
ANCHOR_ROOT = "anchor_root"  # 呼叫時鍵帶錨點名稱（``anchor_root:DOCGEN_OUTPUT``）
G1_METHODS = frozenset({"literal", "resolved"})  # ZoneRoots 的方法：只認屬性寫法
G1_ATTRS = frozenset({"zone_abs"})  # 區的例外帶的實體路徑
_ATTRIBUTE_BUILTINS = frozenset({"getattr", "hasattr", "setattr", "delattr"})
_G1_ALL = G1_CALLABLES | G1_METHODS | G1_ATTRS
# 「模組:函式」→（名稱與次數, 種類與何時釋放）
G1_SITES: dict[str, tuple[dict[str, int], str]] = {
    "src.routers.observability:get_memory_health": (
        {"anchor_root:PERSONAL_DRIVE": 1},
        "整個錨點：健康檢查只看個人空間的根存不存在",
    ),
    "src.protocols.mcp.isolated_stdio:isolated_stdio_client": (
        {"handoff_cwd": 1},
        "SUBPROCESS_CWD：被包住的 MCP 的 cwd 經 handoff_cwd 交出，行程結束時釋放（P6、Q4）",
    ),
    "src.services.evolution_security:EvolutionPathGuard.get_tool_root": (
        {"zone_text": 1},
        "區字串：技能資料夾（P4、Q3）",
    ),
    "src.services.evolution_security:EvolutionPathGuard.run_sandbox_command": (
        {"zone_handoff": 1},
        "SUBPROCESS_CWD：演化指令（venv、pip）的 cwd 是技能資料夾，行程結束時釋放（P6）",
    ),
    "src.services.isolation.home_wipe:_remove_proxy_state": (
        {"proxy_state_owner": 1},
        "P7 存取函式：刪代理人在 ST 的家目錄（區內方法）",
    ),
    "src.services.isolation.home_wipe:_wipe_in_executor": (
        {"zone_handoff": 1},
        "SUBPROCESS_CWD：執行器裡清空家目錄的行程，行程結束時釋放",
    ),
    "src.services.isolation.mcp_profiles:mcp_classification_bases": (
        {"proxy_state_zone": 1},
        "P7 存取函式：ST 沒設時分類基準只有 cwd（只算位置）",
    ),
    "src.services.isolation.profiles:_same_state": (
        {"sandbox_state_root": 1},
        "P7 存取函式共用：核對版面與 RM 的 ST 是否一致",
    ),
    "src.services.isolation.profiles:current_layout": (
        {"sandbox_state_root": 1},
        "版面的 ST 根只從 RM 來（P7）",
    ),
    "src.services.isolation.profiles:proxy_state_owner": (
        {"sandbox_state_dir": 1},
        "P7 存取函式：ST 底下某個種類與擁有者的資料夾",
    ),
    "src.services.isolation.profiles:proxy_state_zone": (
        {"sandbox_state_proxy": 1},
        "P7 存取函式：ST/proxy",
    ),
    "src.services.isolation.profiles:state_dirs": (
        {"proxy_state_owner": 1, "zone_text": 1},
        "ST 的 home／tmp：經存取函式建好後交出區字串（只給 profile）",
    ),
    "src.services.isolation.status:IsolationMonitor._probe": (
        {"proxy_state_zone": 1},
        "P7 存取函式：探測 ST/proxy 可不可寫（區內方法）",
    ),
    "src.services.isolation.status:_rename_proxy_tmp": (
        {"proxy_state_zone": 1},
        "P7 存取函式：開機時把上次的 tmp 改名待刪（區內方法）",
    ),
    "src.services.isolation.status:_repair_state_dir": (
        {"proxy_state_zone": 1, "zone_text": 1},
        "P7 存取函式：ST/proxy 不可寫時交給權限自癒（區字串的出口見 G4_ALLOWED）",
    ),
    "src.services.isolation.status:_state_issue": (
        {"proxy_state_zone": 1},
        "P7 存取函式：判斷 ST 的狀態",
    ),
    "src.services.isolation.status:IsolationMonitor._selftest_in_state": (
        {"zone_handoff": 1},
        "SUBPROCESS_CWD：ST 的自測資料夾交給啟動器，自測結束時釋放（ADR-0293 PR 2）",
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
    "src.services.resource_manager:ResourceManager.handoff_cwd": (
        {"zone_handoff": 1},
        "SUBPROCESS_CWD：被包住的 MCP 的 cwd 字面在區裡時經 zone_handoff 交出（P6）",
    ),
    "src.services.resource_manager:ResourceManager.sandbox_state_dir": (
        {"sandbox_state_root": 1},
        "RM 定義處：ST 底下的產生函式",
    ),
    "src.services.resource_manager:ResourceManager.sandbox_state_proxy": (
        {"sandbox_state_root": 1},
        "RM 定義處：ST/proxy 的產生函式",
    ),
    "src.services.resource_manager:ResourceManager.zone_text": (
        {"resolved": 1},
        "區字串（P4）：解析後的錨點接上字面段落，只交給別人或拿來比對",
    ),
    "src.services.resource_manager:ResourceManager.rename_agent_container": (
        {"resolved": 1},
        "RM：改名整個代理人資料夾（R10）",
    ),
    "src.services.resource_manager:ResourceManager.remove_custom_model_dir": (
        {"resolved": 1},
        "RM：刪除整個自訂模型資料夾（R10）",
    ),
    "src.services.scorecard.artifact_verify:verify_artifact": (
        {"anchor_root:DOCGEN_OUTPUT": 1},
        "整個錨點：文件輸出區，只拿來把產出檔的絕對路徑換成區內路徑（ADR-0293 PR 2 Q6）",
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
    "src.tools.builtin.evolution:_venv_python": (
        {"zone_text": 1},
        "區字串：技能 venv 的 python（子行程 argv、部署條目的 command）",
    ),
    "src.tools.builtin.evolution:_verify_mcp": (
        {"zone_text": 1},
        "區字串：test_wrapper.py 的位置（驗證的 argv）",
    ),
    "src.tools.builtin.evolution:request_mcp_creation": (
        {"zone_text": 1},
        "區字串：部署條目 args 的 server.py",
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


def _anchor_label(call: ast.Call) -> str:
    arg = call.args[0] if call.args else next((k.value for k in call.keywords), None)
    if isinstance(arg, ast.Attribute):
        return arg.attr
    return ast.unparse(arg) if arg is not None else ""


def _g1_names(nodes: Iterable[ast.AST], aliases: dict[str, str]) -> Counter[str]:
    nodes = list(nodes)
    anchor_calls = {
        id(node.func): f"{ANCHOR_ROOT}:{_anchor_label(node)}"
        for node in nodes
        if isinstance(node, ast.Call) and called_name(node.func, aliases) == ANCHOR_ROOT
    }
    found: Counter[str] = Counter()
    for node in nodes:
        if id(node) in anchor_calls:
            found[anchor_calls[id(node)]] += 1
        elif isinstance(node, ast.Attribute) and node.attr in _G1_ALL:
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


def _merged(*tables: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for table in tables:
        for key, names in table.items():
            merged.setdefault(key, {}).update(names)
    return merged


def test_handoffs_whole_anchors_and_anchor_names_are_used_only_where_listed():
    """含 TF 的產生函式與接收函式的呼叫點（``G1_TEXT_SITES``，照解析到的「模組:函式」計次）."""
    found = _merged(g1_sites(product_sources()), g1_text_sites(product_text_flow()))
    expected = _merged(
        {key: names for key, (names, _why) in G1_SITES.items()},
        {key: names for key, (names, _why) in G1_TEXT_SITES.items()},
    )
    assert found == expected


def test_every_g1_site_says_why():
    sites = [*G1_SITES.items(), *G1_TEXT_SITES.items()]
    assert [key for key, (_names, why) in sites if not why.strip()] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("def f(rm, zp):\n    return rm.zone_handoff(zp, k)\n", {"m:f": {"zone_handoff": 1}}),
        ("def f(rm):\n    g = rm.zone_handoff\n", {"m:f": {"zone_handoff": 1}}),
        (
            "from z import anchor_root as a\ndef f(x):\n    return a(x)\n",
            {"m:f": {"anchor_root:x": 1}},
        ),
        (
            "def f(rm):\n    return rm.anchor_root(Anchor.DOCGEN_OUTPUT)\n",
            {"m:f": {"anchor_root:DOCGEN_OUTPUT": 1}},
        ),
        ("def f(rm):\n    g = rm.anchor_root\n", {"m:f": {"anchor_root": 1}}),
        ("def f(rm, zp):\n    return rm.zone_text(zp)\n", {"m:f": {"zone_text": 1}}),
        (
            "def f(layout):\n    return proxy_state_owner(layout, k, o)\n",
            {"m:f": {"proxy_state_owner": 1}},
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
DOCGEN_NAME = anchor_dirname(Anchor.DOCGEN_OUTPUT).rsplit("/", 1)[-1]  # type: ignore[union-attr]
MANAGED_NAME = anchor_dirname(Anchor.MCP_SERVERS).rsplit("/", 1)[-1]  # type: ignore[union-attr]
# ST 不在 data 根底下、zone_paths 沒有它的資料夾名稱：沒設時的預設
# （RM 的 _state_setting_or_default）
STATE_NAME = "sandbox_state"
# 逐段比對的名稱（``mcp_servers``、``temp``、``proxy`` 太常見，不列；由 Z2、Z9 在執行期擋）
G2_NAMES = (PERSONAL_NAME, DRIVE_NAME, DOCGEN_NAME, STATE_NAME, MANAGED_NAME)
_OAUTH = "和錨點無關（OAuth 金鑰來源 managed／external 的設定值）"
_KEYRING = "src.protocols.mcp.oauth.keyring:OAuthKeyRingProvider"
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
    # PR 2（P8）加的三個名稱
    f"src.services.isolation.executor_session:<module>:{MANAGED_NAME}": (
        1,
        "執行器的讀寫掛載根名稱（標準函式庫，不能匯入 zone_paths）",
    ),
    f"src.services.isolation.profiles:build_evolution_profile:{MANAGED_NAME}": (
        1,
        "核對演化的技能資料夾正好是 managed/<skill>（只比對路徑、不做 I/O）",
    ),
    f"src.services.isolation.repair:reserved_roots:{MANAGED_NAME}": (
        1,
        "權限自癒的 W1 根（Z10 核對）",
    ),
    f"src.services.isolation.status:expected_executor_roots:{MANAGED_NAME}": (
        1,
        "執行器應該掛的根（Z10 核對）",
    ),
    f"src.services.resource_manager:_state_setting_or_default:{STATE_NAME}": (
        1,
        "ST 沒設時的預設位置（P7 的唯一來源）",
    ),
    f"src.utils.mcp_origin:managed_root:{MANAGED_NAME}": (
        1,
        "MCP 分類用的演化部署根（只組路徑做比對、不做 I/O）",
    ),
    f"src.utils.mcp_sentinels:resolve_env_sentinel:{DOCGEN_NAME}": (
        1,
        "@TOUKA_OUTPUT@ 換成交給 MCP 子行程的 env 字串（只組資料，Touka 不拿它做 I/O）",
    ),
    f"src.config:<module>:{MANAGED_NAME}": (2, _OAUTH),
    f"src.protocols.mcp.oauth.bootstrap:build_mcp_oauth_runtime_bundle:{MANAGED_NAME}": (
        1,
        _OAUTH,
    ),
    f"{_KEYRING}.__init__:{MANAGED_NAME}": (1, _OAUTH),
    f"{_KEYRING}.create_rotation_candidate:{MANAGED_NAME}": (1, _OAUTH),
    f"{_KEYRING}.prepare_rotation:{MANAGED_NAME}": (1, _OAUTH),
    f"{_KEYRING}.retire:{MANAGED_NAME}": (1, _OAUTH),
    f"{_KEYRING}.load:{MANAGED_NAME}": (2, _OAUTH),
    f"src.protocols.mcp.oauth.rotation:OAuthKeyRotationService.start:{MANAGED_NAME}": (1, _OAUTH),
    f"src.protocols.mcp.oauth.rotation:OAuthKeyRotationService.maintain:{MANAGED_NAME}": (
        1,
        _OAUTH,
    ),
    (
        "src.protocols.mcp.oauth.workload.rotation:"
        f"WorkloadKeyRotationService._start_unserialized:{MANAGED_NAME}"
    ): (1, _OAUTH),
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
            for name in G2_NAMES:
                if segments[name]:
                    found[f"{key}:{name}"] = segments[name]
            if segments[CUSTOM_NAME] and segments[DRIVE_NAME]:
                found[f"{key}:{CUSTOM_AND_DRIVE}"] = 1
    return found


def test_anchor_folder_names_appear_only_where_listed():
    assert g2_sites(product_sources()) == {key: n for key, (n, _why) in G2_SITES.items()}
    assert [key for key, (_n, why) in G2_SITES.items() if not why.strip()] == []


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
        ("def f(d):\n    return d / 'temp' / 'docgen_output'\n", {"m:f:docgen_output": 1}),
        ("def f(d):\n    return f'{d}/temp/sandbox_state'\n", {"m:f:sandbox_state": 1}),
        ("ROOT = 'mcp_servers/managed'\n", {"m:<module>:managed": 1}),
        # 太常見的段不列；整段不同的不算
        ("def f(d):\n    return d / 'mcp_servers' / 'temp' / 'proxy'\n", {}),
        ("def f():\n    return 'managed-key'\n", {}),
    ],
)
def test_g2_finds_anchor_names_in_every_kind_of_literal(text, expected):
    assert g2_sites([Source("m.py", "m", ast.parse(text))]) == expected


# --- TF 區字串的流向、G4 區字串的出口（ADR-0293 PR 2 P4） ----------------------

ZONE_TEXT = "zone_text"  # RM 交出區字串的方法（種子）
_PATH_TYPES = ("Path", "PurePath", "PurePosixPath", "PureWindowsPath")
_PURE_OS_PATH = (
    "join", "normcase", "normpath", "basename", "dirname", "split", "splitext", "splitdrive",
    "isabs", "commonpath",
)
# 往下追：參數裡有區字串時，結果也是區字串（純字串、PurePath 與函式內容器的操作，不做 I/O）
TF_CALLS = frozenset(
    {"str", "list", "tuple", "set", "frozenset", "dict.fromkeys", "os.fspath"}
    | {f"pathlib.{name}" for name in _PATH_TYPES}
    | {f"os.path.{name}" for name in _PURE_OS_PATH}
)
# 接在區字串後面的純字串方法與 PurePath 的純方法、屬性
TF_METHODS = frozenset(
    {"lower", "upper", "casefold", "strip", "lstrip", "rstrip", "replace", "split", "rsplit"}
    | {"partition", "rpartition", "startswith", "endswith", "removeprefix", "removesuffix"}
    | {"join", "format", "encode", "count", "find", "rfind"}
    | {"with_name", "with_suffix", "with_stem", "joinpath", "as_posix", "is_absolute"}
    | {"is_relative_to", "relative_to", "match"}
)
TF_ATTRS = frozenset(
    {"parent", "parents", "name", "stem", "suffix", "suffixes", "parts", "anchor", "drive", "root"}
)
TF_CONTAINER_READS = frozenset({"get", "setdefault"})  # 函式內容器的讀取（同下標）
# 參數是區字串也不算出口、結果不是區字串（P4 出口第 2 種）
TF_PLAIN = frozenset({"len", "hash", "isinstance", "bool"})
# 函式內的容器收下區字串：整個容器算區字串
TF_CONTAINER_METHODS = frozenset({"append", "extend", "update", "add", "insert"})
# 函式內容器的子容器（``data.setdefault(k, {})[x] = …``）也算函式內的
_SUBCONTAINER_METHODS = frozenset({"setdefault", "get"})
# 純字串常數（``os.pathsep.join(…)``）
_STR_CONSTANTS = frozenset({"os.sep", "os.pathsep", "os.linesep"})
# 當成呼叫某個參數位置上的函式（之後的參數依序交給它）
TF_INDIRECT = {"functools.partial": 0, "asyncio.to_thread": 0, "run_in_executor": 1}
ANY_POSITIONAL = "*"  # 去處的「哪個參數」：任何位置參數
Destinations = dict[str, tuple[frozenset[int | str], str]]

_EVO = "src.services.isolation.evolution_command"
_PROFILES = "src.services.isolation.profiles"
_MCP_PROFILES = "src.services.isolation.mcp_profiles"
_ORIGIN = "src.utils.mcp_origin"
_EVOLUTION = "src.tools.builtin.evolution"
_GET_TOOL_ROOT = "src.services.evolution_security:EvolutionPathGuard.get_tool_root"
# 回傳區字串的函式（TF 算出，「模組:函式」→ 用途）
ZONE_TEXT_PRODUCERS: dict[str, str] = {
    _GET_TOOL_ROOT: (
        "技能資料夾 managed/<skill>：演化 profile 的根與 cwd、技能鎖的鍵、部署凍結的重疊比對"
    ),
    f"{_MCP_PROFILES}:mcp_classification_bases": (
        "MCP 分類基準：proxy 的 cwd 加兩種 ST home（啟動與部署凍結共用）"
    ),
    f"{_MCP_PROFILES}:mcp_state_dirs": "MCP 伺服器自己的 ST home／tmp（profile 的規則、cwd、HOME）",
    f"{_PROFILES}:state_dirs": "ST 底下 proxy 位置的 home／tmp（給 profile 的規則與 HOME／TMPDIR）",
    f"{_PROFILES}:_state": "演化與代理人指令的 home／tmp（proxy 位置經 state_dirs）",
    f"{_EVOLUTION}:_venv_python": "技能 venv 的 python：子行程 argv 與部署條目的 command",
}
_CLASSIFY = "以 ST home 為基準的 MCP 分類（只讀中繼資料；出口見 G4_ALLOWED）"
# 收區字串的產品函式（TF 算出，「模組:函式」→（參數, 用途））
ZONE_TEXT_RECEIVERS: dict[str, tuple[frozenset[str], str]] = {
    "src.services.isolation.agent_command:unbound_or_refuse": (
        frozenset({"cwd"}),
        "沒綁定的 profile 的 cwd（交給 SandboxProfile）",
    ),
    f"{_EVO}:_name_key": (frozenset({"text"}), "重疊比對的字面鍵（純字串）"),
    f"{_EVO}:_nested": (frozenset({"first", "second"}), "比對鍵互為上下層（PurePath 的純方法）"),
    f"{_EVO}:_overlapping_roots": (
        frozenset({"first", "second"}),
        "兩個技能資料夾是否重疊（I/O 見 G4_ALLOWED）",
    ),
    f"{_EVO}:_profile_for": (frozenset({"cwd"}), "取得 runner 後重算 profile 的 cwd"),
    f"{_PROFILES}:_resolve": (frozenset({"path"}), "核對 managed/<skill> 前的解析（G4_ALLOWED）"),
    f"{_PROFILES}:build_evolution_profile": (frozenset({"tool_root"}), "演化 profile 的根"),
    f"{_PROFILES}:public_config_rules": (
        frozenset({"anchors"}),
        "演化根上層的公開設定檔（解析並核對過才往上找）",
    ),
    f"{_PROFILES}:sandbox_env": (
        frozenset({"home", "tmp", "ceilings"}),
        "HOME／TMPDIR／XDG 與 GIT_CEILING_DIRECTORIES（交給 env_set）",
    ),
    "src.services.mcp_isolation_store:bound_of": (
        frozenset({"entry"}),
        "部署條目的程式身分雜湊",
    ),
    f"{_EVOLUTION}:_build_jail_code": (
        frozenset({"target_dir"}),
        "驗證包裝裡的技能資料夾（JSON 編碼嵌進程式碼）",
    ),
    f"{_EVOLUTION}:_save_deployment": (frozenset({"entry"}), "部署條目寫進 server_config.json"),
    f"{_ORIGIN}:classify": (frozenset({"base_dir"}), _CLASSIFY),
    f"{_ORIGIN}:scaffold_dir_for": (frozenset({"base_dir"}), _CLASSIFY),
    f"{_ORIGIN}:referenced_paths": (frozenset({"base_dir"}), _CLASSIFY),
    f"{_ORIGIN}:_token_paths": (frozenset({"base_dir"}), _CLASSIFY),
    f"{_ORIGIN}:_item_paths": (frozenset({"base_dir"}), _CLASSIFY),
    f"{_ORIGIN}:_candidates": (frozenset({"path"}), _CLASSIFY),
    f"{_ORIGIN}:is_file_reference": (frozenset({"path"}), _CLASSIFY),
}
# 解析不到、名稱卻和產生函式或接收函式相同的呼叫（「模組:函式:名稱」→ 理由）
ZONE_TEXT_HOMONYMS: dict[str, str] = {
    "src.services.isolation.executor_session:check_start:state_dirs": (
        "執行器自己的 Layout.state_dirs：算執行器容器裡的 POSIX 路徑，不是 profiles.state_dirs"
    ),
}

# G4 列名的去處（P4 第 4 種）：名稱 →（准收區字串的參數, 理由）；「名稱.*」是那個物件的每個方法
G4_DESTINATIONS: Destinations = {
    f"{_PROFILES}.PathRule": (
        frozenset({0, "path"}),
        "沙盒規則的路徑：交給啟動器或執行器套用，Touka 不開",
    ),
    f"{_PROFILES}.SandboxProfile": (
        frozenset({1, "cwd", "project_root", "env_set"}),
        "profile 的 cwd、專案根與 HOME／TMPDIR（env_set）",
    ),
    "src.services.execution_broker.spawn_exec": (
        frozenset({ANY_POSITIONAL}),
        "子行程的 argv（位置參數全是 argv）；cwd= 不在內",
    ),
    "src.services.execution_broker.run_sync": (
        frozenset({0, "argv"}),
        "子行程的 argv；cwd= 不在內",
    ),
    "src.protocols.registry.ProtocolServerConfig": (
        frozenset({"command", "args", "**"}),
        "MCP 伺服器的啟動 argv（venv 的 python、server.py 或 test_wrapper.py 的位置）",
    ),
    f"{_EVO}.EvolutionStdioLaunch": (
        frozenset({"argv"}),
        "演化驗證的 stdio 啟動 argv，經 runner spawn",
    ),
    "launch.run": (
        frozenset({1, "argv"}),
        "EvolutionLaunch.run 的 argv（venv 的 python），經 run_sandbox_command 交給 runner",
    ),
    "src.utils.config_utils.save_config_json": (
        frozenset({1, "data"}),
        "寫進 server_config.json 的部署條目（command／args）",
    ),
    "logger.*": (frozenset({ANY_POSITIONAL}), "記錄"),
}
_CLASSIFY_STRICTER = (
    "MCP 分類以 ST home 為基準時只讀中繼資料；結果只採用變嚴的方向（kind 只會換成 self、"
    "部署凍結多算引用）；managed 技能取聯集，但條目本來就能直接引用同一個 managed/<skill>"
)
# G4 列名的例外（P4 第 5 種）：「模組:函式」→（{出口: 次數}, 理由）
G4_ALLOWED: dict[str, tuple[dict[str, int], str]] = {
    f"{_EVO}:_overlapping_roots": (
        {"os.path.exists": 2, "os.path.samefile": 1, "<text>.resolve": 2},
        "三種比對任一成立就算重疊，只會多判（多拒絕）；任何 OSError 一律當重疊（Q3）",
    ),
    f"{_EVO}:skill_lock": (
        {"key:_skill_locks[]": 2, "_skill_locks.get": 1, "next": 1},
        "normcase(區字串) 只當技能鎖的身分（存、取、刪，與找重疊的已持有鍵）",
    ),
    f"{_EVO}:refuse_deployed": (
        {f"ref:{_MCP_PROFILES}:mcp_classification_bases": 1},
        "partial 交給 deployed_skills 的 bases_for：多一份基準只會多算引用（多凍結、多拒絕）",
    ),
    f"{_PROFILES}:_resolve": (
        {"<text>.resolve": 1},
        "build_evolution_profile 先解析再核對 root == managed/<skill>（不符丟 ProfileError）；"
        "規則、cwd 與公開設定檔都用核對過的值，連結只會讓核對失敗",
    ),
    "src.services.isolation.status:_repair_state_dir": (
        {"repair.repair": 1},
        "權限自癒逐段不跟隨地開（kernel，禁令 66 ⑨），只補自己擁有的 inode 的擁有者位元",
    ),
    "src.services.mcp_isolation_store:bound_of": (
        {"json.dumps": 1},
        "只算條目程式身分的雜湊（字串），不做 I/O",
    ),
    f"{_EVOLUTION}:_build_jail_code": (
        {"json.dumps": 1},
        "技能資料夾以 JSON 編碼嵌進驗證包裝的程式碼，給沙盒裡的子行程；Touka 不拿它做 I/O",
    ),
    f"{_ORIGIN}:_item_paths": ({"<text>.exists": 1}, _CLASSIFY_STRICTER),
    f"{_ORIGIN}:referenced_paths": (
        {f"{_ORIGIN}.PathReference": 3, "<text>.exists": 1},
        _CLASSIFY_STRICTER,
    ),
    f"{_ORIGIN}:_candidates": ({"<text>.resolve": 1}, _CLASSIFY_STRICTER),
    f"{_ORIGIN}:is_file_reference": (
        {"os.lstat": 1, "<text>.is_junction": 1},
        _CLASSIFY_STRICTER,
    ),
}


@dataclass(frozen=True)
class TextScope:
    key: str  # 「模組:函式」，模組層是 ``<module>``
    module: str
    fn: Function | None
    nodes: tuple[ast.AST, ...]
    imports: dict[str, str]  # 名稱 → 點分全名（``import x as y``、``from x import y as z``）
    top: frozenset[str]  # 模組層的 def 與 class


def import_names(module: str, tree: ast.Module) -> dict[str, str]:
    """模組裡匯入的名稱 → 點分全名（相對匯入照模組位置換成絕對的）."""
    package = module.split(".")
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    found[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    found[head] = head
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join([*package[: len(package) - node.level], *([base] if base else [])])
            for alias in node.names:
                found[alias.asname or alias.name] = f"{base}.{alias.name}"
    return found


def qualify(node: ast.expr, scope: TextScope) -> str | None:
    """名稱或屬性鏈的點分全名（照匯入與模組層定義）；其他寫法回 None."""
    if isinstance(node, ast.Name):
        if node.id in scope.imports:
            return scope.imports[node.id]
        return f"{scope.module}.{node.id}" if node.id in scope.top else node.id
    if isinstance(node, ast.Attribute):
        base = qualify(node.value, scope)
        return f"{base}.{node.attr}" if base else None
    return None


def _short_name(func: ast.expr, scope: TextScope) -> str | None:
    if isinstance(func, ast.Name):
        return scope.imports.get(func.id, func.id).rsplit(".", 1)[-1]
    return func.attr if isinstance(func, ast.Attribute) else None


def _str_constant(node: ast.expr, scope: TextScope) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    return qualify(node, scope) in _STR_CONSTANTS


def _decorators(fn: Function) -> set[str]:
    return {ast.unparse(d).rsplit(".", 1)[-1] for d in fn.node.decorator_list}


def _bind(
    fn: Function, skip: bool, args: list[ast.expr], keywords: list[ast.keyword]
) -> dict[str, list[ast.expr]]:
    """呼叫的參數 → 被呼叫函式的參數名稱（``*x``／``**x`` 保守地給後面全部）."""
    spec = fn.node.args
    positional = [p.arg for p in (*spec.posonlyargs, *spec.args)][1 if skip else 0 :]
    rest = [spec.vararg.arg] if spec.vararg else []
    named = [p.arg for p in (*spec.posonlyargs, *spec.args, *spec.kwonlyargs)]
    bound: dict[str, list[ast.expr]] = {}
    for index, value in enumerate(args):
        if isinstance(value, ast.Starred):
            names = [*positional[index:], *rest]
        else:
            names = [positional[index]] if index < len(positional) else rest
        for name in names:
            bound.setdefault(name, []).append(value)
    for keyword in keywords:
        if keyword.arg is None:
            names = [*named, *([spec.kwarg.arg] if spec.kwarg else [])]
        elif keyword.arg in named:
            names = [keyword.arg]
        else:
            names = [spec.kwarg.arg] if spec.kwarg else []
        for name in names:
            bound.setdefault(name, []).append(keyword.value)
    return bound


def _bindings(node: ast.AST) -> Iterator[tuple[ast.AST, ast.expr]]:
    """（指定目標, 值）：指定、標註指定、``+=``、walrus、``for`` 與生成式的迴圈變數."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            yield target, node.value
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value:
        yield node.target, node.value
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        yield node.target, node.iter


def _root_name(node: ast.expr) -> str | None:
    """下標與子容器鏈最底下的名稱（``data.setdefault(k, {})[x]`` → ``data``）."""
    while True:
        if isinstance(node, ast.Subscript):
            node = node.value
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SUBCONTAINER_METHODS
        ):
            node = node.func.value
        else:
            return node.id if isinstance(node, ast.Name) else None


def _call_values(call: ast.Call) -> list[ast.expr]:
    return [*call.args, *(k.value for k in call.keywords)]


def _slot_allowed(slot: int | str, allowed: frozenset[int | str]) -> bool:
    return slot in allowed or (isinstance(slot, int) and ANY_POSITIONAL in allowed)


@dataclass(frozen=True)
class CallTarget:
    fn: Function
    skip: bool  # 第一個參數是 self／cls（不算在呼叫的參數裡）
    args: list[ast.expr]
    keywords: list[ast.keyword]
    partial: bool  # ``functools.partial``：結果是函式，不是區字串


class TextFlow:
    """區字串的流向（P4 的 TF）：產生函式、接收函式（與哪個參數）、往下追的區域變數."""

    def __init__(
        self, sources: Iterable[tuple[str, ast.Module]], destinations: Destinations | None = None
    ) -> None:
        self.destinations = destinations or {}
        self.scopes: list[TextScope] = []
        self.index: dict[str, Function] = {}
        for module, tree in sources:
            imports = import_names(module, tree)
            kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            top = frozenset(node.name for node in tree.body if isinstance(node, kinds))
            _key, module_nodes = next(scopes(Source(f"{module}.py", module, tree)))
            self.scopes.append(
                TextScope(f"{module}:<module>", module, None, tuple(module_nodes), imports, top)
            )
            for fn in functions(module, tree):
                self.index[f"{module}.{fn.qualname}"] = fn
                nodes = tuple(own_nodes(fn.node))
                self.scopes.append(TextScope(fn.key, module, fn, nodes, imports, top))
        self.by_key = {scope.key: scope for scope in self.scopes}
        self.producers: set[str] = set()
        self.receivers: dict[str, set[str]] = {}
        self.passthrough: dict[str, set[str]] = {}
        self.self_attrs: dict[tuple[str, str], set[str]] = {}
        self.local: dict[str, set[str]] = {}
        self._solve()

    # --- 呼叫對到哪個函式 ---

    def resolve(self, func: ast.expr, scope: TextScope) -> tuple[Function, bool] | None:
        """只認解析得到的寫法：同模組、匯入（含別名）、``模組.名稱``、``類別.方法``、``self.方法``."""
        if isinstance(func, ast.Name) and scope.fn is not None:
            qualname: str | None = scope.fn.qualname
            while qualname:
                nested = self.index.get(f"{scope.module}.{qualname}.<locals>.{func.id}")
                if nested is not None:
                    return nested, False
                qualname = qualname.rsplit(".<locals>.", 1)[0] if ".<locals>." in qualname else None
        owner = scope.fn.owner if scope.fn is not None else None
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in ("self", "cls")
            and owner is not None
        ):
            method = self.index.get(f"{scope.module}.{owner}.{func.attr}")
            if method is None:
                return None
            return method, "staticmethod" not in _decorators(method)
        dotted = qualify(func, scope)
        target = self.index.get(dotted) if dotted else None
        if target is None:
            return None
        return target, target.owner is not None and "classmethod" in _decorators(target)

    def indirect(self, call: ast.Call, scope: TextScope) -> int | None:
        """``partial``／``to_thread``／``run_in_executor``：被呼叫的函式在第幾個參數."""
        position = TF_INDIRECT.get(qualify(call.func, scope) or "")
        if position is None and _short_name(call.func, scope) == "run_in_executor":
            position = TF_INDIRECT["run_in_executor"]
        return position if position is not None and len(call.args) > position else None

    def call_target(self, call: ast.Call, scope: TextScope) -> CallTarget | None:
        position = self.indirect(call, scope)
        if position is not None:
            found = self.resolve(call.args[position], scope)
            if found is None:
                return None
            partial = qualify(call.func, scope) == "functools.partial"
            return CallTarget(*found, call.args[position + 1 :], call.keywords, partial)
        found = self.resolve(call.func, scope)
        return None if found is None else CallTarget(*found, call.args, call.keywords, False)

    def destination(self, call: ast.Call, scope: TextScope) -> tuple[str, frozenset | None]:
        """（呼叫的名稱, 列名去處准收的參數；沒列名是 None）."""
        label = qualify(call.func, scope) or ast.unparse(call.func)
        if label in self.destinations:
            return label, self.destinations[label][0]
        prefix = label.rsplit(".", 1)[0] + ".*"
        return label, self.destinations[prefix][0] if prefix in self.destinations else None

    # --- 區字串 ---

    def is_text(self, node: ast.AST, scope: TextScope, local: set[str]) -> bool:
        """``node`` 的值可能是區字串（或裝著區字串的值）."""

        def text(child: ast.AST) -> bool:
            return self.is_text(child, scope, local)

        if isinstance(node, ast.Name):
            return node.id in local
        if isinstance(node, ast.Call):
            return self._call_is_text(node, scope, local)
        if isinstance(node, ast.Attribute):
            if node.attr in TF_ATTRS and text(node.value):
                return True
            owner = scope.fn.owner if scope.fn is not None else None
            is_self = isinstance(node.value, ast.Name) and node.value.id == "self"
            return is_self and owner is not None and node.attr in self.self_attrs.get(
                (scope.module, owner), set()
            )
        if isinstance(node, ast.BinOp):
            arithmetic = isinstance(node.op, (ast.Div, ast.Add, ast.Mod))
            return arithmetic and (text(node.left) or text(node.right))
        if isinstance(node, ast.JoinedStr):
            return any(text(v) for v in node.values)
        if isinstance(node, (ast.FormattedValue, ast.Subscript, ast.Starred, ast.Await)):
            return text(node.value)
        if isinstance(node, ast.NamedExpr):
            return text(node.value)
        if isinstance(node, ast.IfExp):
            return text(node.body) or text(node.orelse)
        if isinstance(node, ast.BoolOp):
            return any(text(v) for v in node.values)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return any(text(e) for e in node.elts)
        if isinstance(node, ast.Dict):
            return any(text(v) for v in (*node.keys, *node.values) if v is not None)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return text(node.elt)
        if isinstance(node, ast.DictComp):
            return text(node.key) or text(node.value)
        return False

    def _call_is_text(self, call: ast.Call, scope: TextScope, local: set[str]) -> bool:
        def text(child: ast.AST) -> bool:
            return self.is_text(child, scope, local)

        func = call.func
        values = _call_values(call)
        if _short_name(func, scope) == ZONE_TEXT:
            return True
        if isinstance(func, ast.Attribute):
            if text(func.value):
                return func.attr in TF_METHODS or func.attr in TF_CONTAINER_READS
            if func.attr in TF_METHODS and _str_constant(func.value, scope):
                return any(text(v) for v in values)
        if qualify(func, scope) in TF_CALLS:
            return any(text(v) for v in values)
        if self.destination(call, scope)[1] is not None:
            return False
        target = self.call_target(call, scope)
        if target is None or target.partial:
            return False
        if target.fn.key in self.producers:
            return True
        through = self.passthrough.get(target.fn.key, set())
        bound = _bind(target.fn, target.skip, target.args, target.keywords)
        return any(name in through and any(text(v) for v in vs) for name, vs in bound.items())

    def _locals(self, scope: TextScope, seeds: set[str]) -> set[str]:
        local = set(seeds)
        while True:
            before = len(local)
            for node in scope.nodes:
                for target, value in _bindings(node):
                    if self.is_text(value, scope, local):
                        local |= _target_names(target)
                        if isinstance(target, ast.Subscript) and (root := _root_name(target)):
                            local.add(root)
                func = node.func if isinstance(node, ast.Call) else None
                if (
                    isinstance(node, ast.Call)
                    and isinstance(func, ast.Attribute)
                    and func.attr in TF_CONTAINER_METHODS
                    and isinstance(func.value, ast.Name)
                    and any(self.is_text(v, scope, local) for v in _call_values(node))
                ):
                    local.add(func.value.id)
            if len(local) == before:
                return local

    def _returns_text(self, scope: TextScope, local: set[str]) -> bool:
        return any(
            isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom))
            and node.value is not None
            and self.is_text(node.value, scope, local)
            for node in scope.nodes
        )

    def _outer(self, scope: TextScope) -> TextScope | None:
        if scope.fn is None or ".<locals>." not in scope.fn.qualname:
            return None
        return self.by_key.get(f"{scope.module}:{scope.fn.qualname.rsplit('.<locals>.', 1)[0]}")

    def _closure(self, scope: TextScope) -> set[str]:
        """巢狀函式用到外層的區字串：算同一份（自己的參數除外）."""
        outer = self._outer(scope)
        if outer is None or scope.fn is None:
            return set()
        spec = scope.fn.node.args
        own = {p.arg for p in (*spec.posonlyargs, *spec.args, *spec.kwonlyargs)}
        return self.local.get(outer.key, set()) - own

    def assigned(self, scope: TextScope) -> set[str]:
        """函式自己（含外層函式，閉包算同一份）指定出來的名稱；參數不算（那是呼叫端的容器）."""
        names: set[str] = set()
        for node in scope.nodes:
            for target, _value in _bindings(node):
                names |= _target_names(target)
            if isinstance(node, ast.withitem) and node.optional_vars is not None:
                names |= _target_names(node.optional_vars)
        outer = self._outer(scope)
        return names | self.assigned(outer) if outer is not None else names

    def _solve(self) -> None:
        changed = True
        while changed:
            changed = False
            for scope in self.scopes:
                closure = self._closure(scope)
                params = self.receivers.get(scope.key, set())
                if (
                    scope.fn is not None
                    and scope.key not in self.producers
                    and self._returns_text(scope, self._locals(scope, closure))
                ):
                    self.producers.add(scope.key)
                    changed = True
                through = self.passthrough.setdefault(scope.key, set())
                for name in params - through:
                    if self._returns_text(scope, self._locals(scope, {name})):
                        through.add(name)
                        changed = True
                local = self._locals(scope, closure | params)
                if local != self.local.get(scope.key):
                    self.local[scope.key] = local
                    changed = True
                changed |= self._find_receivers(scope, local)
                changed |= self._find_self_attrs(scope, local)

    def _find_receivers(self, scope: TextScope, local: set[str]) -> bool:
        changed = False
        for node in scope.nodes:
            if not isinstance(node, ast.Call) or self.destination(node, scope)[1] is not None:
                continue
            target = self.call_target(node, scope)
            if target is None:
                continue
            bound = _bind(target.fn, target.skip, target.args, target.keywords)
            for name, values in bound.items():
                if any(self.is_text(v, scope, local) for v in values):
                    params = self.receivers.setdefault(target.fn.key, set())
                    if name not in params:
                        params.add(name)
                        changed = True
        return changed

    def _find_self_attrs(self, scope: TextScope, local: set[str]) -> bool:
        if scope.fn is None or scope.fn.owner is None:
            return False
        found = {
            attr
            for node in scope.nodes
            for target, value in _bindings(node)
            if self.is_text(value, scope, local)
            for attr in _self_attrs(target)
        }
        attrs = self.self_attrs.setdefault((scope.module, scope.fn.owner), set())
        if found <= attrs:
            return False
        attrs |= found
        return True

    # --- 結果 ---

    def receiver_params(self) -> dict[str, frozenset[str]]:
        return {key: frozenset(params) for key, params in self.receivers.items() if params}

    def homonyms(self) -> set[str]:
        """解析不到、名稱卻和產生函式或接收函式相同的呼叫（「模組:函式:名稱」）."""
        names = {key.rsplit(":", 1)[-1].rsplit(".", 1)[-1] for key in self.producers}
        names |= {key.rsplit(":", 1)[-1].rsplit(".", 1)[-1] for key in self.receiver_params()}
        names.discard(ZONE_TEXT)
        found = set()
        for scope in self.scopes:
            for node in scope.nodes:
                if isinstance(node, ast.Call) and self.call_target(node, scope) is None:
                    short = _short_name(node.func, scope)
                    if short in names and self.indirect(node, scope) is None:
                        found.add(f"{scope.key}:{short}")
        return found

    def exits(self) -> dict[str, dict[str, int]]:
        """每個範圍裡區字串去了沒列名的地方（G4）：「模組:函式」→ {出口: 次數}."""
        found = {}
        for scope in self.scopes:
            exits = self._scope_exits(scope, self.local.get(scope.key, set()))
            if exits:
                found[scope.key] = dict(exits)
        return found

    def _scope_exits(self, scope: TextScope, local: set[str]) -> Counter[str]:
        def text(child: ast.AST) -> bool:
            return self.is_text(child, scope, local)

        exits: Counter[str] = Counter()
        assigned = self.assigned(scope)
        declared = {n for node in scope.nodes if isinstance(node, ast.Global) for n in node.names}
        called = set()
        for node in scope.nodes:
            if isinstance(node, ast.Call):
                called.add(id(node.func))
                position = self.indirect(node, scope)
                if position is not None and qualify(node.func, scope) != "functools.partial":
                    called.add(id(node.args[position]))
        for node in scope.nodes:
            if isinstance(node, ast.Call):
                exits.update(self._call_exits(node, scope, local, assigned))
            elif (
                isinstance(node, ast.Attribute)
                and id(node) not in called
                and isinstance(node.ctx, ast.Load)
                and node.attr not in TF_ATTRS
                and text(node.value)
            ):
                exits[f"<text>.{node.attr}"] += 1
            if (
                isinstance(node, (ast.Name, ast.Attribute))
                and isinstance(node.ctx, ast.Load)
                and id(node) not in called
                and (found := self.resolve(node, scope)) is not None
                and found[0].key in self.producers
            ):
                exits[f"ref:{found[0].key}"] += 1  # 產生函式被當成值交出去（含 ``partial``）
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if text(node.value):
                        exits.update(_store_exits(target, scope, assigned, declared))
                    exits.update(self._key_exits(target, scope, local, assigned))
            if isinstance(node, ast.Delete):
                for target in node.targets:
                    exits.update(self._key_exits(target, scope, local, assigned))
        return exits

    def _call_exits(
        self, call: ast.Call, scope: TextScope, local: set[str], assigned: set[str]
    ) -> list[str]:
        def text(child: ast.AST) -> bool:
            return self.is_text(child, scope, local)

        func = call.func
        method = func.attr if isinstance(func, ast.Attribute) else None
        if (
            method in TF_CONTAINER_METHODS
            and isinstance(func.value, ast.Name)  # type: ignore[union-attr]
            and func.value.id in assigned  # type: ignore[union-attr]
        ):
            return []  # 放進函式內的容器：之後怎麼用照同一套規則檢查
        receiver_text = isinstance(func, ast.Attribute) and text(func.value)
        exits = []
        if receiver_text and method not in TF_METHODS | TF_CONTAINER_READS:
            exits.append(f"<text>.{method}")
        slots: list[tuple[int | str, ast.expr]] = [*enumerate(call.args)]
        slots += [(k.arg or "**", k.value) for k in call.keywords]
        tainted = [slot for slot, value in slots if text(value)]
        if not tainted or receiver_text:
            return exits
        if method in TF_METHODS and _str_constant(func.value, scope):  # type: ignore[union-attr]
            return exits
        if qualify(func, scope) in TF_CALLS | TF_PLAIN:
            return exits
        label, allowed = self.destination(call, scope)
        if allowed is not None:
            return [*exits, *(f"{label}({s})" for s in tainted if not _slot_allowed(s, allowed))]
        if self.call_target(call, scope) is not None:
            return exits  # 接收函式：本體照同一套規則檢查
        return [*exits, label]

    def _key_exits(
        self, target: ast.AST, scope: TextScope, local: set[str], assigned: set[str]
    ) -> list[str]:
        """區字串當成函式外容器的鍵（存或刪）."""
        if not isinstance(target, ast.Subscript) or not self.is_text(target.slice, scope, local):
            return []
        if scope.fn is not None and _root_name(target.value) in assigned:
            return []
        return [f"key:{ast.unparse(target.value)}[]"]


def _store_exits(
    target: ast.AST, scope: TextScope, assigned: set[str], declared: set[str]
) -> list[str]:
    """區字串存到函式外：模組層、``global``、非 ``self`` 物件的屬性、不是函式內的容器."""
    if isinstance(target, (ast.Tuple, ast.List)):
        return [e for elt in target.elts for e in _store_exits(elt, scope, assigned, declared)]
    if isinstance(target, ast.Starred):
        return _store_exits(target.value, scope, assigned, declared)
    if isinstance(target, ast.Name):
        return [f"store:{target.id}"] if scope.fn is None or target.id in declared else []
    if isinstance(target, ast.Attribute):
        is_self = isinstance(target.value, ast.Name) and target.value.id == "self"
        return [] if is_self else [f"store:{ast.unparse(target)}"]
    if isinstance(target, ast.Subscript):
        root = _root_name(target.value)
        if scope.fn is not None and root in assigned and root not in declared:
            return []
        return [f"store:{ast.unparse(target.value)}[]"]
    return []


@cache
def product_text_flow() -> TextFlow:
    return TextFlow(((s.module, s.tree) for s in product_sources()), G4_DESTINATIONS)


# G1 也釘住產生函式與接收函式的呼叫點（照解析到的函式計次；同名不同物不算）
G1_TF_FUNCTIONS = frozenset(ZONE_TEXT_PRODUCERS) | frozenset(ZONE_TEXT_RECEIVERS)
_TF_BOUND = "條目程式身分的雜湊（部署、手動啟動的同意、隔離紀錄核對）"
_TF_CLASSIFY = "MCP 來源分類與引用路徑（只讀中繼資料）"
_TF_UNBOUND = "沒綁定的 profile：cwd 交給 SandboxProfile"
_TF_OVERLAP = "技能鎖與部署凍結：技能資料夾的重疊比對"
_TF_EVOLUTION = "演化：技能資料夾、venv 的 python 與部署條目（只交給 profile、argv、設定）"
_TF_PROFILE = "組 profile：區字串只交給規則、cwd 與 env_set"
_TF_STATE = "ST 的 home／tmp（經 P7 的存取函式）"
_TF_RESOLVE = "版面、規則與專案根的一般解析（參數不是區字串；區字串那一處見 G4_ALLOWED）"
# 「模組:函式」→（{被呼叫的產生或接收函式: 次數}, 理由）
G1_TEXT_SITES: dict[str, tuple[dict[str, int], str]] = {
    "src.protocols.manager:ProtocolManager._manual_start_view": (
        {"src.services.mcp_isolation_store:bound_of": 1},
        _TF_BOUND,
    ),
    "src.routers.mcp:_classify_sync": (
        {f"{_ORIGIN}:classify": 1},
        _TF_CLASSIFY,
    ),
    "src.routers.mcp:_manual_start": (
        {"src.services.mcp_isolation_store:bound_of": 1},
        _TF_BOUND,
    ),
    "src.services.isolation.agent_command:build_command_profile": (
        {"src.services.isolation.agent_command:unbound_or_refuse": 1},
        _TF_UNBOUND,
    ),
    f"{_EVO}:_overlapping_roots": (
        {f"{_EVO}:_name_key": 4, f"{_EVO}:_nested": 2},
        _TF_OVERLAP,
    ),
    f"{_EVO}:_profile_for": (
        {"src.services.isolation.agent_command:unbound_or_refuse": 1},
        _TF_UNBOUND,
    ),
    f"{_EVO}:acquire_evolution_launch": (
        {_GET_TOOL_ROOT: 1, f"{_EVO}:_profile_for": 1},
        _TF_EVOLUTION,
    ),
    f"{_EVO}:evolution_profile": (
        {_GET_TOOL_ROOT: 1, f"{_PROFILES}:build_evolution_profile": 1},
        _TF_EVOLUTION,
    ),
    f"{_EVO}:refuse_deployed": (
        {
            _GET_TOOL_ROOT: 2,
            f"{_EVO}:_name_key": 2,
            f"{_EVO}:_overlapping_roots": 1,
            f"{_MCP_PROFILES}:mcp_classification_bases": 1,
        },
        _TF_OVERLAP,
    ),
    f"{_EVO}:skill_lock": (
        {_GET_TOOL_ROOT: 1, f"{_EVO}:_overlapping_roots": 1},
        _TF_OVERLAP,
    ),
    "src.services.isolation.mcp_launch:McpLauncher._classify_with": (
        {f"{_MCP_PROFILES}:mcp_classification_bases": 1, f"{_ORIGIN}:classify": 2},
        _TF_CLASSIFY,
    ),
    "src.services.isolation.mcp_launch:McpManualStart.consent_refusal": (
        {"src.services.mcp_isolation_store:bound_of": 1},
        _TF_BOUND,
    ),
    "src.services.isolation.mcp_launch:_SelfProfile.__call__": (
        {"src.services.isolation.agent_command:unbound_or_refuse": 1},
        _TF_UNBOUND,
    ),
    "src.services.isolation.mcp_launch:_ThirdPartyProfile.__call__": (
        {"src.services.isolation.agent_command:unbound_or_refuse": 1},
        _TF_UNBOUND,
    ),
    f"{_MCP_PROFILES}:_reference_rule": (
        {f"{_ORIGIN}:is_file_reference": 1},
        _TF_CLASSIFY,
    ),
    f"{_MCP_PROFILES}:build_mcp_self_profile": (
        {f"{_MCP_PROFILES}:mcp_state_dirs": 1, f"{_PROFILES}:sandbox_env": 1},
        _TF_PROFILE,
    ),
    f"{_MCP_PROFILES}:build_mcp_third_party_profile": (
        {
            f"{_MCP_PROFILES}:mcp_state_dirs": 1,
            f"{_PROFILES}:sandbox_env": 1,
            f"{_ORIGIN}:referenced_paths": 1,
            f"{_ORIGIN}:scaffold_dir_for": 1,
        },
        _TF_PROFILE,
    ),
    f"{_MCP_PROFILES}:mcp_classification_bases": (
        {f"{_MCP_PROFILES}:mcp_state_dirs": 1},
        _TF_STATE,
    ),
    f"{_MCP_PROFILES}:mcp_state_dirs": (
        {f"{_PROFILES}:state_dirs": 1},
        _TF_STATE,
    ),
    f"{_PROFILES}:_denied_by_fixed_rules": (
        {f"{_PROFILES}:_resolve": 2},
        _TF_RESOLVE,
    ),
    f"{_PROFILES}:_protected_env_dir": (
        {f"{_PROFILES}:_resolve": 2},
        _TF_RESOLVE,
    ),
    f"{_PROFILES}:_same_state": (
        {f"{_PROFILES}:_resolve": 2},
        _TF_RESOLVE,
    ),
    f"{_PROFILES}:_state": (
        {f"{_PROFILES}:state_dirs": 1},
        _TF_STATE,
    ),
    f"{_PROFILES}:authorize_derived_path": (
        {f"{_PROFILES}:_resolve": 1},
        _TF_RESOLVE,
    ),
    f"{_PROFILES}:build_agent_command_profile": (
        {
            f"{_PROFILES}:_resolve": 2,
            f"{_PROFILES}:_state": 1,
            f"{_PROFILES}:public_config_rules": 1,
            f"{_PROFILES}:sandbox_env": 1,
        },
        _TF_PROFILE,
    ),
    f"{_PROFILES}:build_evolution_profile": (
        {
            f"{_PROFILES}:_resolve": 3,
            f"{_PROFILES}:_state": 1,
            f"{_PROFILES}:public_config_rules": 1,
            f"{_PROFILES}:sandbox_env": 1,
        },
        _TF_PROFILE,
    ),
    f"{_PROFILES}:current_layout": (
        {f"{_PROFILES}:_resolve": 7},
        _TF_RESOLVE,
    ),
    f"{_PROFILES}:fixed_protected": (
        {f"{_PROFILES}:_resolve": 1},
        _TF_RESOLVE,
    ),
    f"{_PROFILES}:has_link": (
        {f"{_PROFILES}:_resolve": 1},
        _TF_RESOLVE,
    ),
    f"{_PROFILES}:project_root_reason": (
        {f"{_PROFILES}:_resolve": 2},
        _TF_RESOLVE,
    ),
    f"{_PROFILES}:public_config_rules": (
        {f"{_PROFILES}:_resolve": 3},
        _TF_PROFILE,
    ),
    f"{_PROFILES}:system_rules": (
        {f"{_PROFILES}:_resolve": 4},
        _TF_RESOLVE,
    ),
    "src.services.mcp_hub_service:MCPHubService.install": (
        {"src.services.mcp_isolation_store:bound_of": 1},
        _TF_BOUND,
    ),
    "src.services.mcp_hub_service:MCPHubService.register_server": (
        {"src.services.mcp_isolation_store:bound_of": 1},
        _TF_BOUND,
    ),
    "src.services.mcp_isolation_store:IsolationSnapshot.view": (
        {"src.services.mcp_isolation_store:bound_of": 1},
        _TF_BOUND,
    ),
    "src.services.mcp_isolation_store:changed_programs": (
        {"src.services.mcp_isolation_store:bound_of": 2},
        _TF_BOUND,
    ),
    "src.services.mcp_isolation_store:reconcile_saved_entries.<locals>.apply": (
        {"src.services.mcp_isolation_store:bound_of": 1},
        _TF_BOUND,
    ),
    f"{_EVOLUTION}:_save_deployment": (
        {"src.services.mcp_isolation_store:bound_of": 1},
        _TF_BOUND,
    ),
    f"{_EVOLUTION}:_verify_mcp": (
        {
            _GET_TOOL_ROOT: 1,
            f"{_EVOLUTION}:_build_jail_code": 1,
            f"{_EVOLUTION}:_venv_python": 1,
        },
        _TF_EVOLUTION,
    ),
    f"{_EVOLUTION}:request_mcp_creation": (
        {f"{_EVOLUTION}:_save_deployment": 1, f"{_EVOLUTION}:_venv_python": 1},
        _TF_EVOLUTION,
    ),
    f"{_EVOLUTION}:write_mcp_implementation": (
        {f"{_EVOLUTION}:_venv_python": 1},
        _TF_EVOLUTION,
    ),
    "src.tools.builtin.system:request_skill_install": (
        {"src.services.mcp_isolation_store:bound_of": 1},
        _TF_BOUND,
    ),
    f"{_ORIGIN}:_mirror_hit": (
        {f"{_ORIGIN}:is_file_reference": 1},
        _TF_CLASSIFY,
    ),
    f"{_ORIGIN}:_token_paths": (
        {f"{_ORIGIN}:_item_paths": 2},
        _TF_CLASSIFY,
    ),
    f"{_ORIGIN}:agent_influenced": (
        {f"{_ORIGIN}:_candidates": 1},
        _TF_CLASSIFY,
    ),
    f"{_ORIGIN}:classify": (
        {f"{_ORIGIN}:referenced_paths": 1, f"{_ORIGIN}:scaffold_dir_for": 1},
        _TF_CLASSIFY,
    ),
    f"{_ORIGIN}:deployed_skills": (
        {f"{_ORIGIN}:_candidates": 1, f"{_ORIGIN}:referenced_paths": 1},
        _TF_CLASSIFY,
    ),
    f"{_ORIGIN}:referenced_managed_skills": (
        {f"{_ORIGIN}:_candidates": 1},
        _TF_CLASSIFY,
    ),
    f"{_ORIGIN}:referenced_paths": (
        {
            f"{_ORIGIN}:_candidates": 1,
            f"{_ORIGIN}:_item_paths": 1,
            f"{_ORIGIN}:_token_paths": 2,
        },
        _TF_CLASSIFY,
    ),
    f"{_ORIGIN}:scaffold_dir_for": (
        {
            f"{_ORIGIN}:_candidates": 1,
            f"{_ORIGIN}:_token_paths": 1,
            f"{_ORIGIN}:is_file_reference": 1,
        },
        _TF_CLASSIFY,
    ),
}


def g1_text_sites(flow: TextFlow) -> dict[str, dict[str, int]]:
    """每個範圍呼叫（含 ``partial``、``to_thread``、當成值交出）產生函式與接收函式的次數."""
    found = {}
    for scope in flow.scopes:
        counts: Counter[str] = Counter()
        called = {id(n.func) for n in scope.nodes if isinstance(n, ast.Call)}
        for node in scope.nodes:
            if isinstance(node, ast.Call) and (target := flow.call_target(node, scope)):
                if flow.indirect(node, scope) is not None:
                    called.add(id(node.args[flow.indirect(node, scope)]))  # type: ignore[index]
                if target.fn.key in G1_TF_FUNCTIONS:
                    counts[target.fn.key] += 1
        for node in scope.nodes:
            if (
                isinstance(node, (ast.Name, ast.Attribute))
                and isinstance(node.ctx, ast.Load)
                and id(node) not in called
                and (found_fn := flow.resolve(node, scope)) is not None
                and found_fn[0].key in G1_TF_FUNCTIONS
            ):
                counts[found_fn[0].key] += 1
        if counts:
            found[scope.key] = dict(counts)
    return found


def test_zone_text_producers_are_pinned():
    assert product_text_flow().producers == set(ZONE_TEXT_PRODUCERS)


def test_zone_text_receivers_are_pinned():
    expected = {key: params for key, (params, _why) in ZONE_TEXT_RECEIVERS.items()}
    assert product_text_flow().receiver_params() == expected


def test_unresolved_calls_with_a_zone_text_name_are_listed():
    assert product_text_flow().homonyms() == set(ZONE_TEXT_HOMONYMS)


def test_zone_text_goes_only_to_listed_places():
    """G4：沒列名的呼叫、方法、屬性與存到函式外一律紅；例外照「模組:函式」與次數釘住."""
    expected = {key: exits for key, (exits, _why) in G4_ALLOWED.items()}
    assert product_text_flow().exits() == expected


def test_every_tf_and_g4_item_says_why():
    reasons = [
        *ZONE_TEXT_PRODUCERS.values(),
        *(why for _params, why in ZONE_TEXT_RECEIVERS.values()),
        *ZONE_TEXT_HOMONYMS.values(),
        *(why for _slots, why in G4_DESTINATIONS.values()),
        *(why for _exits, why in G4_ALLOWED.values()),
    ]
    assert [why for why in reasons if not why.strip()] == []


def _text_flow(text: str, destinations: Destinations | None = None) -> TextFlow:
    return TextFlow([("m", ast.parse(text))], destinations)


_SEED = "    t = rm.zone_text(zp)\n"
# 每種往下追的寫法：``m:f`` 回傳的是區字串
_FOLLOW_FORMS = {
    "zone_text": "def f(rm, zp):\n    return rm.zone_text(zp)\n",
    "指定": f"def f(rm, zp):\n{_SEED}    u = t\n    return u\n",
    "walrus": "def f(rm, zp):\n    if (t := rm.zone_text(zp)):\n        return t\n",
    "拆解": f"def f(rm, zp):\n{_SEED}    a, b = t, 1\n    return a\n",
    "str": f"def f(rm, zp):\n{_SEED}    return str(t)\n",
    "fspath": f"import os\ndef f(rm, zp):\n{_SEED}    return os.fspath(t)\n",
    "Path": f"from pathlib import Path\ndef f(rm, zp):\n{_SEED}    return Path(t)\n",
    "模組別名": f"import pathlib as pl\ndef f(rm, zp):\n{_SEED}    return pl.PurePosixPath(t)\n",
    "os.path": f"from os import path as p\ndef f(rm, zp):\n{_SEED}    return p.join(t, 'x')\n",
    "/": f"def f(rm, zp):\n{_SEED}    return t / 'x'\n",
    "+": f"def f(rm, zp):\n{_SEED}    return t + '/x'\n",
    "%": f"def f(rm, zp):\n{_SEED}    return '%s/x' % t\n",
    "f-string": f"def f(rm, zp):\n{_SEED}    return f'{{t}}/x'\n",
    "format": f"def f(rm, zp):\n{_SEED}    return '{{}}/x'.format(t)\n",
    "join 常數": f"import os\ndef f(rm, zp):\n{_SEED}    return os.pathsep.join([t])\n",
    "純方法": f"def f(rm, zp):\n{_SEED}    return t.rstrip('/')\n",
    "純屬性": f"def f(rm, zp):\n{_SEED}    return t.parent\n",
    "條件運算式": f"def f(rm, zp, c):\n{_SEED}    return t if c else None\n",
    "or": f"def f(rm, zp, c):\n{_SEED}    return c or t\n",
    "下標": f"def f(rm, zp):\n{_SEED}    pair = (t, 1)\n    return pair[0]\n",
    "append": f"def f(rm, zp):\n{_SEED}    out = []\n    out.append(t)\n    return out\n",
    "update": f"def f(rm, zp):\n{_SEED}    env = {{}}\n    env.update(HOME=t)\n    return env\n",
    "子容器": (
        f"def f(rm, zp):\n{_SEED}    d = {{}}\n"
        "    d.setdefault('k', {})['x'] = t\n    return d\n"
    ),
    "容器讀取": f"def f(rm, zp):\n{_SEED}    d = {{'k': t}}\n    return d.get('k')\n",
    "生成式": f"def f(rm, zp):\n{_SEED}    return [x for x in (t,)]\n",
    "dict 生成式": f"def f(rm, zp):\n{_SEED}    return {{k: t for k in 'ab'}}\n",
    "dict.fromkeys": f"def f(rm, zp):\n{_SEED}    return dict.fromkeys([t])\n",
    "yield": f"def f(rm, zp):\n{_SEED}    yield t\n",
    "await": "async def f(rm, zp):\n    return await rm.zone_text(zp)\n",
    "for": f"def f(rm, zp):\n{_SEED}    for x in [t]:\n        return x\n",
    "+=": f"def f(rm, zp):\n{_SEED}    s = ''\n    s += t\n    return s\n",
    "self 屬性": (
        "class C:\n    def set(self, rm, zp):\n        self.root = rm.zone_text(zp)\n"
        "    def f(self):\n        return self.root\n"
    ),
    "產生函式": (
        "def g(rm, zp):\n    return rm.zone_text(zp)\n"
        "def f(rm, zp):\n    return g(rm, zp)\n"
    ),
    "傳過接收函式": (
        "def g(p):\n    return p.lower()\ndef f(rm, zp):\n    return g(rm.zone_text(zp))\n"
    ),
    "閉包": f"def f(rm, zp):\n{_SEED}    def g():\n        return t\n    return g()\n",
    "類別方法": (
        "class C:\n    @classmethod\n    def g(cls, rm, zp):\n        return rm.zone_text(zp)\n"
        "    @classmethod\n    def f(cls, rm, zp):\n        return cls.g(rm, zp)\n"
    ),
    "類別名.方法": (
        "class C:\n    @classmethod\n    def g(cls, rm, zp):\n        return rm.zone_text(zp)\n"
        "def f(rm, zp):\n    return C.g(rm, zp)\n"
    ),
    "to_thread": (
        "import asyncio\ndef g(rm, zp):\n    return rm.zone_text(zp)\n"
        "async def f(rm, zp):\n    return await asyncio.to_thread(g, rm, zp)\n"
    ),
}


@pytest.mark.parametrize("form", sorted(_FOLLOW_FORMS))
def test_tf_follows_every_listed_form(form):
    """每種往下追的寫法各一個：``m:f`` 是產生函式."""
    flow = _text_flow(_FOLLOW_FORMS[form])
    assert any(key.endswith(":f") or key.endswith(".f") for key in flow.producers), form


@pytest.mark.parametrize(
    ("text", "receiver"),
    [
        ("def use(p):\n    pass\ndef f(rm, zp):\n    use(rm.zone_text(zp))\n", ("m:use", "p")),
        (
            "def use(a, *, p):\n    pass\ndef f(rm, zp):\n    use(1, p=rm.zone_text(zp))\n",
            ("m:use", "p"),
        ),
        (
            "from functools import partial\ndef use(p):\n    pass\n"
            "def f(rm, zp):\n    return partial(use, rm.zone_text(zp))\n",
            ("m:use", "p"),
        ),
        (
            "import asyncio\ndef use(p):\n    pass\n"
            "async def f(rm, zp):\n    await asyncio.to_thread(use, rm.zone_text(zp))\n",
            ("m:use", "p"),
        ),
        (
            "def use(p):\n    pass\n"
            "async def f(rm, zp, loop):\n"
            "    await loop.run_in_executor(None, use, rm.zone_text(zp))\n",
            ("m:use", "p"),
        ),
        (
            "class C:\n    def use(self, p):\n        pass\n"
            "    def f(self, rm, zp):\n        self.use(rm.zone_text(zp))\n",
            ("m:C.use", "p"),
        ),
        # 接收函式的參數也是種子：再交給下一個函式也追得到
        (
            "def inner(q):\n    pass\ndef use(p):\n    inner(p)\n"
            "def f(rm, zp):\n    use(rm.zone_text(zp))\n",
            ("m:inner", "q"),
        ),
        (
            "def use(p):\n    pass\ndef f(rm, zp):\n    t = rm.zone_text(zp)\n"
            "    cb = lambda: use(t)\n    return cb\n",
            ("m:use", "p"),
        ),
    ],
)
def test_tf_finds_receivers_and_follows_into_them(text, receiver):
    key, param = receiver
    assert param in _text_flow(text).receiver_params().get(key, set())


@pytest.mark.parametrize(
    "text",
    [
        # 建物件不算往下追（是出口，由 G4 判斷）
        "from dataclasses import dataclass\n@dataclass\nclass C:\n    p: str\n"
        "def f(rm, zp):\n    return C(rm.zone_text(zp))\n",
        # 解析不到的 ``物件.方法`` 不算往下追
        "def f(rm, zp, obj):\n    return obj.convert(rm.zone_text(zp))\n",
        # 純方法以外的方法、比較
        "def f(rm, zp):\n    return rm.zone_text(zp).exists()\n",
        "def f(rm, zp, other):\n    return rm.zone_text(zp) == other\n",
        "def f(rm, zp):\n    return len(rm.zone_text(zp))\n",
        # partial 的結果是函式，不是區字串
        "from functools import partial\ndef use(p):\n    return p\n"
        "def f(rm, zp):\n    return partial(use, rm.zone_text(zp))\n",
    ],
)
def test_tf_does_not_follow_objects_and_unresolved_methods(text):
    assert "m:f" not in _text_flow(text).producers


def test_tf_lists_unresolved_calls_with_a_producer_name():
    text = (
        "def state_dirs(rm, zp):\n    return rm.zone_text(zp)\n"
        "def f(layout):\n    return layout.state_dirs(1, 2)\n"
    )
    assert _text_flow(text).homonyms() == {"m:f:state_dirs"}


_G4 = "def f(rm, zp, other):\n    t = rm.zone_text(zp)\n"


@pytest.mark.parametrize(
    ("body", "exit_"),
    [
        ("    getattr(t, 'exists')()\n", "getattr"),
        (
            "    import operator\n    operator.methodcaller('exists')(t)\n",
            "operator.methodcaller('exists')",
        ),
        ("    import zipfile\n    zipfile.ZipFile(t)\n", "zipfile.ZipFile"),
        ("    import sqlite3\n    sqlite3.connect(t)\n", "sqlite3.connect"),
        ("    import tempfile\n    tempfile.mkstemp(dir=t)\n", "tempfile.mkstemp"),
        ("    import glob\n    glob.glob(t + '/*')\n", "glob.glob"),
        ("    import os\n    os.path.exists(t)\n", "os.path.exists"),
        ("    import os as o\n    o.path.exists(t)\n", "os.path.exists"),
        ("    from os import path as p\n    p.exists(t)\n", "os.path.exists"),
        ("    from pathlib import Path\n    Path(t).touch()\n", "<text>.touch"),
        ("    open(t)\n", "open"),
        ("    other.read(t)\n", "other.read"),
        ("    x = t.stat\n", "<text>.stat"),
        ("    cb = lambda: open(t)\n", "open"),
        ("    other.path = t\n", "store:other.path"),
        ("    other[t] = 1\n", "key:other[]"),
        (
            "    from src.services.execution_broker import spawn_exec\n"
            "    spawn_exec('python', cwd=t)\n",
            "src.services.execution_broker.spawn_exec(cwd)",
        ),
    ],
)
def test_g4_unlisted_uses_of_zone_text_are_red(body, exit_):
    exits = _text_flow(_G4 + body, G4_DESTINATIONS).exits()
    assert exit_ in exits.get("m:f", {}), exits


@pytest.mark.parametrize(
    ("text", "key", "exit_"),
    [
        (
            "CACHE = {}\ndef f(rm, zp):\n    CACHE['k'] = rm.zone_text(zp)\n",
            "m:f",
            "store:CACHE[]",
        ),
        ("def f(rm, zp):\n    global LAST\n    LAST = rm.zone_text(zp)\n", "m:f", "store:LAST"),
        ("LAST = rm.zone_text(zp)\n", "m:<module>", "store:LAST"),
        (
            "def g(rm, zp):\n    return rm.zone_text(zp)\ndef f(cb):\n    return cb(g)\n",
            "m:f",
            "ref:m:g",
        ),
        (
            "def use(p):\n    open(p)\ndef f(rm, zp):\n    use(rm.zone_text(zp))\n",
            "m:use",
            "open",
        ),
    ],
)
def test_g4_stores_outside_the_function_and_receivers_are_checked(text, key, exit_):
    assert exit_ in _text_flow(text, G4_DESTINATIONS).exits().get(key, {})


@pytest.mark.parametrize(
    "body",
    [
        # 列名的去處
        "    from src.services.isolation.profiles import PathRule\n    PathRule(t, 'rwx')\n",
        "    from src.services.execution_broker import spawn_exec\n    spawn_exec('python', t)\n",
        "    logger.info('root %s', t)\n",
        # 比較、len、isinstance
        "    return t == other\n",
        "    return t in other\n",
        "    return len(t) + hash(t)\n",
        # 往下追、函式內容器
        "    env = {}\n    env['HOME'] = t\n    out = []\n    out.append(t)\n    return env, out\n",
        "    return t.rstrip('/').lower()\n",
        "    from pathlib import PurePath\n    return PurePath(t).is_relative_to(other)\n",
        # self 的屬性
        "    self.root = t\n",
    ],
)
def test_g4_listed_places_comparisons_and_follow_forms_are_not_red(body):
    assert "m:f" not in _text_flow(_G4 + body, G4_DESTINATIONS).exits()


def test_g4_a_receiver_that_only_compares_is_not_red():
    text = (
        "def same(a, b):\n    return a == b\n"
        "def f(rm, zp, b):\n    return same(rm.zone_text(zp), b)\n"
    )
    assert _text_flow(text, G4_DESTINATIONS).exits() == {}


# --- G3 區內值只在 zone_paths 與 RM 建出（ADR-0293 PR 2 Q8） --------------------

G3_NAMES = frozenset({"ZonePath", "agent_root"})
G3_HOMES = frozenset({ZONE_PATHS, "ToukaProxy/src/services/resource_manager.py"})
_TYPE_CHECKS = frozenset({"isinstance", "issubclass"})
_CLASS_ATTR = "__class__"
_TYPE_CALL = "type(…)(…)"
_PIPELINE_FIELDS = "讀 Pydantic 類別的 model_fields 判斷鍵（不建物件）"
_GRANT_COPY = "權限授予物件的不可變更新（_GrantBase 的子類別，和區內值無關）"
# 換類別再建的寫法：「模組:函式」→（{寫法: 次數}, 理由）；新增就紅
G3_CLASS_TRICKS: dict[str, tuple[dict[str, int], str]] = {
    "memory_server.utils.no_follow:zone_errors": (
        {_TYPE_CALL: 1},
        "OSError 換成只帶顯示名稱的同類例外（實體路徑放 zone_abs；不是 ZonePath）",
    ),
    "src.middleware.pipeline:ProxyPipeline.execute": ({_CLASS_ATTR: 1}, "記錄中介層的類別名稱"),
    "src.models.context:PipelineContext.__contains__": ({_CLASS_ATTR: 1}, _PIPELINE_FIELDS),
    "src.models.context:PipelineContext.__getitem__": ({_CLASS_ATTR: 1}, _PIPELINE_FIELDS),
    "src.models.context:PipelineContext.__setitem__": ({_CLASS_ATTR: 1}, _PIPELINE_FIELDS),
    "src.utils.permission_grants:_GrantBase.extend": ({_TYPE_CALL: 1}, _GRANT_COPY),
    "src.utils.permission_grants:_GrantBase.with_floor": ({_TYPE_CALL: 1}, _GRANT_COPY),
}


def _type_check_nodes(tree: ast.Module) -> set[int]:
    """``isinstance``／``issubclass`` 第二個參數裡的節點（含 tuple）."""
    found = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _TYPE_CHECKS
            and len(node.args) >= 2
        ):
            found.update(id(n) for n in ast.walk(node.args[1]))
    return found


def g3_refs(sources: Iterable[Source]) -> list[str]:
    """``ZonePath``、``agent_root`` 的引用（「檔案:行:名稱」；標註、型別判斷與 import 不算）."""
    found = []
    for source in sources:
        if source.rel in G3_HOMES:
            continue
        aliases = module_aliases(source.tree)
        skipped = _annotation_nodes(source.tree) | _type_check_nodes(source.tree)
        nodes = [n for n in ast.walk(source.tree) if id(n) not in skipped]
        refs = [n for n in nodes if isinstance(n, (ast.Name, ast.Attribute))]
        for node in refs:
            name = node.attr if isinstance(node, ast.Attribute) else aliases.get(node.id, node.id)
            if name in G3_NAMES:
                found.append(f"{source.rel}:{node.lineno}:{name}")
        for node in nodes:
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _ATTRIBUTE_BUILTINS
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in G3_NAMES
            ):
                found.append(f"{source.rel}:{node.lineno}:{node.func.id}:{node.args[1].value}")
    return found


def g3_class_tricks(sources: Iterable[Source]) -> dict[str, dict[str, int]]:
    found = {}
    for source in sources:
        for key, nodes in scopes(source):
            tricks: Counter[str] = Counter()
            for node in nodes:
                if isinstance(node, ast.Attribute) and node.attr == _CLASS_ATTR:
                    tricks[_CLASS_ATTR] += 1
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Call)
                    and isinstance(node.func.func, ast.Name)
                    and node.func.func.id == "type"
                ):
                    tricks[_TYPE_CALL] += 1
            if tricks:
                found[key] = dict(tricks)
    return found


def test_zone_values_are_built_only_in_zone_paths_and_the_resource_manager():
    assert g3_refs(product_sources()) == []


def test_class_tricks_in_product_code_are_listed():
    expected = {key: tricks for key, (tricks, _why) in G3_CLASS_TRICKS.items()}
    assert g3_class_tricks(product_sources()) == expected
    assert [key for key, (_tricks, why) in G3_CLASS_TRICKS.items() if not why.strip()] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("def f(a):\n    return ZonePath(a, ())\n", ["m.py:2:ZonePath"]),
        ("def f(a):\n    make = ZonePath\n    return make(a, ())\n", ["m.py:2:ZonePath"]),
        ("from z import ZonePath as Z\ndef f(a):\n    return Z(a, ())\n", ["m.py:3:ZonePath"]),
        ("def f(z, a):\n    return z.ZonePath(a, ())\n", ["m.py:2:ZonePath"]),
        ("def f(rm, a):\n    return rm.agent_root(a)\n", ["m.py:2:agent_root"]),
        ("def f(rm):\n    g = rm.agent_root\n", ["m.py:2:agent_root"]),
        ("def f(z):\n    return getattr(z, 'ZonePath')\n", ["m.py:2:getattr:ZonePath"]),
        ("ROOTS = {'a': ZonePath}\n", ["m.py:1:ZonePath"]),
        # 標註、型別判斷與 import 不算
        ("from z import ZonePath\ndef f(a: ZonePath) -> ZonePath | None:\n    x: ZonePath\n", []),
        ("def f(a):\n    return isinstance(a, (str, ZonePath))\n", []),
        ("def f(a):\n    return issubclass(a, ZonePath)\n", []),
    ],
)
def test_g3_finds_every_reference(text, expected):
    assert g3_refs([Source("m.py", "m", ast.parse(text))]) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("def f(zp):\n    return zp.__class__(a, p)\n", {"m:f": {_CLASS_ATTR: 1}}),
        ("def f(zp):\n    k = zp.__class__\n", {"m:f": {_CLASS_ATTR: 1}}),
        ("def f(zp):\n    return type(zp)(a, p)\n", {"m:f": {_TYPE_CALL: 1}}),
        ("def f(zp):\n    return type(zp) is str\n", {}),
    ],
)
def test_g3_finds_class_tricks(text, expected):
    assert g3_class_tricks([Source("m.py", "m", ast.parse(text))]) == expected


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
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith(f"{dotted}.")
        ):
            found.add(node.value[len(dotted) + 1 :].split(".")[0])  # setattr("….<module>.名稱", …)
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


def test_mentions_finds_the_dotted_string_form():
    text = 'def t(m):\n    m.setattr("tests.support.zone_records.remaining", list)\n'
    assert _mentions(Source("m.py", "m", ast.parse(text)), "zone_records") == {"remaining"}


# conftest 判區的紀錄與 Z9 全量檢查的開關：換掉就讓 session 不紅
# 只有直接呼叫 session hook 的測試能碰
CONFTEST_ZONE_SWITCHES = frozenset(
    {"_ZONE_FULL_RUN", "_zone_late_records", "_zone_unused_allowances"}
)
CONFTEST_SWITCH_USERS = frozenset({"tests/unit/test_session_sandbox_cleanup.py"})


def _conftest_switches(source: Source) -> set[str]:
    """``source`` 用到的 conftest 開關：名稱、屬性、匯入、``setattr(x, "名稱", …)`` 與點分字串."""
    found: set[str] = set()
    for node in ast.walk(source.tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.Call) and len(node.args) >= 2 and _literal(node.args[1]):
            found.add(_literal(node.args[1]))  # type: ignore[arg-type]
        elif isinstance(node, ast.Constant) and str(node.value).startswith("tests.conftest."):
            found.add(str(node.value).rsplit(".", 1)[-1])
    return found & CONFTEST_ZONE_SWITCHES


def test_only_listed_tests_touch_the_conftest_zone_switches():
    users = {
        source.rel
        for source in suite_sources()
        if source.rel != "tests/conftest.py" and _conftest_switches(source)
    }
    assert users == CONFTEST_SWITCH_USERS


@pytest.mark.parametrize(
    "text",
    [
        'def t(m):\n    m.setattr(session_conftest, "_ZONE_FULL_RUN", False)\n',
        'def t(m):\n    m.setattr("tests.conftest._zone_late_records", [])\n',
        "from tests.conftest import _zone_unused_allowances\n",
        "def t():\n    session_conftest._ZONE_FULL_RUN = False\n",
    ],
)
def test_conftest_switch_uses_are_found(text):
    assert _conftest_switches(Source("m.py", "m", ast.parse(text)))


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


# 白名單的檔名在 repo 裡（git ls-files）對到的檔：lint_ssot 只比檔名，同名的檔整檔都不檢查，所以
# 新增同名檔就紅。今天就對到多個的照實列出（不在 ADR-0293 PR 2 縮小白名單）
LINT_SSOT_MULTI_PATHS: dict[str, frozenset[str]] = {
    "config.py": frozenset(
        {"ToukaHeartbeat/config.py", "ToukaProxy/src/config.py", "ToukaProxy/src/routers/config.py"}
    ),
    "bootstrap.py": frozenset(
        {
            "ToukaProxy/src/core/bootstrap.py",
            "ToukaProxy/src/utils/bootstrap.py",
            "ToukaProxy/src/protocols/mcp/oauth/bootstrap.py",
            "ToukaProxy/src/protocols/mcp/oauth/workload/bootstrap.py",
        }
    ),
    "conftest.py": frozenset({"tests/conftest.py", "tests/integration/conftest.py"}),
    "config_utils.py": frozenset(
        {"ToukaMemory/src/memory_server/config_utils.py", "ToukaProxy/src/utils/config_utils.py"}
    ),
    "terminal.py": frozenset(
        {
            "ToukaProxy/src/tools/terminal.py",
            "ToukaProxy/src/models/terminal.py",
            "ToukaProxy/src/routers/terminal.py",
        }
    ),
}
# 上面這些路徑裡，不是白名單本意、只因同名而整檔豁免的
LINT_SSOT_SAME_NAME_ONLY = frozenset(
    {
        "ToukaProxy/src/routers/config.py",
        "ToukaProxy/src/protocols/mcp/oauth/bootstrap.py",
        "ToukaProxy/src/protocols/mcp/oauth/workload/bootstrap.py",
        "ToukaProxy/src/models/terminal.py",
        "ToukaProxy/src/routers/terminal.py",
    }
)
LINT_SSOT_SINGLE_PATHS = frozenset(
    {
        "ToukaProxy/src/utils/time_utils.py",
        "scripts/update_docs.py",
        "ToukaProxy/src/scripts/export_schema.py",
        "ToukaProxy/src/services/resource_manager.py",
        "ToukaProxy/src/utils/soul_io.py",
        "ToukaProxy/src/utils/custom_io.py",
        "ToukaMemory/src/memory_server/tools/core_file_ops.py",
        "ToukaMemory/src/memory_server/utils/no_follow.py",
        ZONE_PATHS,
        "tests/unit/test_ssot_guard_path.py",
        "tests/unit/test_ssot_guard_io.py",
        "tests/unit/test_ssot_guard_local_import.py",
        "tests/smoke/smoke_mcp_lifecycle.py",
        "tests/smoke/smoke_io_prison.py",
        "tests/smoke/smoke_ldh_handshake.py",
        "tests/smoke/smoke_mission_awareness.py",
        "tests/smoke/smoke_group_dispatcher.py",
        "tests/smoke/smoke_soul_recovery.py",
        "tests/smoke/smoke_agent_interaction.py",
        "tests/integration/test_api_schema_sync.py",
        "scripts/update_openapi.py",
        "tests/smoke/smoke_autonomy_enforcement.py",
        "tests/smoke/run_all_smoke_tests.py",
        "tests/smoke/smoke_chat.py",
        "tests/smoke/smoke_frontend_persistence.py",
        "tests/smoke/smoke_memory_tools.py",
        "tests/smoke/smoke_model_governance.py",
        "tests/smoke/smoke_pulse_rhythm.py",
        "ToukaProxy/scripts/manage_backup.py",
        "ToukaHeartbeat/awaken.py",
        "tests/unit/test_lint_ssot.py",
        "ToukaProxy/src/core/logging_config.py",
        "tests/unit/test_h3_h4_security.py",
        "tests/unit/test_dispatch_server_tools.py",
        "tests/integration/test_mcp_dispatch_tools.py",
        "tests/unit/test_mission_manager_bugfixes.py",
        "tests/unit/test_dispatch_session.py",
        "tests/unit/test_dispatch_mission_token.py",
        "tests/unit/test_dispatch_hooks.py",
        "tests/unit/test_dispatch_board_integrity.py",
        "tests/unit/test_dispatch_content_sanitization.py",
        "tests/unit/test_heartbeat_config_dir_mount.py",
        "tests/unit/test_docker_compose_config.py",
        "tests/unit/test_env_persistence_anchor.py",
    }
)


def test_the_lint_ssot_whitelist_names_match_only_the_listed_files():
    found: dict[str, frozenset[str]] = {}
    for path in git_ls_files():
        name = PurePosixPath(path).name
        if name in LINT_SSOT_WHITELIST:
            found[name] = found.get(name, frozenset()) | {path}
    expected = {PurePosixPath(p).name: frozenset({p}) for p in LINT_SSOT_SINGLE_PATHS}
    assert found == expected | LINT_SSOT_MULTI_PATHS
    listed = frozenset().union(*LINT_SSOT_MULTI_PATHS.values())
    assert listed >= LINT_SSOT_SAME_NAME_ONLY


# --- T3 測試不能替換區內產生函式與 data 根 -----------------------------------

# RM 的 Z5 方法：本體呼叫過它們的函式也不能替換
Z5_METHODS = frozenset(
    {"agent_drive", "agent_drive_for", "soul_drive", "custom_drive", "outputs_dir", "uploads_dir"}
    | {"tool_root", "sandbox_state_proxy", "sandbox_state_dir"}  # ADR-0293 PR 2 P3
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
        "sandbox_state_root",  # ST 根的來源（ADR-0293 PR 2 P7）
        "_state_setting_or_default",
    }
)
# 交出區字串與 ST 的函式（ADR-0293 PR 2 P4、P6、P7）；TF 的兩份名單另外加進保護
T3_TEXT_NAMES = frozenset({"zone_text", "handoff_cwd", "proxy_state_zone", "proxy_state_owner"})
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
# 改容器內容的方法：``zp._handoffs.clear()``、``os.environ.pop("名稱")``
_MUTATORS = frozenset(
    {"append", "extend", "insert", "pop", "popitem", "remove", "clear", "update", "setdefault"}
    | {"add", "discard"}
)
_ENV_SELECTION = "測 Memory 選哪個 .env：data 根的環境變數有沒有設是這個測試的輸入"
_DATA_ROOT_PARSING = "測 Memory 解析 data 根的環境變數本身（Docker 下的絕對與相對值）"
_STATE_ROOT_PARSING = "測 ST 根的解析本身：沒設、容器裡、經過人做的連結（ADR-0293 PR 2）"
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
    "tests.unit.test_heartbeat_env_selection:run_restart_audit_child": (
        1,
        "另開的子行程在匯入產品程式之前，把 data 根指到這個測試自己的資料夾（測重啟後讀回設定）",
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
    # 下面五個測的是 ST 根的解析本身（ADR-0293 PR 2 P6、P7）：沒設、容器裡、經過人做的連結
    "tests.unit.test_resource_manager_anchors:"
    "test_handoff_cwd_refuses_the_literal_form_of_a_linked_anchor": (1, _STATE_ROOT_PARSING),
    "tests.unit.test_resource_manager_anchors:test_handoff_cwd_through_a_linked_state_root": (
        1,
        _STATE_ROOT_PARSING,
    ),
    "tests.unit.test_resource_manager_anchors:"
    "test_sandbox_state_generators_give_none_only_when_unset": (1, _STATE_ROOT_PARSING),
    "tests.unit.test_resource_manager_anchors:test_sandbox_state_root": (2, _STATE_ROOT_PARSING),
    "tests.unit.test_sandbox_state_access:rm_unset": (1, _STATE_ROOT_PARSING),
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


def _target_name(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """被替換或被改內容的對象名稱：點分字串取最後一段，其他取最後一個名稱（別名對回原名）."""
    text = _literal(node)
    return text.rsplit(".", 1)[-1] if text is not None else called_name(node, aliases)


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
    elif name in _MUTATORS and isinstance(func, ast.Attribute) and _is_environ(func.value):
        if args and _literal(args[0]) is not None:
            found.append(_literal(args[0]))  # type: ignore[arg-type]  # pop、setdefault
        for value in args:
            if isinstance(value, ast.Dict):
                found.extend(t for k in value.keys if (t := _literal(k)) is not None)
        found.extend(k.arg for k in call.keywords if k.arg)
    return found


def _changed_containers(node: ast.AST, aliases: dict[str, str]) -> list[str]:
    """直接改內容的容器（``os.environ`` 另算；別名對回原名）.

    ``setitem(zp._handoffs, …)``、``patch.dict(zp._handoffs, …)``、``zp._handoffs.clear()``、
    ``zp._handoffs[k] = …``、``_observers[:] = []``、``_observers += […]``。
    """
    found: list[str | None] = []
    if isinstance(node, ast.Call) and node.args:
        func, first = node.func, node.args[0]
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        patching = name == "dict" and "patch" in ast.unparse(func)
        if (name in ("setitem", "delitem") or patching) and not _is_environ(first):
            found.append(_target_name(first, aliases))
    func = node.func if isinstance(node, ast.Call) else None
    if isinstance(func, ast.Attribute) and func.attr in _MUTATORS and not _is_environ(func.value):
        found.append(called_name(func.value, aliases))
    if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
        targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
        found.extend(
            called_name(target.value, aliases)
            for target in targets
            if isinstance(target, ast.Subscript) and not _is_environ(target.value)
        )
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
        found.append(called_name(node.target, aliases))  # 串列的 += 是原地修改
    return [name for name in found if name]


def _patched_owner(call: ast.Call, aliases: dict[str, str]) -> str | None:
    """被替換屬性的擁有者：``setattr(ZoneRoots, "resolved", f)``、``patch("….ZoneRoots.x")``、
    ``patch.multiple(ZoneRoots, …)``（別名對回原名）."""
    func, args = call.func, call.args
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if not args or name not in ("setattr", "delattr", "patch", "object", "multiple"):
        return None
    if name in ("patch", "object", "multiple") and "patch" not in ast.unparse(func):
        return None
    text = _literal(args[0])
    if text is not None:
        parts = text.split(".")
        if name == "multiple":
            return parts[-1]
        return parts[-2] if len(parts) >= 2 else None
    return called_name(args[0], aliases) if name != "patch" else None


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


def _assigned_owners(node: ast.AST, aliases: dict[str, str]) -> list[str]:
    """``ZoneRoots.resolved = f`` 一類：被指定屬性的擁有者（別名對回原名）."""
    if not isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
        return []
    targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
    return [
        owner
        for target in targets
        if isinstance(target, ast.Attribute) and (owner := called_name(target.value, aliases))
    ]


def _zone_paths_body() -> list[ast.stmt]:
    return next(s.tree for s in product_sources() if s.rel == ZONE_PATHS).body


def zone_paths_state_names() -> frozenset[str]:
    """``zone_paths.py`` 模組層指定的名稱（私有的根、身分、交接與觀察者狀態）."""
    names: set[str] = set()
    for node in _zone_paths_body():
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(n.id for n in targets if isinstance(n, ast.Name))
    return frozenset(names - ZONE_PATHS_GENERIC_NAMES)


def zone_paths_class_names() -> frozenset[str]:
    """``zone_paths.py`` 定義的類別：替換它們的方法或屬性也會改區判斷."""
    return frozenset(node.name for node in _zone_paths_body() if isinstance(node, ast.ClassDef))


def zone_paths_module_names() -> frozenset[str]:
    """``zone_paths.py`` 模組層定義的所有名稱（含私有的根、身分、交接與觀察者狀態）."""
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    defined = {node.name for node in _zone_paths_body() if isinstance(node, kinds)}
    return frozenset(defined - ZONE_PATHS_GENERIC_NAMES) | zone_paths_state_names()


def _short_names(keys: Iterable[str]) -> set[str]:
    """「模組:qualname」的最後一段（測試替換時用的名稱）."""
    return {key.rsplit(":", 1)[-1].rsplit(".", 1)[-1] for key in keys}


@cache
def t3_protected() -> frozenset[str]:
    """區內產生函式（T2 的集合）、回傳標註有 ``ZonePath`` 的、呼叫過 Z5 方法的、data 根，加上
    ``zone_paths`` 的模組層名稱、交出區字串與 ST 的函式和 TF 的兩份名單."""
    names = set(product_flow().names) | DATA_ROOT_NAMES | zone_paths_module_names()
    names |= T3_TEXT_NAMES
    names |= _short_names(ZONE_TEXT_PRODUCERS)
    names |= _short_names(ZONE_TEXT_RECEIVERS)
    for source in product_sources():
        aliases = module_aliases(source.tree)
        for fn in functions(source.module, source.tree):
            if fn.node.returns is not None and "ZonePath" in ast.unparse(fn.node.returns):
                names.add(fn.node.name)
            calls = (n for n in own_nodes(fn.node) if isinstance(n, ast.Call))
            if any(called_name(c.func, aliases) in Z5_METHODS for c in calls):
                names.add(fn.node.name)
    return frozenset(names)


def t3_sites(
    sources: Iterable[Source],
    protected: frozenset[str],
    owners: frozenset[str] = frozenset(),
    containers: frozenset[str] | None = None,
) -> dict[str, int]:
    """每個範圍替換受保護名稱的次數；``owners`` 是方法也不能換的類別，``containers`` 是內容不能
    直接改的容器（預設同 ``protected``）."""
    found: Counter[str] = Counter()
    watched = protected | owners
    containers = protected if containers is None else containers
    for source in sources:
        aliases = module_aliases(source.tree)
        for key, nodes in scopes(source):
            for node in nodes:
                names = _patched_names(node) if isinstance(node, ast.Call) else []
                if isinstance(node, ast.Call):
                    called = called_name(node.func, aliases)
                    names += [called] if called in T3_DIRECT_CALLS else []
                    owner = _patched_owner(node, aliases)
                    names += [owner] if owner in owners else []
                names += _assigned_names(node)
                names += [o for o in _assigned_owners(node, aliases) if o in owners]
                found[key] += sum(1 for name in names if name in watched)
                changed = _changed_containers(node, aliases)
                found[key] += sum(1 for name in changed if name in containers)
    return {key: n for key, n in found.items() if n}


def test_tests_do_not_replace_zone_producers_or_the_data_root():
    containers = zone_paths_state_names() | DATA_ROOT_NAMES
    sites = t3_sites(suite_sources(), t3_protected(), zone_paths_class_names(), containers)
    assert sites == {key: n for key, (n, _why) in T3_ALLOWED.items()}


def test_t3_covers_the_producers_and_the_data_root():
    protected = t3_protected()
    assert protected >= Z5_METHODS | DATA_ROOT_NAMES | T3_DIRECT_CALLS
    assert {"_agent_artifact_root", "_get_mission_path", "_own_drive"} <= protected
    state = {"_roots_provider", "_identity", "_handoffs", "_observers", "zone_of", "contains_zone"}
    assert state | {"current_roots", "is_handed_off"} <= protected
    assert "logger" not in protected
    # ADR-0293 PR 2：用名稱釘住這批（T3_TEXT_NAMES、TF 的兩份名單、P7）
    assert {"tool_root", "sandbox_state_proxy", "sandbox_state_dir"} <= Z5_METHODS
    text = {"zone_text", "handoff_cwd", "proxy_state_zone", "proxy_state_owner"}
    assert text | {"sandbox_state_root", "_state_setting_or_default"} <= protected
    producers = {"get_tool_root", "mcp_classification_bases", "mcp_state_dirs", "state_dirs"}
    assert producers | {"_state", "_venv_python"} <= protected
    receivers = {"unbound_or_refuse", "_name_key", "_nested", "_overlapping_roots", "_profile_for"}
    receivers |= {"_resolve", "build_evolution_profile", "public_config_rules", "sandbox_env"}
    receivers |= {"bound_of", "_build_jail_code", "_save_deployment", "classify"}
    receivers |= {"scaffold_dir_for", "referenced_paths", "_token_paths", "_item_paths"}
    assert receivers | {"_candidates", "is_file_reference"} <= protected
    assert _short_names(ZONE_TEXT_PRODUCERS) | _short_names(ZONE_TEXT_RECEIVERS) <= protected


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
        (
            "from memory_server.utils.zone_paths import _register_handoff as reg\n"
            "def t():\n    reg(p, k)\n",
            "_register_handoff",
        ),
        ("def t(m):\n    m.setitem(zp._handoffs, k, v)\n", "_handoffs"),
        ("def t():\n    zp._handoffs[k] = v\n", "_handoffs"),
        ("def t():\n    _observers[:] = []\n", "_observers"),
        ("def t():\n    del zp._handoffs[k]\n", "_handoffs"),
        ("def t():\n    zp._handoffs.clear()\n", "_handoffs"),
        ("def t():\n    patch.dict(zp._handoffs, {k: v})\n", "_handoffs"),
        ('def t():\n    patch.dict("memory_server.utils.zone_paths._handoffs", {})\n', "_handoffs"),
        ('def t():\n    os.environ.pop("MEMORY_APP_DATA_DIR", None)\n', "MEMORY_APP_DATA_DIR"),
        ('def t():\n    os.environ.update({"MEMORY_APP_DATA_DIR": x})\n', "MEMORY_APP_DATA_DIR"),
        (
            "from memory_server.utils.zone_paths import _handoffs as H\ndef t():\n    H.clear()\n",
            "_handoffs",
        ),
        (
            "from memory_server.utils.zone_paths import _handoffs as H\n"
            "def t(m):\n    m.setitem(H, k, v)\n",
            "_handoffs",
        ),
        (
            "from memory_server.utils.zone_paths import _handoffs as H\ndef t():\n    H[k] = v\n",
            "_handoffs",
        ),
        (
            "from memory_server.utils.zone_paths import _observers\n"
            "def t():\n    global _observers\n    _observers += [cb]\n",
            "_observers",
        ),
    ],
)
def test_t3_finds_every_way_of_replacing_a_name(text, name):
    assert t3_sites([Source("m.py", "m", ast.parse(text))], frozenset({name})) == {"m:t": 1}


@pytest.mark.parametrize(
    "text",
    [
        'def t(m):\n    m.setattr(ZoneRoots, "resolved", f)\n',
        'def t(m):\n    m.setattr("memory_server.utils.zone_paths.ZoneRoots.resolved", f)\n',
        'def t():\n    with patch.object(zp.ZoneRoots, "resolved"):\n        pass\n',
        'def t():\n    with patch("memory_server.utils.zone_paths.ZoneRoots.resolved"):\n'
        "        pass\n",
        "def t():\n    ZoneRoots.resolved = f\n",
        "from memory_server.utils.zone_paths import ZoneRoots as ZR\n"
        'def t(m):\n    m.setattr(ZR, "resolved", f)\n',
        "from memory_server.utils.zone_paths import ZoneRoots as ZR\n"
        "def t():\n    ZR.resolved = f\n",
        "def t():\n    with patch.multiple(zp.ZoneRoots, resolved=f):\n        pass\n",
        'def t():\n    patch.multiple("memory_server.utils.zone_paths.ZoneRoots", resolved=f)\n',
    ],
)
def test_t3_finds_replaced_methods_of_zone_classes(text):
    owners = frozenset({"ZoneRoots"})
    assert t3_sites([Source("m.py", "m", ast.parse(text))], frozenset(), owners) == {"m:t": 1}


def test_t3_owners_are_the_zone_paths_classes():
    assert {"ZonePath", "ZoneRoots", "ZoneMarkers"} <= zone_paths_class_names()


@pytest.mark.parametrize(
    "text",
    [
        'def t(m):\n    m.setattr(rm, "exists", f)\n',  # 不在清單上的名稱
        'def t(m):\n    m.setenv("TOUKA_ENV_FILE", x)\n',
        "def t(self):\n    self.resource_manager = rm\n",  # 測試類別自己的屬性
        'def t():\n    value = os.environ["MEMORY_APP_DATA_DIR"]\n',  # 只是讀
        "def t():\n    value = zp._handoffs[k]\n",
        "def t():\n    value = zp._handoffs.get(k)\n",
        'def t():\n    os.environ.pop("TOUKA_ENV_FILE", None)\n',
        "from memory_server.utils.zone_paths import _handoffs as H\n"
        "def t():\n    value = H.get(k)\n",
        "def t():\n    count = 0\n    count += 1\n",
    ],
)
def test_t3_leaves_other_names_and_plain_reads_alone(text):
    assert t3_sites([Source("m.py", "m", ast.parse(text))], t3_protected()) == {}
