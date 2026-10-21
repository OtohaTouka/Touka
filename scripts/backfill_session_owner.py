# ruff: noqa: T201
"""一次性運維腳本：把 owner_user_id 為 NULL 的 user_sessions 歸屬給指定使用者。

Session owner attribution（計畫 docs/architecture/proposals/session-owner-attribution-plan.md
Phase 5、擁有者 Q2 裁決＝手動 backfill 而非 runtime 自動認領）：owner_user_id 僅在
session 建立（INSERT）時記錄，既有 legacy rows 為 NULL——單人部署可用本腳本把全部
NULL rows 一次性歸屬給主帳號，使 normal 角色也能看見/管理歷史對話。

安全設計：
- **預設 dry-run**（只統計、不落盤）；加 ``--apply`` 才寫入。
- 只填 NULL（``WHERE owner_user_id IS NULL``）——絕不覆蓋既有歸屬；重跑冪等。
- 僅 Owner 手動執行；不進 runtime、不進 guard_all、不排程。

用法（於倉庫根目錄、3.12 venv）：
    python scripts/backfill_session_owner.py --email admin@example.com                 # dry-run
    python scripts/backfill_session_owner.py --email admin@example.com --apply         # 落盤
    python scripts/backfill_session_owner.py --user-id <uuid> --apply                  # 直接指定 id
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ToukaProxy"))
sys.path.insert(0, str(_ROOT / "ToukaMemory" / "src"))


def resolve_user_id(email: str) -> str | None:
    """由 email 解析 users.json（SSOT）中的 user id；查無回 None。"""
    from src.config import get_config_dir  # noqa: LOCAL_IMPORT_BAN

    users_path = get_config_dir() / "users.json"
    if not users_path.exists():
        return None
    data = json.loads(users_path.read_text(encoding="utf-8"))
    for user in data.get("users", []):
        if user.get("email") == email:
            return user.get("id")
    return None


async def backfill(user_id: str, apply: bool) -> tuple[int, int]:
    """回 (null_count, updated)：NULL 歸屬筆數與實際更新筆數（dry-run 恆 0）。"""
    from src.utils.async_db import get_async_db, init_db_async  # noqa: LOCAL_IMPORT_BAN

    await init_db_async()  # 確保 owner_user_id 欄位遷移已套用
    async with get_async_db() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM user_sessions WHERE owner_user_id IS NULL"
        ) as cur:
            row = await cur.fetchone()
            null_count = int(row[0]) if row else 0

        updated = 0
        if apply and null_count:
            cur = await db.execute(
                "UPDATE user_sessions SET owner_user_id = ? WHERE owner_user_id IS NULL",
                (user_id,),
            )
            await db.commit()
            updated = cur.rowcount or 0
    return null_count, updated


def main() -> int:
    """CLI 入口：解析參數 → dry-run 統計或 --apply 落盤。"""
    parser = argparse.ArgumentParser(
        description="Backfill NULL session owners (dry-run by default)."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--email", help="users.json 中的使用者 email")
    group.add_argument("--user-id", help="直接指定 user id (UUID)")
    parser.add_argument("--apply", action="store_true", help="實際落盤（預設僅 dry-run 統計）")
    args = parser.parse_args()

    user_id = args.user_id or resolve_user_id(args.email)
    if not user_id:
        print(f"[FAIL] 查無使用者：{args.email or args.user_id}")
        return 1

    null_count, updated = asyncio.run(backfill(user_id, apply=args.apply))
    if args.apply:
        print(f"[OK] 已把 {updated} 筆 NULL 歸屬 session 指派給 {user_id}（重跑冪等）。")
    else:
        print(f"[DRY-RUN] 將影響 {null_count} 筆 NULL 歸屬 session → {user_id}；加 --apply 落盤。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
