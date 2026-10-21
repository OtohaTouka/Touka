"""統一檔案動詞工具（Tier 3，提案 §6/§7；2026-07-03 與擁有者定案）.

檔案家族統一為 9 顆裸動詞：read / list / find / grep / write / edit / insert /
undo / delete——**參數形狀由動詞決定、權限由 scope 決定**；每次調用自包含、
嚴禁狀態機式範圍切換。裸動詞在模型的 RL 訓練分布內（pi coding agent 的啟示），
取代 action-enum god-tools（project_tools / vault_tools / text_editor /
update_my_soul 檔案類動作）。

scope 語義：
  - ``project``：當前專案（project_id 由系統依會話自動注入）→ 委派 project_tools 引擎
    （路徑沙盒＋專案審計事件不變）。
  - ``personal`` / ``legacy`` / ``soul``：個人檔案庫 → 委派 vault_tools 橋（身分錨定＋
    soul 唯讀硬化不變）。
  - **soul 讀統一、寫獨立**：read-family 開放 scope=soul；write-family 的 scope enum
    根本沒有 soul（非法狀態不可表示），寫入請走專屬 ``edit_soul``。

零邏輯重複：本模組是既有引擎的薄轉接層（提案 §9 雙軌——舊工具保留為可執行 shim、
不再廣告；新動詞不複製任何沙盒／審計邏輯）。
"""

from __future__ import annotations

import logging
from copy import deepcopy
from types import MappingProxyType
from typing import Any

from src.services import tool_errors
from src.tools.project_tools import project_tools
from src.tools.vault_tools import incognito_scope_error, vault_tools
from src.utils.auth_mode import INCOGNITO_HIDDEN_VAULTS, incognito_hides_vault

logger = logging.getLogger(__name__)

READ_SCOPES = ("project", "personal", "legacy", "soul")
WRITE_SCOPES = ("project", "personal", "legacy")

# 委派時一律剝除、避免與轉接參數撞名（模型若多給這些鍵不會炸 TypeError）。
_RESERVED_KEYS = frozenset({
    "scope", "action", "vault", "path", "content", "old_str", "new_str",
    "query", "pattern", "max_depth", "offset", "limit", "mode",
    "view_range", "line", "text", "depth",
})

# 行程內 undo 快照（深度 1；重啟即清、不落任何 .bak 檔——零檔案污染）。
# 鍵 =（scope, 錨點, path）；錨點= project_id（scope=project）或 agent_id（vault），
# 避免跨 agent／跨專案互相還原。
_UNDO_SNAPSHOTS: dict[tuple[str, str, str], str] = {}

_READ_HEADER_PREFIX = "--- Read "  # core_read 標頭（見 memory_server core_file_ops）


def reset_undo_for_test() -> None:
    """測試隔離：清空 undo 快照。"""
    _UNDO_SNAPSHOTS.clear()


def _clean(kwargs: dict[str, Any]) -> dict[str, Any]:
    """剝除保留鍵，留下注入脈絡（identity/project_id/agent_id/...）供引擎使用。"""
    return {k: v for k, v in kwargs.items() if k not in _RESERVED_KEYS}


async def _dispatch(
    scope: str, action: str, params: dict[str, Any], kwargs: dict[str, Any]
) -> Any:
    """Scope → 引擎路由：project → project_tools；其餘 → vault_tools（身分錨定）。"""
    if scope == "project":
        return await project_tools(action=action, **params, **kwargs)
    return await vault_tools(vault=scope, action=action, **params, **kwargs)


def _scope_error(
    scope: str, verb: str, *, write: bool, context: dict[str, Any] | None = None
) -> str | None:
    """Scope 驗證：非法值→validation_error；寫入碰 soul、無痕碰 legacy→permission_denied。

    ``context`` 是執行器注入的 kwargs（看裡面的 ``_ctx`` 判斷無痕）。
    """
    if incognito_hides_vault(scope, (context or {}).get("_ctx")):
        return incognito_scope_error(scope)
    if write and scope == "soul":
        return tool_errors.build_tool_error(
            tool_errors.PERMISSION_DENIED,
            f"scope 'soul' is read-only for the unified file verbs; "
            f"'{verb}' cannot modify it.",
            suggestion=(
                "Use edit_soul for soul/config writes; "
                "read(scope=soul) / list(scope=soul) to view."
            ),
            denied_scope="soul",
        )
    allowed = WRITE_SCOPES if write else READ_SCOPES
    if scope not in allowed:
        return tool_errors.build_tool_error(
            tool_errors.VALIDATION_ERROR,
            f"Unknown scope '{scope}' for '{verb}'.",
            suggestion=f"Use one of: {', '.join(allowed)}.",
            param="scope",
        )
    return None


def _undo_key(scope: str, path: str, kwargs: dict[str, Any]) -> tuple[str, str, str]:
    if scope == "project":
        anchor = kwargs.get("project_id") or ""
    else:
        identity = kwargs.get("identity")
        anchor = kwargs.get("agent_id") or getattr(identity, "agent_id", "") or ""
    return (scope, str(anchor), path)


async def _read_raw(scope: str, path: str, kwargs: dict[str, Any]) -> str | None:
    """經引擎讀取並剝除 core_read 標頭；讀取失敗（缺檔／越界）回 None。"""
    result = await _dispatch(scope, "read", {"path": path}, kwargs)
    text = str(result)
    if text.startswith(_READ_HEADER_PREFIX):
        _header, _sep, body = text.partition("\n")
        return body
    return None


def _applied_hunk(content: str, line_1based: int, radius: int = 3) -> str:
    """回傳變更行周邊的帶行號預覽（P2a-2：讓模型免重讀即可驗證變更）。

    以 ``>`` 標記變更行；行號夾在 [1, 行數]；空內容回空字串。
    """
    lines = content.splitlines()
    total = len(lines)
    if total == 0:
        return ""
    center = max(1, min(int(line_1based), total))
    lo = max(1, center - radius)
    hi = min(total, center + radius)
    width = len(str(hi))
    return "\n".join(
        f"{'>' if i == center else ' '}{i:>{width}}| {lines[i - 1]}"
        for i in range(lo, hi + 1)
    )


# --------------------------------------------------------------------------- #
# 讀取家族（scope 含 soul）
# --------------------------------------------------------------------------- #


async def verb_read(
    scope: str,
    path: str,
    view_range: list[int] | None = None,
    **kwargs: Any,
) -> Any:
    """讀取檔案內容；view_range=[start,end]（1-based 含端）映射引擎 offset/limit。"""
    err = _scope_error(scope, "read", write=False, context=kwargs)
    if err:
        return err
    params: dict[str, Any] = {"path": path}
    if isinstance(view_range, (list, tuple)) and len(view_range) == 2:
        start, end = int(view_range[0]), int(view_range[1])
        params["offset"] = max(0, start - 1)
        params["limit"] = max(0, end - start + 1)
    return await _dispatch(scope, "read", params, _clean(kwargs))


async def verb_list(
    scope: str,
    path: str | None = None,
    depth: int | None = None,
    **kwargs: Any,
) -> Any:
    """以樹狀列出目錄內容；depth 映射引擎 max_depth（預設 2）。"""
    err = _scope_error(scope, "list", write=False, context=kwargs)
    if err:
        return err
    params: dict[str, Any] = {}
    if path is not None:
        params["path"] = path
    if depth is not None:
        params["max_depth"] = int(depth)
    return await _dispatch(scope, "list", params, _clean(kwargs))


async def verb_find(scope: str, pattern: str, **kwargs: Any) -> Any:
    """按檔名模式搜尋檔案。"""
    err = _scope_error(scope, "find", write=False, context=kwargs)
    if err:
        return err
    return await _dispatch(scope, "find", {"pattern": pattern}, _clean(kwargs))


async def verb_grep(scope: str, query: str, **kwargs: Any) -> Any:
    """在檔案內容中搜尋關鍵字。"""
    err = _scope_error(scope, "grep", write=False, context=kwargs)
    if err:
        return err
    return await _dispatch(scope, "grep", {"query": query}, _clean(kwargs))


# --------------------------------------------------------------------------- #
# 寫入家族（scope 無 soul——寫靈魂請走 edit_soul）
# --------------------------------------------------------------------------- #


async def verb_write(
    scope: str,
    path: str,
    content: str,
    mode: str = "overwrite",
    **kwargs: Any,
) -> Any:
    """寫入檔案：overwrite 覆寫／append 追加／create_only 僅新建（已存在即拒）。"""
    err = _scope_error(scope, "write", write=True, context=kwargs)
    if err:
        return err
    if mode not in ("overwrite", "append", "create_only"):
        return tool_errors.build_tool_error(
            tool_errors.VALIDATION_ERROR,
            f"Unknown write mode '{mode}'.",
            suggestion="Use one of: overwrite, append, create_only.",
            param="mode",
        )
    clean = _clean(kwargs)
    if mode == "create_only":
        probe = str(await _dispatch(scope, "read", {"path": path}, clean))
        if probe.startswith(_READ_HEADER_PREFIX):
            return tool_errors.build_tool_error(
                tool_errors.VALIDATION_ERROR,
                f"'{path}' already exists; create_only refuses to overwrite.",
                suggestion="Use mode=overwrite to replace it, or edit for in-place changes.",
                param="mode",
            )
        if "File not found" not in probe:
            return probe  # 其他讀取錯誤（越界等）原樣傳遞（已是可讀錯誤）
    action = "append" if mode == "append" else "write"
    return await _dispatch(scope, action, {"path": path, "content": content}, clean)


async def verb_edit(
    scope: str, path: str, old_str: str, new_str: str, **kwargs: Any
) -> Any:
    """精確字串替換（old_str 需唯一匹配）；成功後可用 undo 回復最近一次。"""
    err = _scope_error(scope, "edit", write=True, context=kwargs)
    if err:
        return err
    clean = _clean(kwargs)
    snapshot = await _read_raw(scope, path, clean)
    result = await _dispatch(
        scope, "str_replace", {"path": path, "old_str": old_str, "new_str": new_str}, clean
    )
    if not str(result).startswith("Success"):
        return result  # 失敗（含結構化錯誤）原樣傳遞
    # P2a-2：成功→結構化成功＋變更後片段（省重讀；含 ❌/Error: 內容也不被誤判）。
    preview: str | None = None
    if snapshot is not None:
        _UNDO_SNAPSHOTS[_undo_key(scope, path, kwargs)] = snapshot
        new_content = snapshot.replace(old_str, new_str, 1)
        idx = snapshot.find(old_str)
        change_line = (snapshot[:idx].count("\n") + 1) if idx != -1 else 1
        preview = _applied_hunk(new_content, change_line)
    return tool_errors.build_tool_success(f"Edited {path}", preview=preview)


async def verb_insert(
    scope: str, path: str, line: int, text: str, **kwargs: Any
) -> Any:
    """在指定行插入文字（line=插入後文字所在的 1-based 行號；超界則附加於檔尾）。"""
    err = _scope_error(scope, "insert", write=True, context=kwargs)
    if err:
        return err
    clean = _clean(kwargs)
    raw = await _read_raw(scope, path, clean)
    if raw is None:
        return tool_errors.build_tool_error(
            tool_errors.RESOURCE_NOT_FOUND,
            f"File '{path}' not found in scope '{scope}'.",
            suggestion="Use write(mode=create_only) to create it first.",
            resource_type="file",
        )
    lines = raw.splitlines(keepends=True)
    idx = min(len(lines), max(0, int(line) - 1))
    insert_text = text if text.endswith("\n") else text + "\n"
    lines.insert(idx, insert_text)
    result = await _dispatch(
        scope, "write", {"path": path, "content": "".join(lines)}, clean
    )
    if str(result).startswith("Success"):
        _UNDO_SNAPSHOTS[_undo_key(scope, path, kwargs)] = raw
        # P2a-2：結構化成功＋含插入行的變更後片段。
        preview = _applied_hunk("".join(lines), idx + 1)
        return tool_errors.build_tool_success(
            f"Inserted text at line {idx + 1} of {path}", preview=preview
        )
    return result


async def verb_undo(scope: str, path: str, **kwargs: Any) -> Any:
    """回復同一路徑最近一次 edit/insert（行程內快照、深度 1、重啟即清）。"""
    err = _scope_error(scope, "undo", write=True, context=kwargs)
    if err:
        return err
    key = _undo_key(scope, path, kwargs)
    snapshot = _UNDO_SNAPSHOTS.get(key)
    if snapshot is None:
        return tool_errors.build_tool_error(
            tool_errors.RESOURCE_NOT_FOUND,
            f"No undo history for '{path}' in scope '{scope}' "
            "(undo covers the most recent edit/insert in this process).",
            suggestion="Use read to inspect the current content, then edit to fix it.",
            resource_type="undo_snapshot",
        )
    result = await _dispatch(
        scope, "write", {"path": path, "content": snapshot}, _clean(kwargs)
    )
    if str(result).startswith("Success"):
        del _UNDO_SNAPSHOTS[key]
        return f"Success: Reverted last edit for {path}."
    return result


async def verb_delete(scope: str, path: str, **kwargs: Any) -> Any:
    """刪除檔案。"""
    err = _scope_error(scope, "delete", write=True, context=kwargs)
    if err:
        return err
    return await _dispatch(scope, "delete", {"path": path}, _clean(kwargs))


# --------------------------------------------------------------------------- #
# Schemas（固定形狀、top-level required——不需要 oneOf）
# --------------------------------------------------------------------------- #

_SCOPE_LABELS = {
    "project": "project=當前專案（需已選專案，project_id 與身分由系統自動注入）",
    "personal": "personal=個人空間",
    "legacy": "legacy=遺產紀錄",
    "soul": "soul=靈魂設定（唯讀鏡像）",
}


def _scope_description(scopes: list[str], *, write: bool) -> str:
    text = "操作範圍：" + "；".join(_SCOPE_LABELS[scope] for scope in scopes)
    return text + "。（soul 不可由檔案動詞寫入——請用 edit_soul）" if write else text


def _verb_schema(
    name: str,
    description: str,
    extra_props: dict[str, Any],
    required: list[str],
    *,
    write: bool,
) -> dict[str, Any]:
    scopes = list(WRITE_SCOPES if write else READ_SCOPES)
    scope_prop = {
        "type": "string",
        "enum": scopes,
        "description": _scope_description(scopes, write=write),
    }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"scope": scope_prop, **extra_props},
                "required": required,
            },
        },
    }


FILE_VERB_SCHEMAS: list[dict[str, Any]] = [
    _verb_schema(
        "read",
        "讀取檔案內容。網路資訊請用 research；語意記憶請用 memory_tools。",
        {
            "path": {"type": "string", "description": "相對於該範圍根目錄的路徑"},
            "view_range": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "選填 [start,end]（1-based 含端）只讀某行段",
            },
        },
        ["scope", "path"],
        write=False,
    ),
    _verb_schema(
        "list",
        "以樹狀列出目錄內容。",
        {
            "path": {"type": "string", "description": "起點目錄（省略＝根目錄）"},
            "depth": {"type": "integer", "description": "樹深度（預設 2）"},
        },
        ["scope"],
        write=False,
    ),
    _verb_schema(
        "find",
        "按檔名模式搜尋檔案。",
        {"pattern": {"type": "string", "description": "檔名匹配模式（如 *.py）"}},
        ["scope", "pattern"],
        write=False,
    ),
    _verb_schema(
        "grep",
        "在檔案內容中搜尋關鍵字。",
        {"query": {"type": "string", "description": "搜尋關鍵字"}},
        ["scope", "query"],
        write=False,
    ),
    _verb_schema(
        "write",
        "寫入檔案。mode：overwrite=覆寫、append=追加、create_only=僅新建（已存在即拒絕）。",
        {
            "path": {"type": "string", "description": "相對於該範圍根目錄的路徑"},
            "content": {"type": "string", "description": "要寫入的完整內容"},
            "mode": {
                "type": "string",
                "enum": ["overwrite", "append", "create_only"],
                "default": "overwrite",
                "description": "寫入模式（預設 overwrite）",
            },
        },
        ["scope", "path", "content"],
        write=True,
    ),
    _verb_schema(
        "edit",
        "精確字串替換：old_str 必須在檔中精確且唯一匹配。可用 undo 回復最近一次。",
        {
            "path": {"type": "string", "description": "相對於該範圍根目錄的路徑"},
            "old_str": {"type": "string", "description": "要替換的舊字串（精確且唯一）"},
            "new_str": {"type": "string", "description": "替換後的新字串"},
        },
        ["scope", "path", "old_str", "new_str"],
        write=True,
    ),
    _verb_schema(
        "insert",
        "在指定行插入文字（line=插入後所在的 1-based 行號；超界則附加檔尾）。可用 undo 回復。",
        {
            "path": {"type": "string", "description": "相對於該範圍根目錄的路徑"},
            "line": {"type": "integer", "description": "插入行號（1-based）"},
            "text": {"type": "string", "description": "要插入的文字"},
        },
        ["scope", "path", "line", "text"],
        write=True,
    ),
    _verb_schema(
        "undo",
        "回復同一路徑最近一次 edit/insert（行程內、深度 1）。",
        {"path": {"type": "string", "description": "要回復的檔案路徑"}},
        ["scope", "path"],
        write=True,
    ),
    _verb_schema(
        "delete",
        "刪除檔案。",
        {"path": {"type": "string", "description": "要刪除的檔案路徑"}},
        ["scope", "path"],
        write=True,
    ),
]



def _hide_scopes(schema: dict[str, Any], hidden: frozenset[str]) -> dict[str, Any]:
    """複製一份、拿掉指定範圍（enum 與說明都拿掉）；共用的 schema 物件不動。"""
    projected = deepcopy(schema)
    scope = projected["function"]["parameters"]["properties"]["scope"]
    write = tuple(scope["enum"]) == WRITE_SCOPES
    scope["enum"] = [item for item in scope["enum"] if item not in hidden]
    scope["description"] = _scope_description(scope["enum"], write=write)
    return projected


# 無痕對話列出的版本：沒有 legacy（ToolLayer 換用；hist-bak b，禁令 20）
INCOGNITO_FILE_VERB_SCHEMAS = MappingProxyType(
    {
        schema["function"]["name"]: _hide_scopes(schema, INCOGNITO_HIDDEN_VAULTS)
        for schema in FILE_VERB_SCHEMAS
    }
)

# 註冊：builtin/__init__.py 以「字面鍵」逐一掛進 tool_registry——Guard #32
# （permissions↔registry 防漂移）以 AST 靜態抽取 dict 字面鍵，勿改用 **spread。
