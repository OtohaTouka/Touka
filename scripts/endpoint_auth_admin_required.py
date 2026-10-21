"""端點 Admin 等級註冊表 (Endpoint Admin-Level Registry — SSOT).

列出所有「必須 admin 級守衛」的端點。guard_endpoint_auth.py 以此做**等級辨識**
（早期 ADR 守衛缺口 E05/E06/E07 家族④治本）：

- 舊守衛只驗「有認證 Depends」——``_AUTH_KEYWORDS`` 把 ``get_verified_identity``
  視為足夠，故 ``require_admin → get_verified_identity`` 的**降級**全綠通過
  （實證：ADR-0035 sliding_window、ADR-0053 setup/embedding 皆如此）。
- 本註冊表與守衛做**雙向同步**：
  1. 表列端點必須存在且帶 admin 級 Depends（含 ``require_admin`` 子串——涵蓋
     ``require_admin``／``_require_admin``／``_require_admin_only``／
     ``_require_admin_role`` 四種現行拼法）→ 降級或改名未更新即 FAIL。
  2. 帶 admin 級 Depends 的端點必須列於本表 → 新增 admin 端點須同步登記，
     防清單靜默漂移（同 NOQA registry 模式）。

每個條目格式：{"method": "<http verb>", "path": "<router prefix + route path>"}
（**精確匹配**，非 prefix——admin 等級不該被前綴吞掉）。

注意：owner 級守衛（``_require_owner_role``，skills.py/feedback.py）屬 agent 角色
平面、不在本表範圍。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 必須 admin 級的端點（方法 + 精確路徑）
# ---------------------------------------------------------------------------

ADMIN_REQUIRED: list[dict[str, str]] = [
    # ── MCP OAuth 非秘密 profile 治理（human admin only）──────────────────
    {"method": "get", "path": "/admin/mcp/oauth/profiles"},
    {"method": "put", "path": "/admin/mcp/oauth/profiles/{profile_id}"},
    {"method": "delete", "path": "/admin/mcp/oauth/profiles/{profile_id}"},
    {"method": "get", "path": "/admin/mcp/oauth/keys/rotation"},
    {"method": "post", "path": "/admin/mcp/oauth/keys/rotation"},
    # ── MCP workload OAuth profile/credential/recovery（ADR-0207）───────────
    {"method": "get", "path": "/admin/mcp/oauth/workload/profiles"},
    {"method": "put", "path": "/admin/mcp/oauth/workload/profiles/{profile_id}"},
    {"method": "delete", "path": "/admin/mcp/oauth/workload/profiles/{profile_id}"},
    {"method": "get", "path": "/admin/mcp/oauth/workload/profiles/{profile_id}/credential"},
    {"method": "put", "path": "/admin/mcp/oauth/workload/profiles/{profile_id}/credential"},
    {"method": "post", "path": "/admin/mcp/oauth/workload/profiles/{profile_id}/credential/revoke"},
    {"method": "post", "path": "/admin/mcp/oauth/workload/profiles/{profile_id}/metadata/refresh"},
    {"method": "get", "path": "/admin/mcp/oauth/workload/recovery"},
    {"method": "post", "path": "/admin/mcp/oauth/workload/recovery/retry"},
    {
        "method": "post",
        "path": "/admin/mcp/oauth/workload/recovery/transitions/{operation_id}/reconcile",
    },
    {"method": "get", "path": "/admin/mcp/oauth/workload/keys/rotation"},
    {"method": "post", "path": "/admin/mcp/oauth/workload/keys/rotation"},
    # ── 使用者管理（admin.py）────────────────────────────────────────────────
    {"method": "get", "path": "/admin/users"},
    {"method": "get", "path": "/admin/users/{user_id}"},
    {"method": "post", "path": "/admin/users"},
    {"method": "patch", "path": "/admin/users/{user_id}"},
    {"method": "delete", "path": "/admin/users/{user_id}"},
    {"method": "post", "path": "/admin/agents/{target}/reset"},

    # ── 身分映射／權限矩陣（identity.py／permissions.py）───────────────────
    {"method": "post", "path": "/identity"},
    {"method": "get", "path": "/permissions"},
    {"method": "post", "path": "/permissions"},

    # ── 首次設定精靈與嵌入模型（setup.py，ADR-0053/0118；E07）──────────────
    {"method": "get", "path": "/admin/setup/status"},
    {"method": "post", "path": "/admin/setup/apply"},
    {"method": "post", "path": "/admin/setup/embedding/download"},
    {"method": "get", "path": "/admin/setup/embedding/download-status"},
    {"method": "post", "path": "/admin/setup/embedding/switch"},

    # ── 系統運維：備份／設定快照／滑動視窗（system.py；E06）─────────────────
    {"method": "get", "path": "/system/backups"},
    {"method": "post", "path": "/system/backup/run"},
    {"method": "get", "path": "/system/backups/stats"},
    {"method": "get", "path": "/system/restore/status"},
    {"method": "delete", "path": "/system/backups/{filename}"},
    {"method": "post", "path": "/system/backups/{filename}/download"},
    {"method": "get", "path": "/system/backup/schedule"},
    {"method": "post", "path": "/system/backup/schedule"},
    {"method": "post", "path": "/system/settings/export"},
    {"method": "post", "path": "/system/sliding_window"},

    # ── 工作區設定（ui_sessions.py）─────────────────────────────────────────
    {"method": "post", "path": "/ui/settings/workspace"},
]
