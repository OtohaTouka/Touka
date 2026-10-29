"""ADR-0110：使用者自助 GDPR 完整資料匯出（建構加密 ZIP）。

收集呼叫者本人的個資（脫敏白名單）＋ 其自有 agent 的對話、事實記憶與個人硬碟檔案，
組成明文 ZIP 後即時 AES-256 加密（沿用 ADR-0108 的 ``encrypt_backup_zip``）。
**純唯讀**——完全不修改任何既有資料（含 agent / soul），符合常駐約束。

排除：帳號機密（密碼雜湊／所有 token/JTI）、向量嵌入（衍生、不可攜）、全域系統設定。

IOPrison：僅用 ``tempfile.mkstemp`` / ``os.close`` / ``zipfile``（非禁令清單），明文暫存
清理走 ResourceManager（SSOT）。時間走 ``get_current_epoch``（TIME_SSOT），非 datetime.now。
個人硬碟走區內方法、不跟隨連結（ADR-0293）：走訪與寫入在同一個迴圈，一次開一個檔案。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memory_server.utils.no_follow import LinkRefusedError, WalkReport, zip_open_file
from starlette.concurrency import run_in_threadpool

from src.models.identity import AgentIdentity
from src.services.avatar_service import find_avatar
from src.services.backup_crypto import encrypt_backup_zip
from src.services.resource_manager import resource_manager
from src.services.tool_errors import is_tool_error
from src.tools.builtin.memory import memory_tools
from src.utils.async_db import session_service
from src.utils.continuity_events_db import list_agent_events
from src.utils.time_utils import format_epoch_to_taipei, get_current_epoch

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from memory_server.utils.zone_paths import ZonePath

    from src.models.user import User

logger = logging.getLogger(__name__)

# 明文建構 + 加密為 CPU／暫存磁碟密集；限制同時匯出數，避免（並發/誤觸）執行緒與暫存暴衝
# （比照 ADR-0108 下載端點的信號量）。
EXPORT_ENCRYPT_SEMAPHORE = asyncio.Semaphore(2)

SCHEMA_VERSION = 1

# 個人硬碟檔案大小上限（複審：加密時逐條目讀入記憶體 → 上限 bound 峰值，防 OOM DoS）。
# 寬鬆預設，避免截斷正常匯出；超限者略過並列入 manifest（不靜默丟棄）。
_MAX_FILE_BYTES = 100 * 1024 * 1024  # 單檔 100 MiB
_MAX_TOTAL_FILE_BYTES = 1024 * 1024 * 1024  # 個人硬碟總量 1 GiB

# zip arcname 片段淨化用：只留安全字元，中和 ../ 與絕對/磁碟前綴（防消費端 zip-slip）。
_UNSAFE_SEG_RE = re.compile(r"[^A-Za-z0-9_.\- ]")


def _safe_seg(raw: str) -> str:
    """把 agent_id 淨化成安全的 zip arcname 片段（複審：未驗證 agent_id → 消費端 zip-slip）。"""
    seg = _UNSAFE_SEG_RE.sub("_", raw).replace("..", "_")
    return seg or "_"


def _sanitize_user(user: User) -> dict[str, Any]:
    """脫敏**白名單**：只含可攜個資。明確排除密碼雜湊／所有 token/JTI／is_temp_password。

    用顯式 allowlist（而非黑名單）→ 將來 User 新增欄位不會意外洩漏（防禦縱深）。
    """
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "bio": user.bio,
        "status": user.status,
        "created_at": user.created_at,
        "last_active_at": user.last_active_at,
        "owned_agents": list(user.owned_agents),
    }


async def _fetch_agent_facts(identity: AgentIdentity) -> dict[str, Any] | None:
    """經記憶閘道唯讀匯出該 agent 全部事實；記憶系統不可用時回 None（優雅降級）。"""
    raw = await memory_tools(action="export_facts", identity=identity)
    # 閘道錯誤是結構化 JSON（error_class 開頭），也能被 json.loads 解開，必須先擋下
    if not isinstance(raw, str) or raw.startswith("Error") or is_tool_error(raw):
        logger.warning("GDPR export: facts unavailable for %s", identity.agent_id)
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("GDPR export: facts JSON parse failed for %s", identity.agent_id)
        return None


async def _fetch_agent_timeline(identity: AgentIdentity) -> list[dict[str, Any]] | None:
    """該 agent 的活動時間軸（只有時間與識別碼）；讀不到回 None（列進 skipped）。"""
    try:
        return await list_agent_events(identity)
    except Exception:
        logger.warning("GDPR export: activity timeline unavailable for %s", identity.agent_id)
        return None


_DRIVE_LABEL = "files"


@dataclass
class _Export:
    """收集到的匯出內容；個人硬碟寫 zip 時才走訪，manifest 最後才寫（含略過數）。"""

    text_entries: dict[str, str]
    file_entries: list[tuple[str, Path]]  # 頭像（不在代理人資料夾裡）
    drives: list[tuple[str, ZonePath]]  # (zip 裡的 agent 片段, 個人硬碟)
    manifest: dict[str, Any]
    state: dict[str, Any]  # 大小上限的累計與略過清單（manifest 的 skipped 是同一個 list）


def _skip_drive(seg: str, reason: str, state: dict[str, Any]) -> None:
    logger.warning("GDPR export: personal drive skipped (%s)", reason)
    state["skipped"].append({"agent": seg, "vault": _DRIVE_LABEL, "reason": reason})


def _drive_to_export(identity: AgentIdentity, state: dict[str, Any]) -> ZonePath | None:
    """自有 agent 的個人硬碟；不在回 None.

    根是連結（含上層是連結）照舊記 ``root_escapes_app_data``。
    """
    drive = resource_manager.agent_drive(identity)
    if drive is None:
        return None
    try:
        kind = resource_manager.zone_kind(drive)
    except LinkRefusedError:
        kind = "link"
    if kind == "link":
        _skip_drive(_safe_seg(identity.agent_id), "root_escapes_app_data", state)
        return None
    return drive if kind == "dir" else None


def _add_drive_files(
    zf: zipfile.ZipFile, seg: str, drive: ZonePath, state: dict[str, Any]
) -> None:
    """把個人硬碟的檔案一次一個寫進 zip（不跟隨連結；ADR-0293）。

    複審加固（防越權外洩 / DoS）：
    - **連結不打包**：走訪不進入、不開連結，略過數記入 ``state['skipped']``。
    - **單檔 + 累計大小上限**：依開好的檔案的 ``fstat``；超限者略過並記入（不靜默丟棄）。
    """
    report = WalkReport()
    try:
        with contextlib.closing(resource_manager.zone_walk_open(drive, report)) as walk:
            for parts, handle, info in walk:
                name, size = parts[-1], info.st_size
                if size > _MAX_FILE_BYTES:
                    state["skipped"].append(
                        {"file": name, "reason": "file_too_large", "bytes": size}
                    )
                    continue
                if state["total"] + size > _MAX_TOTAL_FILE_BYTES:
                    state["skipped"].append({"file": name, "reason": "total_budget_exceeded"})
                    continue
                state["total"] += size
                zip_open_file(zf, f"agents/{seg}/{_DRIVE_LABEL}/{'/'.join(parts)}", handle, info)
    except LinkRefusedError:  # 收集之後根被換成連結
        _skip_drive(seg, "root_escapes_app_data", state)
    for reason, count in (
        ("symlink_escape", report.links),
        ("bad_name", report.bad_names),
        ("unreadable", len(report.errors)),
    ):
        if count:
            state["skipped"].append(
                {"agent": seg, "vault": _DRIVE_LABEL, "reason": reason, "count": count}
            )


def _readme(user: User, taipei_time: str) -> str:
    """匯出包內的人類可讀說明（GDPR 知情）。"""
    return (
        "Touka — 個人資料匯出 / Personal Data Export\n"
        "================================================\n\n"
        f"使用者 User: {user.email} ({user.id})\n"
        f"匯出時間 Exported: {taipei_time} (Asia/Taipei)\n\n"
        "本壓縮檔為您在 Touka 系統中的個人資料副本（GDPR 資料可攜權）。\n"
        "This archive is a copy of your personal data in Touka (GDPR data portability).\n\n"
        "內容 Contents:\n"
        "  - profile.json        你的帳號基本資料（已移除密碼與權杖）\n"
        "  - conversations.json  你名下 agent 的對話記錄\n"
        "  - projects.json       你擁有的專案設定\n"
        "  - agents/<id>/facts.json   該 agent 的事實記憶\n"
        "  - agents/<id>/activity_timeline.json  該 agent 的活動時間軸（只有時間與識別碼）\n"
        "  - agents/<id>/files/...    該 agent 的個人硬碟檔案\n"
        "  - avatar.*            你的頭像（若有）\n"
        "  - manifest.json       本次匯出的清單與統計\n\n"
        "未包含 Not included: 帳號密碼/權杖（機密）、向量嵌入（衍生資料）、全域系統設定。\n"
        "Agent 的人格設定（soul/system-prompt）屬系統配置，不在個資匯出範圍。\n"
    )


async def _collect(
    user: User,
    *,
    facts_fn: Callable[[AgentIdentity], Awaitable[dict[str, Any] | None]],
) -> _Export:
    """收集匯出內容（個人硬碟只記下要走訪哪些）。純讀取，不修改任何資料。"""
    owned = list(user.owned_agents)
    owned_set = set(owned)
    text_entries: dict[str, str] = {}
    file_entries: list[tuple[str, Path]] = []
    drives: list[tuple[str, ZonePath]] = []
    state: dict[str, Any] = {"total": 0, "skipped": []}  # 大小/逃逸略過追蹤（複審）

    # 1) profile（脫敏白名單）
    text_entries["profile.json"] = json.dumps(
        _sanitize_user(user), ensure_ascii=False, indent=2
    )

    # 2) 頭像檔（若有）
    avatar = find_avatar(user.id)
    if avatar is not None:
        path, _media = avatar
        file_entries.append((f"avatar{path.suffix.lower()}", path))

    # 3) 對話（本人自有 agent 的 UI sessions，∪ 本人「擁有」的裸模型對話）
    #    範圍化雙判準（ADR-0110 × ADR-0132）：agent_id∈owned_agents（自有 agent 對話），
    #    OR owner_user_id==user.id（與裸模型如 gemini-2.5-flash 直聊、agent_id 非 owned agent，
    #    ADR-0132 加的 session 人類歸屬）。owner_user_id 為 server-assigned INSERT-only
    #    （ui_sessions 不可由 client 設定），且嚴格比對本人 id，故 additive 不擴大越權面、
    #    杜絕他人對話外洩。與下方 projects 匯出（owner_user_id==user.id）同構。
    sessions = await session_service.get_all_sessions()
    mine = [
        s for s in sessions
        if s.get("agent_id") in owned_set or s.get("owner_user_id") == user.id
    ]
    text_entries["conversations.json"] = json.dumps(
        mine, ensure_ascii=False, indent=2, default=str
    )

    # 4) 專案（僅本人擁有）
    projects = await session_service.get_all_projects()
    my_projects = [p for p in projects if p.get("owner_user_id") == user.id]
    text_entries["projects.json"] = json.dumps(
        my_projects, ensure_ascii=False, indent=2, default=str
    )

    # 5) 每個自有 agent：事實記憶 + 個人硬碟檔案
    facts_count = events_count = 0
    for agent_id in owned:
        identity = AgentIdentity(agent_id=agent_id, is_human=False)
        facts = await facts_fn(identity)
        if facts is not None:
            text_entries[f"agents/{_safe_seg(agent_id)}/facts.json"] = json.dumps(
                facts, ensure_ascii=False, indent=2
            )
            if isinstance(facts, dict):
                facts_count += int(facts.get("count", 0) or 0)
        timeline = await _fetch_agent_timeline(identity)
        if timeline is None:
            state["skipped"].append(
                {"agent": _safe_seg(agent_id), "reason": "activity_timeline_unavailable"}
            )
        else:
            text_entries[f"agents/{_safe_seg(agent_id)}/activity_timeline.json"] = json.dumps(
                {"agent_id": agent_id, "count": len(timeline), "events": timeline},
                ensure_ascii=False,
                indent=2,
            )
            events_count += len(timeline)
        drive = _drive_to_export(identity, state)
        if drive is not None:
            drives.append((_safe_seg(agent_id), drive))

    # 6) manifest + README
    epoch = get_current_epoch()
    taipei_time = format_epoch_to_taipei(epoch)[0]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "export_kind": "gdpr_self_service",
        "exported_at_epoch": epoch,
        "exported_at_taipei": taipei_time,
        "user_id": user.id,
        "email": user.email,
        "owned_agents": owned,
        "counts": {
            "conversations": len(mine),
            "projects": len(my_projects),
            "agents": len(owned),
            "facts": facts_count,
            "activity_events": events_count,
        },
        "included": [
            "profile",
            "avatar",
            "conversations",
            "projects",
            "agent_facts",
            "agent_activity_timeline",
            "agent_personal_drive",
        ],
        "excluded": [
            "password_hash_and_tokens (security)",
            "vector_embeddings (derived, non-portable)",
            "agent_persona_soul_config (system configuration)",
            "global_system_settings (not personal data)",
        ],
        # 略過的個人硬碟檔案（連結／過大／超總量）——明確揭露，不靜默丟棄；寫 zip 時才補齊。
        "skipped": state["skipped"],
    }
    text_entries["README.txt"] = _readme(user, taipei_time)
    return _Export(text_entries, file_entries, drives, manifest, state)


def _write_plaintext_zip(out_path: Path, export: _Export) -> None:
    """把收集到的條目寫入「已知路徑」``out_path``（清理由呼叫端 finally 統一處理）。

    檔案條目由磁碟串流寫入（不整檔讀入記憶體）；個人硬碟一次開一個檔案寫入，manifest 最後寫。
    """
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, text in export.text_entries.items():
            zf.writestr(arcname, text)
        for arcname, src in export.file_entries:
            if Path(src).is_file():
                zf.write(src, arcname)
        for seg, drive in export.drives:
            _add_drive_files(zf, seg, drive, export.state)
        zf.writestr("manifest.json", json.dumps(export.manifest, ensure_ascii=False, indent=2))


async def create_encrypted_export(
    user: User,
    passphrase: str,
    *,
    facts_fn: Callable[[AgentIdentity], Awaitable[dict[str, Any] | None]] | None = None,
) -> Path:
    """收集 → 明文 ZIP → AES-256 加密，回傳加密檔路徑（呼叫端負責串流後刪除）。

    明文暫存路徑在任何 await 前就先建好——故即使收集/加密期間協程被取消（用戶端斷線/逾時），
    finally 仍能刪除、明文永不殘留磁碟（複審：取消時孤兒檔）。收集（含全表掃描）與加密同在
    信號量內，併發受限（複審：重活落在 semaphore 外）。
    """
    fn = facts_fn or _fetch_agent_facts
    fd, name = tempfile.mkstemp(prefix="touka_gdpr_", suffix=".zip")
    os.close(fd)  # 先關 fd；ZipFile 以路徑自行開啟，Windows 上失敗時才能 unlink。
    plain = Path(name)
    try:
        async with EXPORT_ENCRYPT_SEMAPHORE:
            export = await _collect(user, facts_fn=fn)
            await run_in_threadpool(_write_plaintext_zip, plain, export)
            return await run_in_threadpool(encrypt_backup_zip, plain, passphrase)
    finally:
        resource_manager.remove_file(plain)  # 明文暫存即刪（成功/失敗/取消皆然）
