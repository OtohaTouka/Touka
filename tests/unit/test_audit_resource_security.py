"""ADR-0283：政策稽核帳本（``stats/decisions.csv``）不記終端指令內容。

- 終端指令那一列只記程式名稱和長度：``[command program=<名稱或?> chars=<數字>]``；
  參數和值一律不記，認不出程式名稱就記 ``?``，不猜。
- 其他列記工具名稱；不像工具名稱的內容只記長度，像金鑰的工具名稱照既有樣式遮掉。
- 只改寫進檔案的那一份：權限判斷照舊看完整指令，``PolicyContext`` 不被改動。
- 只照 shell 的方式切段（ASCII 空白、tab、換行）；設定值、程式段落與工具名稱的字元集逐字元
  固定，有人放寬時這裡會紅。
- 守門：建 ``PolicyContext`` 的位置、action 與 resource 固定在清單裡；其他建法與改 resource／
  action 的寫法一律擋下，新增或改動時這裡會紅。

假金鑰都在執行時組出來，不在原始碼裡留下像金鑰的字面值。
"""

from __future__ import annotations

import ast
import asyncio
import csv
import json
import string
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext, PolicyDecision
from src.services import tool_executor as executor_module
from src.services.approval_service import approval_service
from src.services.audit_service import PolicyAuditService, audit_service
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor
from src.utils import audit_resource
from src.utils.audit_resource import audit_resource_for, summarize_command_for_audit

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "ToukaProxy" / "src"
_ACTOR = AgentIdentity.for_role(agent_id="AuditProbe", role="Owner", is_human=False)
_DRAIN_TIMEOUT_SECONDS = 30

# 像真金鑰的形狀（會被既有遮罩樣式認出）與一般密碼（任何樣式都認不出）。
_KEY = "sk-" + "Zq7" * 14
_PW = "Pw" + "9xQ" * 6


def _fragments(secret: str) -> list[str]:
    return [secret[i : i + 8] for i in range(len(secret) - 7)]


def _assert_secret_free(text: str) -> None:
    for secret in (_KEY, _PW):
        for fragment in _fragments(secret):
            assert fragment not in text


def _summary(program: str, command: str) -> str:
    return f"[command program={program} chars={len(command)}]"


# ── 終端指令摘要 ───────────────────────────────────────────────────────────────

_COMMAND_CASES = [
    (f"curl -H 'Authorization: Bearer {_KEY}' https://api.example.com", "curl"),
    (f"OPENAI_API_KEY={_KEY} python app.py", "python"),
    (f"A=1 B={_PW} node run.js", "node"),
    (f'OPENAI_API_KEY="{_PW}" python app.py', "?"),
    (f"TOKEN=a\\ {_PW} python x.py", "?"),
    (f"TOKEN=$(cat {_PW}) python x.py", "?"),
    (f"TOKEN=$(get {_PW} x) python x.py", "?"),
    (f"TOKEN='abc {_PW} end' python x.py", "?"),
    (f"TOKEN= {_PW} python x.py", "?"),
    (f"TOKEN=a=={_PW} python x.py", "?"),
    (f"1A={_PW} python x.py", "?"),
    (f"A-B={_PW} python x.py", "?"),
    (f"1A={_PW}/tool --x", "?"),
    (f"A=1 B={_PW}", "?"),
    (f'$env:API_KEY="{_PW}"; curl https://example.com', "?"),
    (f"[Environment]::SetEnvironmentVariable('API_KEY', '{_PW}')", "?"),
    (f"set API_KEY={_PW}", "set"),
    (f"setx API_KEY {_PW}", "setx"),
    (f"env API_KEY={_PW} python x.py", "env"),
    (f"sudo -u root mysql -p{_PW}", "sudo"),
    (f"GH_TOKEN={_KEY} C:\\tools\\gh.exe auth login --with-token {_PW}", "gh.exe"),
    (f"/usr/local/bin/psql postgres://u:{_PW}@db/x", "psql"),
    (f'"C:\\Program Files\\Git\\bin\\git.exe" -c http.extraHeader="Basic {_PW}"', "?"),
    (f"& 'C:\\x\\y.exe' --password {_PW}", "?"),
    (f"https://u:{_PW}@example.com/x", "?"),
    ("x" * 64 + f" --password {_PW}", "x" * 64),
    ("x" * 65 + f" --password {_PW}", "?"),
    (f"-p{_PW} x", "?"),
    (f"--password {_PW}", "?"),
    (f"admin:{_PW} x", "?"),
    (f"程式 --password {_PW}", "?"),
    (f"{_KEY} --help", "?"),
    (f"curl\t-u\tuser:{_PW}\thttps://example.com", "curl"),
    (f"\n  curl -u user:{_PW} https://example.com\n", "curl"),
    ("curl\n", "curl"),
    ("", "?"),
    ("   ", "?"),
]


@pytest.mark.parametrize(("command", "program"), _COMMAND_CASES)
def test_command_summary_keeps_only_program_and_length(command: str, program: str):
    summary = summarize_command_for_audit(command)
    assert summary == _summary(program, command)
    _assert_secret_free(summary)


# shell 只在 ASCII 空白、tab、換行切字；str.split() 還會在這些字元切開。
_NON_SHELL_SEPARATORS = [
    chr(code)
    for code in (
        0x0D,
        0x0B,
        0x0C,
        0x1C,
        0x1D,
        0x1E,
        0x1F,
        0x85,
        0xA0,
        0x1680,
        0x2000,
        0x2007,
        0x200A,
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
    )
]


@pytest.mark.parametrize("separator", _NON_SHELL_SEPARATORS)
def test_non_shell_whitespace_never_splits_a_value_into_the_program(separator: str):
    command = f"TOKEN=abc{separator}{_PW} python app.py"
    summary = summarize_command_for_audit(command)
    assert summary == _summary("?", command)
    _assert_secret_free(summary)


# 字元集逐字元固定（獨立寫一份，不從被測模組取）：以後有人放寬，不在集合裡的字元會讓
# 值延伸到下一段，祕密就變成程式名稱或照記的工具名稱。
_PRINTABLE_ASCII = [chr(code) for code in range(0x21, 0x7F)]
_ALNUM = set(string.ascii_letters + string.digits)
_VALUE_PUNCTUATION = set("_./:@%+,-")
_PROGRAM_TOKEN_CHARS = _ALNUM | set("_./:+-") | {chr(92)}
_TOOL_NAME_CHARS = _ALNUM | set("_.:/-")


def _char_id(char: str) -> str:
    return f"U+{ord(char):04X}"


@pytest.mark.parametrize(
    "char",
    [c for c in _PRINTABLE_ASCII if c not in _ALNUM | _VALUE_PUNCTUATION],
    ids=_char_id,
)
def test_assignment_value_stops_at_every_other_character(char: str):
    command = f"TOKEN=a{char}b {_PW} x python"
    summary = summarize_command_for_audit(command)
    assert summary == _summary("?", command)
    _assert_secret_free(summary)


@pytest.mark.parametrize("char", sorted(_VALUE_PUNCTUATION), ids=_char_id)
def test_assignment_value_punctuation_is_skipped(char: str):
    command = f"TOKEN=a{char}b python {_PW}"
    assert summarize_command_for_audit(command) == _summary("python", command)


@pytest.mark.parametrize(
    "char",
    [c for c in _PRINTABLE_ASCII if c not in _PROGRAM_TOKEN_CHARS and c != "="],
    ids=_char_id,
)
def test_program_segment_rejects_every_other_character(char: str):
    # 第二種接路徑：只看最後一段的程式名稱檢查擋不住，要靠整段的字元集。
    for command in (f"a{char}{_PW} x", f"a{char}{_PW}/tool x"):
        summary = summarize_command_for_audit(command)
        assert summary == _summary("?", command)
        _assert_secret_free(summary)


def test_summary_failure_never_lets_the_command_through(monkeypatch: pytest.MonkeyPatch):
    def _boom(_text: str) -> tuple[str, bool]:
        raise RuntimeError(_PW)

    monkeypatch.setattr(audit_resource, "redact_secrets_from_content", _boom)
    summary = summarize_command_for_audit(f"curl --token {_PW}")
    assert summary == "[command unavailable]"


# ── 依動作挑寫法 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool", ["vault_read", "mcp__github__create_issue", "server.tool:v2/list", "x" * 200]
)
def test_tool_names_are_kept(tool: str):
    assert audit_resource_for("tool_call", tool) == tool


def test_key_shaped_tool_name_is_redacted():
    assert audit_resource_for("tool_call", _KEY) == "<redacted>"


@pytest.mark.parametrize(
    "resource",
    [
        f"echo --token {_PW}",
        "x" * 201,
        "",
        f"{_PW}\n",
        f"名稱{_PW}",
        f"key={_PW}",
        f"u@{_PW}",
        f"a?{_PW}",
    ],
)
def test_non_tool_shaped_resource_keeps_only_length(resource: str):
    expected = f"[resource chars={len(resource)}]"
    assert audit_resource_for("schema_injection", resource) == expected


@pytest.mark.parametrize(
    "char", [c for c in _PRINTABLE_ASCII if c not in _TOOL_NAME_CHARS], ids=_char_id
)
def test_tool_name_shape_rejects_every_other_character(char: str):
    resource = f"a{char}{_PW}"
    expected = f"[resource chars={len(resource)}]"
    assert audit_resource_for("tool_call", resource) == expected


def test_terminal_action_uses_the_command_summary():
    command = f"curl --token {_PW}"
    assert audit_resource_for("terminal_execute", command) == _summary("curl", command)


def test_resource_failure_never_lets_the_value_through(monkeypatch: pytest.MonkeyPatch):
    def _boom(_text: str) -> tuple[str, bool]:
        raise RuntimeError(_KEY)

    monkeypatch.setattr(audit_resource, "redact_secrets_from_content", _boom)
    assert audit_resource_for("tool_call", "vault_read") == "[resource unavailable]"
    assert audit_resource_for("terminal_execute", "curl x") == "[command unavailable]"


# ── 真實寫檔路徑 ───────────────────────────────────────────────────────────────


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _drain()
    path = tmp_path / "decisions.csv"
    monkeypatch.setattr(audit_service, "audit_file", path)
    return path


@pytest.fixture(autouse=True)
def _clean_broker():
    approval_service.reset_for_test()
    yield
    approval_service.reset_for_test()


def _drain() -> None:
    PolicyAuditService._executor.submit(int).result(timeout=_DRAIN_TIMEOUT_SECONDS)


def _rows(path: Path) -> list[dict[str, str]]:
    _drain()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _terminal_rows(path: Path) -> list[dict[str, str]]:
    rows = _rows(path)
    for row in rows:
        _assert_secret_free(json.dumps(row, ensure_ascii=False))
    return [row for row in rows if row["action"] == "terminal_execute"]


def test_writer_keeps_the_policy_context_unchanged(ledger: Path):
    command = f"curl --token {_PW}"
    context = PolicyContext(
        actor=_ACTOR,
        resource=command,
        action="terminal_execute",
        environment={"auth_level": "Manual", "session_id": "s-audit"},
    )
    audit_service.log_decision(context, PolicyDecision(effect="CONFIRM", reason="r"))
    rows = _terminal_rows(ledger)
    assert [row["resource"] for row in rows] == [_summary("curl", command)]
    assert context.resource == command


def test_classify_command_ledger_row_has_only_the_summary(ledger: Path):
    command = f"git push https://u:{_PW}@example.com/repo --token {_KEY}"
    permission_service.classify_command(command, _ACTOR, auth_level="Manual", isolation="landlock")
    rows = _terminal_rows(ledger)
    assert [row["resource"] for row in rows] == [_summary("git", command)]


def test_blacklist_still_sees_the_full_command(ledger: Path):
    """黑名單樣式只出現在指令後段；判斷仍看完整原文，帳本只有摘要。"""
    command = f"echo go --token {_PW} ; rm -rf /"
    effect = permission_service.classify_command(
        command, _ACTOR, auth_level="YOLO", isolation="landlock"
    )
    assert effect == "DENY"
    rows = _terminal_rows(ledger)
    assert [(row["resource"], row["effect"]) for row in rows] == [
        (_summary("echo", command), "DENY")
    ]
    assert rows[0]["reason"].startswith("Matched critical blacklist pattern")


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return json.dumps({"ok": True})


@pytest.mark.asyncio
async def test_tool_executor_gate2_ledger_row_has_only_the_summary(
    monkeypatch: pytest.MonkeyPatch, ledger: Path
):
    recorder = _Recorder()
    bindings = dict(executor_module.BUILTIN_EXECUTION_BINDINGS)
    bindings["execute_command"] = replace(bindings["execute_command"], handler=recorder)
    monkeypatch.setattr(executor_module, "BUILTIN_EXECUTION_BINDINGS", bindings)
    executor = ToolExecutor(
        protocol_manager=None,
        tool_registry={"execute_command": recorder},
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )
    command = f"OPENAI_API_KEY={_KEY} echo {_PW} && rm -rf /"
    ctx = {"identity": _ACTOR, "auth_level": "YOLO", "session_id": "s-audit-gate2"}
    result = await executor.execute_to_queue(
        "execute_command", {"command": command}, "req-audit", ctx, asyncio.Queue()
    )
    assert recorder.calls == []
    assert json.loads(result)["error_class"] == "permission_denied"
    rows = _terminal_rows(ledger)
    assert [(row["resource"], row["effect"]) for row in rows] == [
        (_summary("echo", command), "DENY")
    ]
    assert rows[0]["reason"].startswith("Matched critical blacklist pattern")


# ── 守門：PolicyContext 的 resource 從哪裡來 ───────────────────────────────────
# 掃 ToukaProxy/src 全部檔案。以下任何一項變動都會紅，先確認 resource 放的是什麼，
# 再更新清單與稽核寫法。model_copy 與屬性寫入看不出物件型別，所以對全部物件檢查。

_EXPECTED_POLICY_CONTEXTS = sorted(
    [
        ("middleware/layers/tool.py", "'schema_injection'", "tool_name"),
        ("protocols/mcp/oauth/workload/adapters.py", "'tool_call'", "raw_tool_name"),
        ("services/auto_reviewer.py", "'auto_review'", "request.tool_name"),
        (
            "services/permission_service.py",
            "'terminal_execute' if 'command' in args else 'tool_call'",
            "args.get('command', tool_name)",
        ),
        (
            "services/tool_executor.py",
            "'terminal_execute' if is_terminal else 'tool_call'",
            "args.get('command', name) if is_terminal else name",
        ),
    ]
)
# update 不是字面字典的 model_copy：看不出改了哪些欄位，人看過才列進來。
_EXPECTED_OPAQUE_MODEL_COPIES = ["services/interjection_queue.py: item"]
_GUARDED_FIELDS = {"resource", "action"}
_ALTERNATE_CONSTRUCTORS = {"model_validate", "model_validate_json", "model_construct"}


def _policy_context_names(tree: ast.Module) -> set[str]:
    names = {"PolicyContext"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.asname for a in node.names if a.name == "PolicyContext" and a.asname)
    return names


def _is_policy_context(node: ast.expr, names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    return isinstance(node, ast.Attribute) and node.attr == "PolicyContext"


def _keyword_source(node: ast.Call, name: str) -> str:
    value = next((k.value for k in node.keywords if k.arg == name), None)
    return ast.unparse(value) if value is not None else "<none>"


def _scan_call(node: ast.Call, rel: str, names: set[str], found: dict[str, list]) -> None:
    func = node.func
    if _is_policy_context(func, names):
        resource = "<positional>" if node.args else _keyword_source(node, "resource")
        found["sites"].append((rel, _keyword_source(node, "action"), resource))
    elif isinstance(func, ast.Attribute) and func.attr in _ALTERNATE_CONSTRUCTORS:
        if _is_policy_context(func.value, names):
            found["constructors"].append(f"{rel}:{node.lineno}")
    elif isinstance(func, ast.Attribute) and func.attr == "model_copy":
        for keyword in (k for k in node.keywords if k.arg == "update"):
            value = keyword.value
            if not isinstance(value, ast.Dict) or None in value.keys:
                found["opaque"].append(f"{rel}: {ast.unparse(func.value)}")
            elif {k.value for k in value.keys if isinstance(k, ast.Constant)} & _GUARDED_FIELDS:
                found["copies"].append(f"{rel}:{node.lineno}")
    elif isinstance(func, ast.Name) and func.id == "setattr" and len(node.args) >= 2:
        # 欄位名是變數的 setattr（設定檔載入等）靜態看不出寫哪個欄位，這裡不管。
        field = node.args[1]
        if isinstance(field, ast.Constant) and field.value in _GUARDED_FIELDS:
            found["writes"].append(f"{rel}:{node.lineno}")


def _scan_assignment(node: ast.stmt, rel: str, found: dict[str, list]) -> None:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Attribute) and sub.attr in _GUARDED_FIELDS:
                found["writes"].append(f"{rel}:{sub.lineno}")


def _scan_policy_contexts() -> dict[str, list]:
    kinds = ("sites", "constructors", "opaque", "copies", "writes")
    found: dict[str, list] = {kind: [] for kind in kinds}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(_SRC).as_posix()
        names = _policy_context_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                _scan_call(node, rel, names, found)
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                _scan_assignment(node, rel, found)
    return {k: sorted(v) for k, v in found.items()}


def test_policy_context_sites_are_pinned():
    """建構點、其他建法、改 resource／action 的寫法都固定；變動時先看 resource 放了什麼。"""
    found = _scan_policy_contexts()
    assert found["sites"] == _EXPECTED_POLICY_CONTEXTS
    assert found["constructors"] == []
    assert found["opaque"] == _EXPECTED_OPAQUE_MODEL_COPIES
    assert found["copies"] == []
    assert found["writes"] == []
