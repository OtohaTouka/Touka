# ADR-0034: Endpoint Auth Dependency Guard — 端點認證守衛與技術債清零

**狀態**: ✅ 已接受（時點性內容修訂：GET /terminal/stream「永久豁免」同期 InfoSec 修復已改真認證（安全方向）；文件所列偵測規則 #2〔函式體 guard call〕從未實作＝dead documentation。端點認證守衛核心不變）
**日期**: 2026-05-20
**關鍵字**: 端點認證, FastAPI Depends, AST 掃描, Grandfather List, SOLID-SRP, 安全守衛

---

## 背景與問題

在完成 C1~N12 弱點修復（ADR-0033）後，系統仍存在一類系統性問題：
**未受保護的端點（Missing Depends）**。這類問題屬於「授權遺漏（Authorization Omission）」，不是
程式碼 bug，而是端點在建立時未遵守「每個狀態變更端點必須有 Auth Depends」的設計原則所累積的技術債。

靜態程式分析顯示，在全部路由器中存在 **52 個端點**無任何 `Depends(get_verified_identity)` 或等效認證守衛。
這些端點可在不需任何憑證的情況下被呼叫，違反最小授權原則。

---

## 決策

建立第 24 道守衛：**Endpoint Auth Dependency Guard**，包含：

### 1. AST 掃描器 (`scripts/guard_endpoint_auth.py`)

- 靜態解析所有 `ToukaProxy/src/routers/` 下的 FastAPI route handler
- 識別認證關鍵字集合（`get_verified_identity`、`get_current_user`、`_require_`、`verify_token`、`verify_schedule_token`、`verify_heartbeat`、`verify_origin` 等）
- 對照 **Allowlist**（公開端點）與 **Grandfather List**（已知技術債，設限期清零）
- 不在 Allowlist 且不在 Grandfather List 的未認證端點 → 守衛失敗

### 2. Allowlist (`scripts/endpoint_auth_allowlist.py`)

公開端點白名單，永久豁免：
- `GET /` (health check)
- `GET /v1/heartbeat/test` (connectivity probe)
- `GET /terminal/stream` (SSE read-only broadcast)
- `GET /v1/system_logs` (observability alias)
- 等（共 ~20 個合法公開端點）

### 3. Grandfather List (`scripts/endpoint_auth_grandfather.py`)

**技術債追蹤清單**，目標為 0。初始 52 個技術債項分三個批次清零：

| 批次 | 路由器 | 補修端點數 | 新增守衛 |
|------|--------|------------|---------|
| 批次一 | `system`, `identity`, `permissions`, `scheduler`, `autonomy`, `skills`, `souls` | 22 | `_require_admin_role`, `_require_authenticated` |
| 批次二 | `bots`, `custom_models`, `mcp`, `ui_sessions`, `projects`, `settings` | 18 | `_require_bots_role`, `_require_models_role`, `_require_mcp_role`, `_require_authenticated` |
| 批次三 | `editor`, `models`, `terminal`, `stats`, `group_settings` | 12 | `_require_editor_role`, `_require_models_mgmt`, 修復 None bypass |

清零後 `GRANDFATHER: list[dict] = []`，Grandfather List 永久維持空清單。

### 4. SOLID-SRP Heartbeat Token (`verify_heartbeat_token`)

作為 Guard #24 配套工作，`/heartbeat/awaken` 端點的 token 驗證邏輯從 `awaken_agent()` body
移入獨立的 `verify_heartbeat_token(request, req) -> str` Depends 函式：

```python
def verify_heartbeat_token(
    request: Request,           # X-Heartbeat-Token header
    req: HeartbeatAwakenRequest,  # body token fallback
) -> str:
    """SOLID-SRP SSOT: 所有 token 驗證邏輯集中此處。"""
    service_token = request.headers.get("X-Heartbeat-Token") or req.token
    if not service_token:
        raise ToukaException(ErrorCode.HEARTBEAT_INVALID_TOKEN)
    if HEARTBEAT_TOKEN and service_token == HEARTBEAT_TOKEN:
        return service_token
    try:
        payload = jwt.decode(service_token, AuthSecretManager.get_secret(), algorithms=["HS256"])
        if payload.get("type") != "service" or payload.get("sub") != "heartbeat":
            raise ToukaException(ErrorCode.HEARTBEAT_INVALID_TOKEN)
    except ToukaException:
        raise
    except Exception:
        raise ToukaException(ErrorCode.HEARTBEAT_INVALID_TOKEN) from None
    return service_token
```

`awaken_agent` 簽名接收 `_svc_token: str = Depends(verify_heartbeat_token)`，body 中不再包含任何 `HEARTBEAT_TOKEN`、`jwt.decode` 或 `HEARTBEAT_INVALID_TOKEN` 的參考。由 `test_awaken_agent_body_no_longer_contains_inline_validation()` 測試靜態確認。

---

## 認證守衛識別規則

AST 掃描時，以下任一條件視為「端點已認證」：

1. 函式簽名 Depends 中包含任何 `_AUTH_KEYWORDS` 之一
2. 函式體中含有對應守衛函式的呼叫（如 `verify_origin()`）
3. 端點路徑＋方法在 Allowlist 中明確豁免

---

## 對齊三大開發原則

| 原則 | 落實方式 |
|------|---------|
| **TDD** | 每個批次均先建立 `test_batch*_router_auth.py` Red 測試，再補修端點，最後 Green |
| **SOLID-SRP** | `verify_heartbeat_token()` 抽出為獨立 Depends；每個路由器各自定義 `_require_*_role()` 本地守衛 |
| **SSOT** | Allowlist / Grandfather List 為唯一豁免事實來源；`verify_heartbeat_token` 為 heartbeat token 驗證的唯一實作點 |

---

## 驗證機制

1. **`scripts/guard_all.py` 第 24 道關卡** — Guard #24 PASS
2. **`pytest tests/unit/test_batch1_router_auth.py`** — 批次一端點認證覆蓋測試
3. **`pytest tests/unit/test_batch2_router_auth.py`** — 批次二端點認證覆蓋測試
4. **`pytest tests/unit/test_batch3_router_auth.py`** — 批次三端點認證覆蓋測試
5. **`pytest tests/unit/test_heartbeat_token_depends.py`** — 11 個 TDD 測試覆蓋 `verify_heartbeat_token` 所有情境（reject/accept 各 case + SOLID-SRP 靜態確認）
6. **`pytest tests/`** — 555+ 測試全綠

---

## 影響與後果

- ✅ 守衛持續監控：任何新端點若未設定認證 Depends，Guard #24 將在 CI 時攔截
- ✅ Grandfather List 清零後，技術債為 0，不可再新增 grandfather 項（清單長度靜態斷言）
- ✅ `verify_heartbeat_token` 可獨立單元測試，不依賴 FastAPI TestClient
- ⚠️ 新建路由器時，開發者必須為每個非公開端點明確加入認證 Depends，否則 guard_all.py CI 失敗
