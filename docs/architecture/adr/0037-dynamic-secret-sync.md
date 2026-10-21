# ADR-0037: Dynamic Secret Synchronization — 動態安全金鑰獲取與同步

**狀態**: ✅ 已接受
**日期**: 2026-05-24
**關鍵字**: JWT, Secret Cache, AuthSecretManager, Cache Stalling, Property, SOLID

---

## 背景與問題

原先 `identity_service.py` 中的 `JWTAuthenticationProvider` 作為單例被載入時，會在構造函數（`__init__`）中一次性快取安全金鑰：
```python
def __init__(self) -> None:
    self.secret = AuthSecretManager.get_secret()
```
這導致了嚴重的設計缺陷：如果驗證金鑰在執行期被動態修改，或者在測試環境中呼叫了 `AuthSecretManager.reset_for_test()` 以重置/重新生成金鑰，由於 `self.secret` 被快取寫死，它將無法同步更新。

這會導致後續所有的 Token 解密與簽發（使用舊金鑰驗證新簽發的 Token，或用新金鑰驗證舊簽署的 Token）出現不一致而驗證失敗。

---

## 決策

### 1. 移除 `__init__` 中的金鑰快取
我們廢除了在單例初始化時寫死 `self.secret` 的做法，落實單一事實來源（SSOT）原則。

### 2. 使用 Python `@property` 動態代理
我們在 `JWTAuthenticationProvider` 中引入了 `@property` 裝飾的動態 `secret` 屬性，在每次有 token 需要驗證或簽發而讀取 `self.secret` 時，均動態向 `AuthSecretManager` 獲取最新金鑰：
```python
@property
def secret(self) -> str:
    """動態獲取驗證金鑰，避免快取 stall。"""
    return AuthSecretManager.get_secret()
```

### 3. 優點與原則符合度
- **SOLID (Open/Closed Principle)**：不需修改 `verify_token` 與 `issue_token` 對 `self.secret` 的屬性調用方式，僅透過擴充 property 改變底層行為。
- **SSOT (Single Source of Truth)**：`AuthSecretManager` 成為全系統金鑰唯一的最新事實來源。

---

## 影響

- **動態安全更新**：安全金鑰可以隨時在執行期更新，Token 系統會即時感應並套用，無須重啟容器。
- **測試沙盒隔離**：在 TDD 測試中，呼叫 `AuthSecretManager.reset_for_test()` 後，新舊金鑰能立即在 JWT 驗證器中同步生效，解決了測試污染與快取 stall 問題。
