# ruff: noqa: T201
"""端點 Auth 守衛祖父清單 (Endpoint Auth Guard Grandfather List — SSOT).

列出所有「目前尚未有 auth dependency，但屬於既有代碼」的端點。
這些是「技術負債」條目，非「刻意公開」。

守衛規則：
  1. 既有代碼：此清單中的端點允許暫時缺少 auth，守衛不失敗。
  2. 新代碼：不在此清單的端點如果缺少 auth，守衛立即失敗。
  3. 修復路徑：修復端點 auth 後，從此清單移除對應條目（漸進式清理）。

⚠️  嚴禁直接將新端點加入此清單以規避守衛。
    新端點若為刻意公開，應加入 endpoint_auth_allowlist.py 並說明理由。
    新端點若需要 auth，請直接加入 auth dependency。

修復記錄：
  2026-05-20 — Grandfather Batch-1（第一優先）完成：
    已移除：/config, /system, /identity, /agents, /permissions,
             /scheduler, /autonomy, /skills, /souls(update)
  2026-05-20 — Grandfather Batch-2（第二優先）完成：
    已移除：/bots, /ui/custom_models, /ui/sessions, /ui/projects,
             /ui/settings, /mcp, /group_settings(stale)
  2026-05-20 — Grandfather Batch-3（第三優先）完成：
    已移除：/editor/files, /editor/read, /models/refresh, /models(patch/delete),
             /models/{id}/health, /terminal/history, /ui/mission,
             /stats (stale—added None check), /heartbeat/awaken (added Depends keyword)
    移至 allowlist：/terminal/stream (SSE read-only broadcast), /system_logs (observability alias)
  殘餘：無 — Grandfather 清單已清空！
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 技術負債條目（既有代碼，待補 auth，漸進式清除）
# ---------------------------------------------------------------------------

GRANDFATHER: list[dict[str, str]] = [
    # 🎉 所有技術負債條目已在三個批次中清除完畢！
    # 如需新增，請務必附上理由並設定修復目標日期。
]
