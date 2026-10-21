# ADR-0064: ToukaDispatch Mission Token 機制（外部 AI 一次性授權）

## 狀態

✅ 已接受

## 日期

2026-05-30

## 背景

ToukaDispatch 使用全域 `DISPATCH_TOKEN` 作為主要認證機制（ADR-0050）。當 Touka 需要召喚外部 AI 工具（如 Claude Code CLI）協同執行任務時，必須提供某種憑證讓外部 AI 可以連接到 ToukaDispatch。

直接將 `DISPATCH_TOKEN` 傳遞給外部 AI 存在嚴重安全風險：`DISPATCH_TOKEN` 是系統全域主金鑰，一旦外洩，攻擊者可建立任意 session、存取或建立任意 mission board。

## 問題

1. 外部 AI 工具需要某種憑證才能連接 ToukaDispatch
2. 直接傳遞 `DISPATCH_TOKEN` 風險過高（全域主金鑰洩漏）
3. 需要確保外部 AI 只能存取指定 mission board，不能建立新任務或存取無關 mission

## 決策

引入 **mission_token** 機制——一種一次性、任務範圍限制的委派 token：

### 1. Token 層次分離

| Token | 生命週期 | 使用者 | 安全邊界 |
|-------|---------|--------|---------|
| `DISPATCH_TOKEN` | 永久（環境變數） | Touka 內部 Agent | **不得離開 Touka 進程邊界** |
| `mission_token` | 一次性（新 mission 建立時失效） | 外部 AI 工具 | 與 mission_id 一對一綁定 |

### 2. SessionManager 擴充（`session_manager.py`）

`SessionInfo` dataclass 新增兩個欄位：
- `bound_mission_id: str | None = None`：外部 session 被指派的 mission ID
- `is_external: bool = False`：標記此 session 由外部 AI 持有

新增方法：
```python
def create_mission_token(self, mission_id: str) -> str:
    """生成與 mission_id 一對一綁定的一次性 token。"""

def validate_mission_token(self, token: str) -> str | None:
    """驗證 mission_token，回傳 mission_id 或 None。"""

def revoke_mission_token(self, token: str) -> None:
    """撤銷單一 mission_token。"""

def clear_mission_tokens(self) -> None:
    """清除所有 mission_token（新 mission 建立時呼叫）。"""

def create_external_session(self, mission_id: str, caller_info: dict) -> str:
    """建立 is_external=True、bound_mission_id 鎖定的外部 session。"""
```

### 3. dispatch_handshake 雙路徑認證（`server.py`）

```python
# 內部路徑：DISPATCH_TOKEN（主金鑰，不離開 Touka 邊界）
if _verify_dispatch_token(dispatch_token):
    session_token = _session_mgr.create_session(caller_info)
    return f"✅ Handshake successful. Session token: {session_token}"

# 外部路徑：mission_token（一次性，外部 AI 專用）
mission_id = _session_mgr.validate_mission_token(dispatch_token)
if mission_id is not None:
    session_token = _session_mgr.create_external_session(mission_id, caller_info)
    return f"✅ Handshake successful. Session token: {session_token}"
```

### 4. dispatch_initialize_mission 安全限制

- 外部 session（`is_external=True`）呼叫 → 立即拒絕
- 成功建立 mission 後：
  1. 呼叫 `clear_mission_tokens()` 清除舊 mission_token
  2. 呼叫 `create_mission_token(mission.mission_id)` 生成新 token
  3. 回應中同時包含 `Mission ID` 與 `Mission Token`

### 5. dispatch_get_board board 完整性檢查

外部 session 的 `bound_mission_id` 必須與當前 active mission 的 `mission_id` 相符，不符時回傳錯誤，防止外部 AI 存取已被替換的任務板。

## 安全性分析

此模式等同業界標準的臨時憑證委派模式：

| 業界模式 | Touka 對應 |
|---------|----------|
| AWS IAM Role | `DISPATCH_TOKEN`（主金鑰） |
| AWS STS AssumeRole 回傳的 temp credentials | `mission_token` |
| IAM 最小權限原則 | 外部 AI 只能讀/更新指定 mission |
| Credential rotation | `clear_mission_tokens()` 在新 mission 建立時 |

## 後果

### 正面

- `DISPATCH_TOKEN` 完全封閉在 Touka 進程內，不進入任何外部通訊通道
- 外部 AI 只能存取被指派的 mission board，無法建立新任務或存取其他 mission
- mission_token 在新 mission 建立時自動失效
- 外部 AI 即使持有 mission_token，也無法建立 mission 或提升權限

### 負面

- `SessionManager` 複雜度增加（mission_token 字典 + 5 個新方法）
- `dispatch_initialize_mission` 回應格式變更（新增 Mission Token 欄位，調用方需解析）
- 一次只能有一個 active mission_token（`clear_mission_tokens` 清除所有舊 token）

## 測試覆蓋

`tests/unit/test_dispatch_mission_token.py`（9 個 TDD 測試）：

- `create_mission_token` 回傳長度 > 20 的字串且 `validate_mission_token` 可還原 mission_id
- `revoke_mission_token` 後驗證失敗
- `clear_mission_tokens` 一次清除全部
- `create_external_session` 的 `is_external=True`、`bound_mission_id` 正確設定
- `dispatch_handshake` 接受有效 mission_token，拒絕偽造 token
- 外部 session 呼叫 `dispatch_initialize_mission` → 拒絕
- `dispatch_initialize_mission` 成功時回傳 Mission ID + Mission Token

## 相關 ADR

- [ADR-0047](0047-dispatch-session-token-handshake.md) — Session Token Handshake 基礎架構
- [ADR-0050](0050-dispatch-handshake-reinstate-token-param.md) — DISPATCH_TOKEN 認證
- [ADR-0065](0065-touka-dispatch-skills.md) — Dispatch 協調員 Skill 與外部 AI 指南
