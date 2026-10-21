# ADR 0015: 環境變數主權 (Environment Variable Sovereignty / Env SSOT)

## 狀態
已接受 (2026-05-06)

## 上下文 (Context)
在本次架構轉向之前，環境變數散落在程式庫各處，透過 `os.environ.get()` 或 `os.getenv()` 進行存取。這造成了幾個關鍵問題：
1. **隱含依賴**：如果不閱讀完整源碼，無法得知模組運作需要哪些環境變數。
2. **型別安全**：環境變數的原始字串需要手動轉換型別，容易導致執行時期的 `TypeError` 或 `ValueError`。
3. **配置漂移 (Configuration Drift)**：不同檔案中的預設值不一致。
4. **Agent 漏洞**：自主 Agent 可以輕易地透過直接讀取或修改環境來繞過系統限制。
5. **靜態分析缺口**：無法透過程式化方式驗證所有配置是否已中心化。

## 決策 (Decision)
我們實作了全面的 **環境變數主權 (Env SSOT Guard)**：

1. **中心化配置**：所有外部環境變數與靜態設定必須映射至 Pydantic `BaseSettings` 模型。
2. **後端 SSOT**：`ToukaProxy/src/config.py` 是後端配置的唯一權威。
3. **前端 SSOT**：`ToukaChat/src/lib/config.ts` 是前端配置的唯一權威。
4. **強制守衛 (The Judge)**：
    - **後端**：`lint_ssot.py` 使用 AST 分析，攔截業務邏輯中所有對 `os.environ`、`os.getenv` 及類似敏感 API 的直接調用。
    - **前端**：已實作 `guard_env.ts` 掃描器，使用 `ts-morph` 攔截 `src/` 下所有對 `process.env` 與 `import.meta.env` 的直接調用。
5. **特權存取**：僅限指定的「引導者 (Bootstrapper)」檔案（`config.py`、`main.py`、`bootstrap.py` (後端) 與 `src/lib/config.ts` (前端)）擁有存取原始環境的白名單權限。
6. **自癒機制**：引導程序會自動從 `.env.example` 與 `config/*.json.example` 復原缺失的配置。

## 後果 (Consequences)
- **正面影響**：系統依賴項 100% 可見。強健的型別驗證與轉換。中心化的預設值。加固了防禦 Agent 未經授權操縱環境的安全防線。
- **負面影響**：需要一次性遷移所有舊有的 `os.getenv` 調用。新增配置項時會增加些許樣板代碼。
- **合規性**：由 `lint_ssot.py` 強制執行。違規將導致靜態分析失敗並拒絕 CI/CD 部署。

## 參考資料
- [SSOT Master Map](../SSOT_MASTER_MAP.md)
- [AGENTS.md](https://github.com/OtohaTouka/Touka/blob/main/AGENTS.md)
