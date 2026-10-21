"""權限服務的最小安全 fallback 資料。

本模組刻意只依賴標準庫型別，讓 Guard #32 可直接 import 並掃描，不需載入
PermissionService、Pydantic、ResourceManager 或任何應用程式單例。

執行期由 ``permission_service`` 將本資料驗證成 ``PermissionsConfig``。fallback
只保障系統擁有者的修復通道；其餘角色全部 fail-closed。
"""

from __future__ import annotations

from typing import Final

DEFAULT_PERMISSIONS_CONFIG: Final[dict[str, object]] = {
    "roles": {
        "Owner": {
            "desc": "Superuser recovery fallback",
            "allowed_tools": ["*"],
            "allowed_mcp_servers": ["*"],
            "allowed_skills": ["*"],
        },
        "Base": {"desc": "Closed fallback"},
        "Subagent": {"desc": "Closed fallback"},
        "Debugger": {"desc": "Closed fallback"},
    },
    "system_config": {"default_role": "Base"},
    "agent_mappings": {},
    "mcp_servers": [],
}
