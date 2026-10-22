# ADR-0243：Touka 重新運作前的小任務（全停 Gemini 顯式快取、KF-6、.env.example、Next.js 升級、Docker uv、禁令 63）

- 狀態：✅ 已接受（維護者 2026-10-12 核准模型層總計畫）
- 日期：2026-10-12
- 決策者：維護者
- 影響範圍：`gemini_adapter`（刪 `gemini_cache_service`）、模型健康檢查（`routers/models.py`）、根 `.env.example`、ToukaChat `next`／`eslint-config-next` 與鎖檔、Proxy 與 Heartbeat Dockerfile 的 uv 映像、AGENTS.md 禁令 63、對應測試與文件
- 消除：[ADR-0242](0242-model-layer-stage-1a.md) 的 KF-1、KF-2、KF-2d、KF-6
- 已知限制中的 Guard #20 knip 在工作資料夾假紅，已由 [ADR-0250](0250-touka-leftovers.md) K1 修正（2026-10-14：`guard:prune` 加 `--no-gitignore`，產生檔與建置產物改用 knip `project` 排除）

## 背景

Touka 重新運作前，先清掉 1a 凍結出來、或擋住檢查的小問題：Gemini 顯式快取在迴圈重建時會丟掉較早的對話，還會和 `tool_config`、`system_instruction`／`tools` 一起送（KF-1、KF-2、KF-2d；維護者在 1a 已決定全停）；Claude 與 OpenAI 的健康檢查請求不合廠商要求（KF-6）；根 `.env.example` 沒有列出廠商金鑰；Guard #23 擋下 `next@16.3.6`；Docker 映像的 uv 能不能讀冷卻期設定一直沒確認（ADR-0241 已知限制）。另外，曾有一次臨時查驗在沙盒外載入專案程式，初始化時自動寫出設定檔，差點出事，寫成禁令 63。

## 決策

1. **全停 Gemini 顯式快取**：本決策取代 [ADR-0241](0241-model-layer-stage-0.md) 決策 4（只在無痕時不建）。刪除 `services/gemini_cache_service.py`；`gemini_adapter` 拿掉初次建快取與迴圈中重建，只服務快取的 `_State` 欄位（`is_force_tool`、`preserved_system_instruction`、`preserved_tools`、`cache_allowed`）一併刪除，ADR-0155 C3 的保存值也隨之消失。一般與無痕對話、初次與迴圈中，每一輪都照常送 `system_instruction`、`tools`、`tool_config`，不再送 `cachedContent`，也不再連 `cachedContents`。
   - 保留：`extract_usage` 仍把 `usageMetadata.cachedContentTokenCount` 對映成快取讀取量。這是 Google 端自動進行的隱式快取的回報數字，Touka 沒有建立任何東西，只是讀回應；拿掉會讓遙測與 1b 的用量正規化（要從 `promptTokenCount` 扣掉快取 token）少了資料。
2. **KF-6 健康檢查**：Claude 健康檢查帶 `anthropic-version: 2023-06-01`；`owned_by == "openai"` 的健康檢查改送 `max_completion_tokens`，不再送 `max_tokens`。兩個釘子測試照 docstring 改成新期望，並改名為 `test_claude_health_check_sends_anthropic_version`、`test_openai_health_check_sends_max_completion_tokens`。
3. **根 `.env.example` 列出廠商金鑰**：`ANTHROPIC_API_KEY`、`GEMINI_API_KEY`、`GROK_API_KEY`、`OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`TAVILY_API_KEY`，值一律留空、各附一行說明（整合模式只讀根 `.env`，禁令 38；填佔位字串會被當成真金鑰送出）。防漂移：`test_root_env_example_provider_keys.py` 要求範本涵蓋 `guard_provider_parity.backend_required_key_names()` 的全部金鑰名且值為空，後端新增廠商時範本必須一起補。順帶修好設定頁：`GET /config/env` 的 definitions 只從 live `.env` 旁的 `.env.example` 產生，所以補上之後「API 金鑰」分頁會列出這六把金鑰，不再一律顯示未設定（已設定的回遮罩、未設定的回空字串，`test_config_env_root_example_keys.py` 以沙盒內的合成 `.env` 釘住）。
4. **Next.js 16.3.6 → 16.3.8**：`next`、`eslint-config-next` 精確釘版只換值。鎖檔以 `npm install --package-lock-only --ignore-scripts --before=2026-10-05` 重生，只變 12 個 Next 套件（`next`、`@next/env`、`@next/eslint-plugin-next`、`eslint-config-next`、8 個 `@next/swc-*`），全部 2026-09-30 上架、已滿 7 天，integrity 與 registry 相同；`sharp` 的上游範圍仍是 `^0.35.4`。修正 `next@16.3.6` 的 6 個 advisory：GHSA-cjq9-62q9-8jv4（high，Image Optimization SSRF）、GHSA-3w37-wq28-93x7、GHSA-4jqv-mc3x-m676、GHSA-f87g-xv8r-7p7x、GHSA-mcj8-r9mp-w47p（moderate）、GHSA-39w2-rjm5-chcv（low）。三個安全測試只換版本值，不放寬。
5. **Docker 映像的 uv 換成 0.12.1**：兩個 Dockerfile 與 `test_p8b_supply_chain_gate.py` 的 `UV_CHILD` 同時換成 linux/amd64 子 manifest `sha256:f6edac8c6d516a01a5387a8655c97481e0db46dd0a62ab62f41e5b2294376f6e`，和 CI 的 setup-uv 同版。
   - 舊映像 `bbfa230b…` 是 uv 0.10.5。反例證明它認得 `exclude-newer` 期間與 `uv.lock` 的 `exclude-newer-span`：亂填會警告或解析失敗，改錯欄位名則被默默忽略。
   - 但映像是 distroless（只有 `/uv`，沒有 Python 也沒有 shell），`lock --locked --offline` 在找 Python 時就停下，Docker build 實際跑的 `--locked` 比對無法在容器內離線判定，所以照計畫換版。
   - 0.12.1 的容器同樣停在找 Python；同版本在主機以真 Python 跑 `lock --locked --offline` 通過，把冷卻期改成 8 天的反例會失敗，證明比對確實看冷卻期。
6. **禁令 63（臨時查驗沙盒限定）**：臨時查驗不得在測試沙盒外載入專案程式，載入時的初始化也算寫入；需要看值或驗證行為時，改寫成沙盒內的測試。只追加於 AGENTS.md 禁令清單最後（任務前閱讀表的禁令範圍同步為 1–63）。

## 代價與剩餘風險

- Gemini 長對話每一輪都送完整的 system 與工具，沒有顯式快取的折扣，費用可能比以前高；Google 的隱式快取可能自動命中，但不保證。
- 全停只代表 Touka 不在 Google 端另建 1 小時副本；Google 仍可能依其政策保留請求。
- 健康檢查與 Gemini 的新請求只用 mock 驗證，廠商是否接受留到下一段實測（禁令 61）。
- uv 0.12.1 在 Python 基底映像裡實際跑 `uv sync --locked`，要等下一段的完整 `docker build` 才算確認。
- 禁令 63 沒有工具能偵測，靠開發者遵守。

## 已知限制

- Guard #20 的 knip 在 `.toukadev/worktrees/`（維護流程放 git worktree 的位置）底下的工作資料夾會假紅：根 `.gitignore` 有 `.toukadev/`，knip 依 gitignore 濾掉絕對路徑含 `.toukadev` 的入口檔，因此誤報 `backport.mjs`、`build-artifact-runtime.mjs` 沒被使用；加 `--no-gitignore` 即通過。屬環境問題，守衛未改；這類工作資料夾的 knip 改在路徑不含 `.toukadev` 的複本驗證。（2026-10-14 已由 ADR-0250 K1 修正，工作資料夾裡直接跑即可）
- Guard #23 只擋 high 以上；Chat／IDE 仍有少數 moderate advisory（如 fast-uri），隨上游相容修正版更新。

## 驗證

- Gemini：新測試 `test_gemini_no_explicit_cache.py` 9 個案例（一般、無痕、未帶旗標 × 初次、長迴圈、帶 `tool_config` 的輪次）；把 `cachedContent` 塞回 payload 等突變都會變紅。`ToukaProxy/src` 裡的 `cachedContent` 只剩用量計數。goldens 沒有任何變動。
- 健康檢查與 `.env.example`：2＋2 個測試，各突變都會變紅。
- Next.js：升級前 Guard #23 因 `next` 的 high advisory 擋下（同套件共 6 個 advisory），升級後 Chat 與 IDE 都通過；Vitest 1,494、build 通過。
- uv：p8b 與 `test_docker_build_inputs.py` 113 passed，把任一 Dockerfile 換回舊 digest 都會變紅；Guard #16、#30 通過。
- 全量 pytest 與守衛結果見 HANDOVER。本段沒有打任何 API，也沒有啟動 Touka。

## 回復

不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。各項彼此獨立，逐項 revert 產品程式、測試與對應文件；本 ADR 改「⛔ 已撤回」（或註明撤回哪一項），CHANGELOG 加一列。撤回快取＝連同 ADR-0241 決策 4 的無痕行為一起回來。撤回 Next.js 會讓 Guard #23 再次擋下，不建議。撤回 uv＝兩個 Dockerfile 與 `UV_CHILD` 換回舊 digest。禁令 63 要刪須經維護者同意，編號照禁令 54、62 留墓碑。
