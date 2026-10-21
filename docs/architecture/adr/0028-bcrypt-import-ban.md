# ADR-0028: BCRYPT_IMPORT_SSOT — bcrypt/passlib 匯入靜態執法

**狀態**: ✅ 已接受  
**日期**: 2026-05-17  
**關鍵字**: 密碼安全, bcrypt, passlib, SHA256, AuthService, SSOT, LaC

---

## 背景與問題

`AGENTS.md` §18 禁令 4 明文規定：「嚴禁繞過 AuthService 使用原生 bcrypt 或 passlib，
必須維持 SHA256 → Bcrypt 預雜湊鏈以相容長密碼。」

**問題根源**：bcrypt 原生存在 72-byte 截斷限制。若開發者直接呼叫 `bcrypt.hashpw(password.encode(), salt)`，
超過 72 字節的密碼將被靜默截斷，導致本應不相符的密碼錯誤通過驗證。
過去曾因此引入長密碼 500 錯誤，已透過 SHA256 預雜湊根治（見歷史演進日誌 2026-05-09）。

**執法缺口**：禁令 4 只有文字規範，`lint_ssot.py` 沒有對應的靜態攔截規則。
`passlib` 已從依賴完全移除，但也缺乏防止重新引入的靜態屏障。

---

## 決策

在 `scripts/lint_ssot.py` 中新增 `BcryptImportRule`（Rule ID: `BCRYPT_IMPORT_SSOT`）：

**禁止目標**：
- `import bcrypt` / `import passlib` 及其所有子模組
- `from bcrypt import ...` / `from passlib.* import ...`

**偵測方式**：AST `check_Import` + `check_ImportFrom` 鉤子，利用現有 `SSOTGuard` 分發機制。

**白名單（檔案級）**：
- `auth_service.py`：系統唯一授權的密碼雜湊實作點

**路徑過濾**：全域掃描（不限 `/src/`），因密碼安全相關違規在任何層級都危險。

**noqa 豁免**：若未來需要在非授權場景使用（如密碼遷移腳本），使用
`# noqa: BCRYPT_IMPORT_SSOT` 精確豁免並登記於 `NOQA_REGISTRY.md`。

---

## 違規處置

初始掃描結果：**零違規**（全系統只有 `auth_service.py` 一處使用 bcrypt，且已正確實作預雜湊鏈）。
規則主要作為**前瞻防護**，防止未來開發者繞過 AuthService。

---

## 正確實作模式（auth_service.py）

```python
def hash_password(self, password: str) -> str:
    pre_hash = hashlib.sha256(password.encode()).hexdigest()  # 解決 72-byte 限制
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(pre_hash.encode(), salt)
    return hashed.decode()

def verify_password(self, plain_password: str, hashed_password: str) -> bool:
    pre_hash = hashlib.sha256(plain_password.encode()).hexdigest()
    return bcrypt.checkpw(pre_hash.encode(), hashed_password.encode())
```

---

## 後果

**正向影響**：
- 密碼安全違規在 CI/CD 中被 `lint_ssot.py` 靜態攔截，在進入代碼庫前即被阻止
- passlib 依賴移除後獲得靜態屏障，防止意外重新引入
- 規則整合至現有 `guard_all.py` 執法鏈

**限制**：
- 白名單基於 filename（不含路徑），若系統出現另一個 `auth_service.py` 需更新白名單
- 不偵測透過 `importlib.import_module("bcrypt")` 的動態匯入（已知靜態分析邊界）
