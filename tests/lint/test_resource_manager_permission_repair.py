"""ResourceManager 的權限自癒歸類（iso-c2 U2n，設計 §N7）：AST 結構測試，Windows 也能跑.

1. 直接 I/O：類別所有方法（含私有、async、巢狀函式）與同檔模組函式，呼叫 ``open``／``io.open``、
   ``os.*``、``shutil.*``、``tempfile.*``、``zipfile.*``、``os.path`` 的查詢、Path 類的檔案方法
   （``replace``／只有 1 個位置參數才算）或 ``IO_HELPERS``／``WRITE_IO_HELPERS``（不跟隨連結的
   讀寫）。別名與 from-import 先對回原模組。
2. 遞移：沿 ``self.<方法>`` 與同檔模組函式傳遞；呼叫 ``path_only`` 豁免方法不傳遞。
3. 被偵測到的公開方法恰好一個裝飾器：``permission_repair(retry=<字面 bool>)`` 或
   ``permission_repair_exempt(category=…, reason=<非空字串>)``，並和下面兩份釘選清單完全相等。
4. 豁免不能誤用；``retry=True`` 只能呼叫 retry=True、``path_only`` 或不碰檔案的 self 方法；
   ``permission_repair`` 只能裝在同步方法上。
5. 棘輪：不得從 os／os.path／shutil／tempfile／zipfile／io from-import 或取別名；
   ``PermissionError(...)`` 恰好一個位置參數；``OSError``／``IOError``／``EnvironmentError`` 不得帶
   2 個以上參數（帶 errno 的會建出 PermissionError，保護拒絕的 errno 就不再是 None）。

記憶系統第 7 段（ADR-0270）的 6 個方法（``rewrite_archive``、``copy_file_metadata``、
``fsync_file``、``fsync_dir``、``read_file_prefix``、``make_writable``）歸為
``swap_semantics``，列在 ``EXPECTED_EXEMPT``；
之後新增會碰檔案的方法，漏歸類這裡會紅。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RM_FILE = ROOT / "ToukaProxy" / "src" / "services" / "resource_manager.py"

EXPECTED_RETRY = {
    "get_outputs_path": True,
    "get_uploads_path": True,
    "get_vault_root": True,
    "list_all_agent_stats": True,
    "read_file_text": True,
    "read_file_binary": True,
    "read_file_text_no_follow": True,
    "read_file_binary_no_follow": True,
    "exists": True,
    "make_dir": True,
    "iter_files": True,
    "stat_files": True,
    "list_directory": True,
    "glob_search": True,
    "write_file_binary": True,  # 權限錯誤只在開檔時、截斷之前
    "write_file_text": False,
    "copy_file": False,
    "remove_dir": False,
    "create_archive": False,
    "extract_archive": False,
    "append_file_text": False,
    "write_file_text_no_follow": False,
    "append_file_text_no_follow": False,
    "remove_file_no_follow": False,
    "append_csv_row": False,
}
# 方法 → (類別, 理由裡必須出現的摘要)
EXPECTED_EXEMPT = {
    "move_file": ("swap_semantics", "in_use"),
    "remove_file": ("swap_semantics", "原樣往上丟"),
    "rewrite_archive": ("swap_semantics", "一部分輸出"),
    "copy_file_metadata": ("swap_semantics", "改權限"),
    "fsync_file": ("swap_semantics", "置換前"),
    "fsync_dir": ("swap_semantics", "改名後"),
    "read_file_prefix": ("swap_semantics", "中斷暫存"),
    "make_writable": ("swap_semantics", "唯讀位元"),
    "is_protected_path": ("path_only", "路徑"),
    "get_vault_path": ("path_only", "路徑"),
    "is_within_workspace": ("path_only", "路徑"),
    "read_json": ("delegates", "read_file_text"),
    "write_json": ("delegates", "write_file_text"),
    "query_ui_conversations": ("delegates", "query_ui_conversations"),
}

IO_MODULES = ("os", "shutil", "tempfile", "zipfile", "io")
RATCHET_MODULES = frozenset({*IO_MODULES, "os.path"})
OS_PATH_IO = frozenset(
    {"exists", "lexists", "isdir", "isfile", "islink", "ismount", "realpath", "getsize",
     "getmtime", "getatime", "getctime", "samefile"}
)
OS_READ_ONLY = frozenset(
    {"stat", "lstat", "listdir", "scandir", "access", "readlink", "fspath", "fsdecode",
     "fsencode", "walk"}
)
READ_ATTRS = frozenset(
    {"read_text", "read_bytes", "exists", "is_dir", "is_file", "is_symlink", "is_junction",
     "is_mount", "is_fifo", "is_socket", "is_block_device", "is_char_device", "stat", "lstat",
     "iterdir", "glob", "rglob", "walk", "resolve", "readlink", "samefile", "owner", "group"}
)
WRITE_ATTRS = frozenset(
    {"write_text", "write_bytes", "mkdir", "rmdir", "unlink", "rename", "touch", "chmod",
     "lchmod", "chown", "symlink_to", "hardlink_to"}
)
IO_HELPERS = frozenset(
    {"validate_no_symlink_escape", "read_text_no_follow", "read_bytes_no_follow"}
)
WRITE_IO_HELPERS = frozenset(
    {"write_text_no_follow", "replace_text_no_follow", "remove_no_follow"}
)
EXTERNAL_READ_DELEGATES = frozenset({"self._ui_conversation_reader.query_ui_conversations"})
CATEGORIES = frozenset({"swap_semantics", "path_only", "delegates"})
OSERROR_NAMES = frozenset({"OSError", "IOError", "EnvironmentError"})


@dataclass
class Unit:
    """一個方法或模組函式的分析結果。"""

    node: ast.FunctionDef | ast.AsyncFunctionDef
    io: set[str] = field(default_factory=set)  # "read"／"write"
    delegated_io: set[str] = field(default_factory=set)
    self_calls: set[str] = field(default_factory=set)
    func_calls: set[str] = field(default_factory=set)
    marks: list[tuple] = field(default_factory=list)  # ("repair", retry) / ("exempt", cat, reason)


@dataclass
class Analysis:
    detected: set[str]
    marks: dict[str, tuple]
    violations: list[str]


def _aliases(tree: ast.Module, violations: list[str]) -> dict[str, str]:
    """模組層級的 import → 名稱對回原模組（也順便執行匯入棘輪）。"""
    names: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name in RATCHET_MODULES:
                    violations.append(f"alias import: {alias.name} as {alias.asname}")
                bound = alias.asname or alias.name.split(".")[0]
                names[bound] = alias.name if alias.asname else bound
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module in RATCHET_MODULES:
                violations.append(f"from-import: {node.module}")
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    return None


def _open_kind(call: ast.Call, mode_index: int) -> str:
    mode = call.args[mode_index] if len(call.args) > mode_index else None
    mode = next((kw.value for kw in call.keywords if kw.arg == "mode"), mode)
    text = mode.value if isinstance(mode, ast.Constant) and isinstance(mode.value, str) else "r"
    return "write" if set(text) & set("wax+") else "read"


def _call_io(call: ast.Call, aliases: dict[str, str], module_funcs: set[str]) -> str | None:
    func = call.func
    full = _dotted(func, aliases) or ""
    root = full.split(".")[0]
    if full in {"open", "io.open"}:
        return _open_kind(call, 1)
    if full.startswith("os.path."):
        return "read" if full.rsplit(".", 1)[1] in OS_PATH_IO else None
    if root in IO_MODULES and "." in full:
        if root == "os" and full.split(".")[1] in OS_READ_ONLY:
            return "read"
        return "write"
    helper = full.rsplit(".", 1)[-1] if isinstance(func, ast.Name) or "." in full else ""
    if helper in IO_HELPERS:
        return "read"
    if helper in WRITE_IO_HELPERS:
        return "write"
    if not isinstance(func, ast.Attribute) or (root in aliases.values() or root in module_funcs):
        return None
    if isinstance(func.value, ast.Name) and func.value.id == "self":
        return None
    attr = func.attr
    if attr == "open":
        return _open_kind(call, 0)
    if attr == "replace":
        single = len(call.args) == 1 and not call.keywords
        return "write" if single else None
    if attr in WRITE_ATTRS:
        return "write"
    return "read" if attr in READ_ATTRS else None


def _decorator_mark(dec: ast.expr) -> tuple | None:
    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Name):
        return None
    kwargs = {kw.arg: kw.value for kw in dec.keywords}
    if dec.func.id == "permission_repair":
        retry = kwargs.get("retry")
        literal = isinstance(retry, ast.Constant) and isinstance(retry.value, bool)
        if dec.args or set(kwargs) != {"retry"} or not literal:
            return ("invalid",)
        return ("repair", retry.value)
    if dec.func.id == "permission_repair_exempt":
        cat, reason = kwargs.get("category"), kwargs.get("reason")
        ok = (
            not dec.args
            and set(kwargs) == {"category", "reason"}
            and isinstance(cat, ast.Constant)
            and cat.value in CATEGORIES
            and isinstance(reason, ast.Constant)
            and isinstance(reason.value, str)
            and reason.value.strip()
        )
        return ("exempt", cat.value, reason.value) if ok else ("invalid",)
    return None


def _ratchet_raise(call: ast.Call, violations: list[str]) -> None:
    if not isinstance(call.func, ast.Name):
        return
    starred = any(isinstance(arg, ast.Starred) for arg in call.args) or any(
        kw.arg is None for kw in call.keywords
    )
    count = len(call.args) + len(call.keywords)
    if call.func.id == "PermissionError" and (
        starred or len(call.args) != 1 or call.keywords
    ):
        violations.append(f"PermissionError args at line {call.lineno}")
    if call.func.id in OSERROR_NAMES and (starred or count >= 2):
        violations.append(f"{call.func.id} args at line {call.lineno}")


def analyze(source: str) -> Analysis:
    """對一份原始碼跑同一套偵測與規則；結構測試與自我測試共用。"""
    tree = ast.parse(source)
    violations: list[str] = []
    aliases = _aliases(tree, violations)
    module_funcs = {
        n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    units: dict[str, Unit] = {}
    methods: set[str] = set()
    targets = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        for item in cls.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                targets.append(item)
                methods.add(item.name)
    for node in targets:
        unit = Unit(node, marks=[m for d in node.decorator_list if (m := _decorator_mark(d))])
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if _dotted(func, aliases) in EXTERNAL_READ_DELEGATES:
                unit.delegated_io.add("read")
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id == "self":
                    unit.self_calls.add(func.attr)
            elif isinstance(func, ast.Name) and func.id in module_funcs:
                unit.func_calls.add(func.id)
            kind = _call_io(sub, aliases, module_funcs)
            if kind:
                unit.io.add(kind)
        units[node.name] = unit
    for sub in ast.walk(tree):  # 保護拒絕棘輪看整個檔（含模組層級）
        if isinstance(sub, ast.Call):
            _ratchet_raise(sub, violations)

    def mark(name: str) -> tuple | None:
        marks = units[name].marks if name in units else []
        return marks[0] if len(marks) == 1 else None

    def is_path_only(name: str) -> bool:
        return (mark(name) or ("",))[0] == "exempt" and mark(name)[1] == "path_only"

    def callees(name: str) -> set[str]:
        unit = units[name]
        own = {c for c in unit.self_calls if c in methods and not is_path_only(c)}
        return own | {f for f in unit.func_calls if f in units}

    io: dict[str, set[str]] = {
        name: unit.io | unit.delegated_io for name, unit in units.items()
    }
    changed = True
    while changed:
        changed = False
        for name in units:
            merged = io[name].union(*(io[c] for c in callees(name)))
            if merged != io[name]:
                io[name], changed = merged, True

    detected = {n for n in methods if not n.startswith("_") and io[n]}
    marks = {n: units[n].marks[0] for n in units if units[n].marks}
    for name in sorted(detected | set(marks)):
        unit, found = units[name], units[name].marks
        if name in detected and len(found) != 1:
            violations.append(f"classification count {len(found)}: {name}")
        if any(m[0] == "invalid" for m in found):
            violations.append(f"invalid decorator: {name}")
        current = mark(name)
        if current is None:
            continue
        if current[0] == "repair" and isinstance(unit.node, ast.AsyncFunctionDef):
            violations.append(f"async permission_repair: {name}")
        if current[:2] == ("exempt", "path_only") and "write" in io[name]:
            violations.append(f"path_only writes: {name}")
        if current[:2] == ("exempt", "delegates"):
            calls_ok = all((mark(c) or ("",))[0] == "repair" for c in unit.self_calls)
            if unit.io or unit.func_calls or not calls_ok:
                violations.append(f"delegates misuse: {name}")
        if current == ("repair", True):
            for callee in sorted(callees(name)):
                if io[callee] and mark(callee) != ("repair", True):
                    violations.append(f"retry=True calls unsafe: {name} -> {callee}")
    return Analysis(detected, marks, violations)


@pytest.fixture(scope="module")
def resource_manager_analysis() -> Analysis:
    return analyze(RM_FILE.read_text(encoding="utf-8"))


def test_resource_manager_follows_every_rule(resource_manager_analysis):
    assert resource_manager_analysis.violations == []


def test_detected_methods_match_the_pinned_lists(resource_manager_analysis):
    analysis = resource_manager_analysis
    assert analysis.detected == set(EXPECTED_RETRY) | set(EXPECTED_EXEMPT)
    assert set(analysis.marks) == analysis.detected
    retry = {n: m[1] for n, m in analysis.marks.items() if m[0] == "repair"}
    assert retry == EXPECTED_RETRY
    exempt = {n: m[1] for n, m in analysis.marks.items() if m[0] == "exempt"}
    assert exempt == {n: cat for n, (cat, _) in EXPECTED_EXEMPT.items()}
    for name, (_, summary) in EXPECTED_EXEMPT.items():
        assert summary in analysis.marks[name][2], name


def test_detection_positive_control(resource_manager_analysis):
    """偵測壞掉時（例如什麼都抓不到）這一條會紅。"""
    assert {"read_file_text", "write_file_text", "append_file_text"} <= (
        resource_manager_analysis.detected
    )


# --- 自我測試：對合成原始碼跑同一個 analyze ---

HEADER = (
    "import errno\nimport os\nimport shutil\nfrom pathlib import Path\n"
    "from src.utils.permission_repair import permission_repair, permission_repair_exempt\n"
)


def _src(body: str) -> str:
    return HEADER + body


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "class R:\n    async def query(self, identity):\n"
            "        return await self._ui_conversation_reader.query_ui_conversations(identity)\n",
            "classification count 0: query",
        ),
        (
            "class R:\n    def save(self, p, b):\n        Path(p).write_bytes(b)\n",
            "classification count 0: save",
        ),
        ("class R:\n    def swap(self, x, y):\n        x.replace(y)\n", "count 0: swap"),
        (
            "class R:\n"
            "    @permission_repair(retry=True)\n"
            "    def read(self, p):\n        return Path(p).read_text()\n"
            "    def wrapper(self, p):\n        return self.read(p)\n",
            "count 0: wrapper",
        ),
        (
            "class R:\n    def _helper(self, p):\n        return open(p).read()\n"
            "    def go(self, p):\n        return self._helper(p)\n",
            "count 0: go",
        ),
        (
            "from os import replace\n"
            "class R:\n    def mv(self, a, b):\n        replace(a, b)\n",
            "from-import: os",
        ),
        (
            "from os import replace\n"
            "class R:\n    def mv(self, a, b):\n        replace(a, b)\n",
            "count 0: mv",
        ),
        (
            "from os.path import exists\n"
            "class R:\n    def has(self, a):\n        return exists(a)\n",
            "from-import: os.path",
        ),
        (
            "def _wipe(p):\n    shutil.rmtree(p)\n"
            "class R:\n    def clear(self, p):\n        _wipe(p)\n",
            "count 0: clear",
        ),
        ("import shutil as sh\nclass R:\n    pass\n", "alias import: shutil as sh"),
        ("import os.path as osp\nclass R:\n    pass\n", "alias import: os.path as osp"),
        (
            "import shutil as sh\nclass R:\n    def rm(self, p):\n        sh.rmtree(p)\n",
            "count 0: rm",
        ),
        (
            "class R:\n"
            "    @permission_repair_exempt(category=\"path_only\", reason=\"r\")\n"
            "    def where(self, p):\n        Path(p).mkdir()\n        return p\n",
            "path_only writes: where",
        ),
        (
            "class R:\n"
            "    @permission_repair_exempt(category=\"delegates\", reason=\"r\")\n"
            "    def load(self, p):\n        return open(p).read()\n",
            "delegates misuse: load",
        ),
        (
            "class R:\n"
            "    @permission_repair(retry=False)\n"
            "    def append(self, p):\n        Path(p).open(\"a\").write(\"x\")\n"
            "    @permission_repair(retry=True)\n"
            "    def twice(self, p):\n        self.append(p)\n",
            "retry=True calls unsafe: twice -> append",
        ),
        (
            "RETRY = True\nclass R:\n"
            "    @permission_repair(retry=RETRY)\n"
            "    def read(self, p):\n        return Path(p).read_text()\n",
            "invalid decorator: read",
        ),
        (
            "class R:\n"
            "    @permission_repair_exempt(category=\"path_only\", reason=\" \")\n"
            "    def where(self, p):\n        return Path(p).resolve()\n",
            "invalid decorator: where",
        ),
        (
            "class R:\n"
            "    @permission_repair_exempt(category=\"anything\", reason=\"r\")\n"
            "    def where(self, p):\n        return Path(p).resolve()\n",
            "invalid decorator: where",
        ),
        (
            "class R:\n"
            "    @permission_repair(retry=True)\n"
            "    async def read(self, p):\n        return Path(p).read_text()\n",
            "async permission_repair: read",
        ),
        (
            "class R:\n"
            "    @permission_repair(retry=True)\n"
            "    @permission_repair_exempt(category=\"path_only\", reason=\"r\")\n"
            "    def read(self, p):\n        return Path(p).read_text()\n",
            "classification count 2: read",
        ),
        (
            "class R:\n    def deny(self):\n"
            "        raise PermissionError(errno.EACCES, \"x\")\n",
            "PermissionError args",
        ),
        (
            "class R:\n    def deny(self):\n"
            "        raise PermissionError(\"x\", filename=\"p\")\n",
            "PermissionError args",
        ),
        (
            "class R:\n    def deny(self):\n"
            "        raise OSError(errno.EACCES, \"x\", \"p\")\n",
            "OSError args",
        ),
        (
            "class R:\n    def deny(self):\n        raise IOError(errno.EACCES, \"x\")\n",
            "IOError args",
        ),
        (
            "class R:\n    def deny(self):\n"
            "        raise EnvironmentError(errno.EACCES, \"x\")\n",
            "EnvironmentError args",
        ),
    ],
)
def test_self_test_catches_the_reverse_cases(body, expected):
    violations = analyze(_src(body)).violations
    assert any(expected in v for v in violations), violations


@pytest.mark.parametrize(
    "body",
    [
        (
            "class R:\n"
            "    @permission_repair_exempt(category=\"delegates\", reason=\"reader\")\n"
            "    async def query(self, identity):\n"
            "        return await self._ui_conversation_reader.query_ui_conversations(identity)\n"
        ),
        "class R:\n    def label(self, p):\n        return \"a\".replace(\"b\", \"c\")\n",
        "class R:\n    def child(self, p):\n        return Path(p) / \"x\"\n",
        (
            "class R:\n"
            "    @permission_repair_exempt(category=\"swap_semantics\", reason=\"swap\")\n"
            "    def mv(self, a, b):\n        Path(a).replace(b)\n        Path(a).chmod(0)\n"
        ),
        (
            "class R:\n"
            "    @permission_repair_exempt(category=\"path_only\", reason=\"r\")\n"
            "    def where(self, p):\n        return Path(p).resolve()\n"
            "    @permission_repair(retry=True)\n"
            "    def read(self, p):\n        self.where(p)\n        return Path(p).read_text()\n"
            "    def plain(self, p):\n        return self.where(p)\n"
        ),
        "class R:\n    def deny(self):\n        raise PermissionError(\"Soul Protection Error\")\n",
    ],
)
def test_self_test_accepts_the_positive_cases(body):
    assert analyze(_src(body)).violations == []


def test_self_test_detection_of_plain_methods():
    analysis = analyze(
        _src(
            "class R:\n"
            "    def label(self):\n        return \"a\".replace(\"b\", \"c\")\n"
            "    def plain(self, p):\n        return self.where(p)\n"
            "    @permission_repair_exempt(category=\"path_only\", reason=\"r\")\n"
            "    def where(self, p):\n        return Path(p).resolve()\n"
        )
    )
    assert analysis.detected == {"where"}
