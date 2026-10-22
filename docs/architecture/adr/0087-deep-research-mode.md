# ADR-0087: Deep Research 模式（Skill 打包多步研究＋引用報告）

**狀態 (Status):** ✅ 已接受
**日期 (Date):** 2026-06-13
**決策者 (Deciders):** 專案擁有者 + 主程

## 上下文 (Context)

對 claude.ai／ChatGPT／Gemini 網頁版做競品功能盤點（2026 中、web 查證）後確認：三家皆有「Deep Research」（多步搜尋→閱讀→產出帶引用的長報告），ToukaChat 雖有 `research` 工具（Tavily/Google）卻無打包的研究 UX。鑑於 Touka 本就是**自主代理平台**（ReAct 迴圈＋Skills＋artifacts），此功能與定位高度契合，故補上。維護者決定採**逐則切換鈕**觸發（非整段對話模式、非自動召喚）。

## 決策方案 (Decision)

**Deep Research ＝ 一個打包的 Skill（`deep-research-coordinator`）＋ 一個 `deep_research` 請求旗標**，由**既有 ReAct 迴圈驅動**，不新建編排服務、不動自主預算 SSOT。

### 後端
- **Skill**：`config/skills/deep-research-coordinator/SKILL.md`（is_system，標準 frontmatter）。body 即「編排」：釐清（必要時 `ask_user`）→拆 3–6 子問題→**同一輪並行發多個 `research()` 呼叫**（`tool_executor` 本就並行執行，故無需提高 slots）→交叉比對→產出繁中結構化報告含「## 參考來源」編號連結；嚴禁杜撰來源。
- **旗標管線**（鏡像 incognito/ADR-0086）：`adapters/schemas.py` 兩請求模型 + `models/context.py` `PipelineContext` 加 `deep_research: bool=False`；`routers/proxy.py` 兩 UI 端點在 `if identity:` 區塊設 `ctx["deep_research"]`。
- **SkillLayer 強制注入**：`ctx["deep_research"]` 為真時，將 `deep-research-coordinator` 加入 `authorized_skill_ids`（提示詞層）。**RBAC 安全論證**：`skill_required_tools` 從不授予工具（僅供收集 MCP），實際 `research` 工具仍受 `permission_service.get_allowed_tools(identity)` 把關——故強制注入提示詞**無越權**（Base 角色無 research 工具時 agent 自然無從搜尋、優雅退化）。

### 前端
- `deepResearch` per-session 旗標貫穿 7 點鏈（`Session` 型別→`useChatStream` 出口→`StartStreamParams`→`StreamRegistry`→`connections` body `deep_research`），完全比照 incognito。**釐清（P4/[ADR-0161](0161-inputbar-repair-p4.md)）**：此處「per-session」僅指旗標的**記憶體內作用域**（當前 session 物件），語意仍是**逐則**——切走／重整歸零、不跨 session 持久化（正是本 ADR「顯式切換避免忘了關→之後每則靜默觸發昂貴研究」的初衷）。原 `ChatArea` toggle 內的 `syncSession` 呼叫實為死碼（後端 `SessionModel` 無此欄、POST 被 Pydantic 靜默丟棄從未落盤），P4 已移除。
- `ChatArea` 工具列加「🔬 研究」**逐則切換鈕**（active 著色、`aria-pressed`、設計 token 合規）；經 `useSessionList` 更新 `currentSession.deepResearch`（記憶體暫態）。

### 重用（免造輪子，AGENTS.md 守則）
research 工具、ReAct/`tool_executor` 並行執行、Skills 機制（ADR-0066）、mode 旗標管線（ADR-0081/0086）、進度/工具步驟串流（工具步驟追蹤器已逐筆顯示每次 `research` 呼叫＝「研究中」訊號）、ArtifactView 的 MarkdownRenderer（報告內嵌引用）。

## 替代方案（已評估、未採）
- **決定性編排服務**：更可控但重造輪子、違背 Skill 慣例；若品質不足再升級（Skill 可改委派創建引擎式流水線）。
- **提高自主預算**：碰 ADR-0012 自主 SSOT（敏感、預算飢餓風險）；因單回合並行 fan-out 已足，**v1 不動預算**。
- **ADR-0067 自動召喚**：深入研究較貴，改**顯式切換**避免靜默成本，可預期。

## 後果與影響 (Consequences)

- 新增一個 system Skill＋一個 request 旗標＋前端切換鈕；**不新增端點/錯誤碼**（沿用既有對話端點，Guard #24 不受影響）；`openapi.json`＋`api.ts` 重生（新 schema 欄位，Guard #17）。
- **無痕一致性**：無痕下深入研究仍可跑，但 ADR-0086 既有閘門使其不讀寫記憶、不落盤（提示詞會引導但記憶工具未注入）。
- **v1 限制（記錄不做）**：內嵌引用報告（無結構化 `sources[]`/`CitationsPanel`、無報告存檔下載）、不動預算、無專屬 `research_sources` 進度條（工具步驟追蹤器已涵蓋）；皆列 Tier-2。

## Action Items
1. [x] `deep-research-coordinator` SKILL.md（多步研究＋引用報告 + ask_user 收斂）。
2. [x] `deep_research` 旗標貫穿（schema×2 / context / proxy×2）＋ SkillLayer 強制注入。
3. [x] 前端 7 點 threading（Session/useChatStream/StreamRegistry/connections body）＋ ChatArea 切換鈕。
4. [x] TDD：後端 `test_deep_research.py`（SkillLayer 注入/RBAC bypass/off/schema/context 共 5）＋ 前端 `connections.deepResearch.test.ts`（body threading 3）。
5. [x] `openapi.json`＋`api.ts` 重生；ADR-0087＋`adr/README` 索引；`tests/README`／`README_HANDOVER`／`CHANGELOG` 同步；guard 31/31＋全量測試綠。
