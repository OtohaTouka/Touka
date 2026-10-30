"""docgen 產出檔的零相依驗證（軌B Batch 5：成品正確性維度）。

OOXML（xlsx/docx/pptx）皆為 zip 封裝 → 以 stdlib `zipfile` 驗「格式合法（含必要部件）＋
含指定內容」，**不需** openpyxl/python-docx 等額外相依，且可用 fixture 做零-API 單測。
runner 取得產出路徑後呼叫；不讀 env。產出檔只在文件輸出區（``data/temp/docgen_output``）裡
經 RM 不跟隨地開（ADR-0293 PR 2）：在區外或路上有連結都判失敗，不丟例外。
"""
from __future__ import annotations

import re
import zipfile

from memory_server.utils.no_follow import ZoneLinkRefusedError
from memory_server.utils.zone_paths import Anchor

from src.services.resource_manager import resource_manager

# 各 OOXML 類型的「合法性標記部件」與「內容文字所在前綴」。
_OOXML_SPECS: dict[str, dict[str, str]] = {
    "xlsx": {"marker": "xl/workbook.xml", "text_prefix": "xl/"},
    "docx": {"marker": "word/document.xml", "text_prefix": "word/"},
    "pptx": {"marker": "ppt/presentation.xml", "text_prefix": "ppt/"},
}


def extract_artifact_path(tool_trace: tuple[dict, ...], kind: str) -> str:
    """從工具軌跡找出產出檔路徑（`create_{kind}` 回傳裡的 `*.{kind}`）。找不到回空字串。

    只看 `create_{kind}` 的項目：別的工具（讀檔、搜尋）提到的檔案不算產出檔（ADR-0293 PR 2）。
    軌跡的 result/args 為 JSON-dump 後字串（路徑中的反斜線被轉義為 ``\\\\``），故命中後還原。
    """
    if kind not in _OOXML_SPECS:
        return ""
    pat = re.compile(r"([^\s\"'`,]+\." + re.escape(kind) + r")")
    create = f"create_{kind}"

    def _find(entries: list[dict]) -> str:
        for e in entries:
            m = pat.search(f"{e.get('result', '')} {e.get('args', '')}")
            if m:
                return m.group(1).replace("\\\\", "\\")
        return ""

    return _find([e for e in tool_trace if create in str(e.get("name", ""))])


def verify_artifact(path: str, kind: str, must_contain: tuple[str, ...]) -> tuple[bool, str]:
    """打開產出檔驗「格式合法 ＋ 含指定內容」。回 (是否有效, 說明).

    區外、連結、開檔失敗、不是 zip 都判失敗；其他 zip 解析錯誤照舊往外丟（runner 不接）。
    """
    if not path:
        return False, "未擷取到產出檔路徑（工具或許未回傳路徑）"
    spec = _OOXML_SPECS.get(kind)
    if not spec:
        return False, f"不支援的 artifact 類型：{kind}"
    try:  # 照字面在輸出區裡才讀（不解析連結）；名稱不能當一段也算不在
        zp = resource_manager.zone_rel_from_abs(
            resource_manager.anchor_root(Anchor.DOCGEN_OUTPUT), path
        )
    except ValueError:
        zp = None
    if zp is None:
        return False, "產出檔不在文件輸出區"
    try:
        handle, _ = resource_manager.zone_open_read(zp)
        with handle, zipfile.ZipFile(handle) as zf:  # ZipFile 不會關收到的檔案，Windows 沒關會鎖住
            names = zf.namelist()
            if spec["marker"] not in names:
                return False, f"非合法 {kind}（缺必要部件 {spec['marker']}）"
            blob_parts = [
                zf.read(n).decode("utf-8", "replace")
                for n in names
                if n.startswith(spec["text_prefix"]) and n.endswith(".xml")
            ]
    except ZoneLinkRefusedError:
        return False, "產出檔是連結，不讀"
    except FileNotFoundError:
        return False, f"產出檔不存在：{path[:120]}"
    except zipfile.BadZipFile:
        return False, f"非合法 {kind}（不是 OOXML zip 封裝）"
    except OSError:  # 例外文字可能帶路徑，回固定文字
        return False, "產出檔開檔失敗"
    blob = "\n".join(blob_parts)
    missing = [s for s in must_contain if s not in blob]
    if missing:
        return False, f"合法 {kind} 但缺內容：{missing}"
    return True, f"合法 {kind}，含全部預期內容（{len(must_contain)} 項）"
