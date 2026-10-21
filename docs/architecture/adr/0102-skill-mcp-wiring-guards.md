# ADR-0102: 技能 ↔ MCP 接線保障（Guard #37 ＋ 啟動健檢 ＋ 舊 key 遷移）

**狀態**：✅ 已接受
**日期**：2026-06-20
**影響範圍**：ToukaProxy（mcp_hub_service／skill_mcp_wiring／main lifespan／SkillLayer／permissions）＋ scripts（Guard #37）
**關鍵字**：skill, MCP, server key, derive_server_key, curated, Guard #37, 啟動健檢, 遷移, 無聲失敗, CI 盲區, allowed_skills, 建基於 ADR-0096／ADR-0079／ADR-0101

---

## 背景

docgen 計分卡情境（ADR-0101 Batch 4）略過，根因是**「技能宣告的 MCP server key ≠ 實機 registry 註冊的 key」**：`document-generator` 技能 `.mcp.json` 宣告 `mcp-docgen`，但使用者**舊版安裝**把 MCP Hub 條目的 `id`（`curated-mcp-docgen`）當成 server key 註冊（現行 `install()` 已改用 `name`→`mcp-docgen`，但殘留資料未遷移）。三個讓它**沒被事先抓到**的因素：

1. **無聲失敗**：技能連不上 MCP 時不報錯（模型只是「用不到那個工具」）。
2. **CI 盲區**：live `server_config.json` 為 gitignored，CI 全新 checkout 看不到實際註冊的 key。
3. **無遷移**：修 install bug（id→name）時沒清掉既有殘留 key。

（先決修復見 ADR-0101 後續：`Owner` 角色補 `allowed_skills:["*"]`——`can_use_skill` 無 Owner 捷徑，缺它則技能全被 RBAC 擋；SkillLayer 補「技能存在性過濾」——`["*"]` 下擋掉不存在的技能 id。本 ADR 處理「名稱接不上」這層。）

## 決策

四層補強，**各層作用不同（誠實標註哪層真正抓得到此類錯）**：

1. **`derive_server_key` SSOT**（`mcp_hub_service`）：install 的 server key 衍生規則（`name.lower().replace(" ","-")`）抽成單一函式；install／遷移／健檢／Guard #37 皆以此為準。
2. **Guard #37（靜態、CI）** `guard_skill_mcp_consistency.py`：對每個 `config/skills/*/.mcp.json` 宣告的 server，若其 recipe(command+args) 與某 curated `mcp_hub.json(.example)` 條目相同，則宣告 key 必須等於該條目的衍生 key。**預防新碼**設計面不一致；recipe 不符（SSE/url 內部 server）不在範圍。**抓不到 live 殘留資料**（那靠下兩層）。
3. **啟動健檢（live、非 fail-fast）** `validate_skill_mcp_wiring`：對 first-party 技能宣告的 **hub-installable** MCP，檢查是否已在 registry 註冊；缺 → **明確 WARNING**（非擋啟動，MCP 安裝是使用者選項）。比照 **ADR-0096**：live 設定 CI 看不到 → 在啟動真實環境檢查；**不計入 guard 數**，機制由單元測試守。**把無聲失敗變有聲。**
4. **啟動遷移（D1=A）** `migrate_stale_server_keys`：把 `server_config.json` 中「key == 某 curated 條目 `id`」的舊殘留改回衍生 key（`curated-mcp-docgen`→`mcp-docgen`），**冪等、僅在目標 key 不存在時改名**（絕不覆蓋既有/自訂 server）；改後重載 registry。**自動根除既有殘留。**

## 後果

- ✅ 技能 MCP 名不符：**新碼**被 Guard #37 擋、**既有殘留**啟動自動遷移、**未安裝**啟動大聲告警。docgen 命名鏈三道防線齊備。
- ✅ `guard_all` 36 → **37**；啟動健檢不計 guard（live、CI no-op，比照 ADR-0096）。
- ✅ install key 衍生單一來源（`derive_server_key`），install／遷移／健檢／守衛不再各寫一份。
- ⚠️ 遷移會**改動 live `server_config.json`**：以冪等＋「目標 key 不存在才改」＋「不誤改自訂 server」測試把關。
- 範圍：純後端＋守衛；零真實 API；計分卡 docgen 情境連得上（前提：mcp-docgen 已安裝）。

建基於 [ADR-0096](0096-live-config-boot-validation.md)（live 啟動健檢）、[ADR-0079](0079-mcp-docgen-first-party-document-mcp.md)（docgen 整合）、[ADR-0101](0101-scorecard-production-fidelity.md)（計分卡 docgen 情境）。
