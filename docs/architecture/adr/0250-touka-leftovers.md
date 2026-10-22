# ADR-0250：Touka 待排小任務（主要模型兩欄、模型名大小寫、規則漏洞與介面清理）

- 狀態：✅ 已接受（維護者 2026-10-14 核准計畫 v2，含第 1 項「主要模型兩欄」與第 17 項「knip 不靠 .gitignore」；U0～U6 全部完成，驗證見文末）
- 日期：2026-10-14
- 決策者：維護者
- 影響範圍：`routers/souls.py`、`utils/brain_persistence.py`、`services/dispatchers/route_family.py`、`services/models/{manager,routes}.py`、`middleware/layers/soul.py`、`services/dispatchers/group.py`、`routers/heartbeat.py`、`tools/subagent_tools.py`、`routers/custom_models.py`、Gemini／OpenAI 相容 adapter 與 `adapters/gemini_to_openai.py`、`adapters/schemas.py`、`services/process_registry.py`、`tools/terminal.py`、`services/review/evidence.py`、`tools/builtin/research.py`、`services/scheduler_service.py`、ToukaDispatch `server.py`、`scripts/update_openapi.py`／`update_docs.py`／新 `openapi_merge.py`、`config/errors.json.example`；ToukaChat（SoulStudio、ModelStudio、ModelsTab、`app/create`、ChatProvider、ModelProvider、新 `lib/default_model.ts`、兩語 locales、`package.json`、`knip.config.ts`）；ToukaIDE 行內補全與 `package.json`
- 修訂 [ADR-0241](0241-model-layer-stage-0.md) 決策 6：靈魂頁存檔設主要模型的條件改為下面 A1 的寫入規則
- 修訂 [ADR-0111](0111-native-model-default-sampling-params.md) 決策 D4：ModelsTab 的取樣參數編輯面板拿掉（A2）；`ModelInfo` 的參數欄位、能力旗標與 `models.json` 存放值保留
- 修正 [ADR-0243](0243-model-pre-run.md) 的已知限制：Guard #20 knip 在工作資料夾假紅（K1）；[ADR-0244](0244-model-layer-stage-1b.md) 的剩餘風險：Gemini 思考 token 與 DeepSeek 快取命中未讀（A6、A7）；[ADR-0247](0247-model-layer-stage-3a.md) 的範圍外項目：`AnthropicThinking` 定義兩次（C6），以及剩餘風險：ToukaDispatch 的 httpx client 沒明寫 `follow_redirects=False`、不在禁令 22 守衛範圍（B3）。這幾項是補已知缺口、沒有推翻原決策，所以寫修正、不寫修訂；三份 ADR 的狀態區都已加回指註記
- 修正（[ADR-0254](0254-iso-c1-small-fixes-and-execution-gaps.md)）：本 ADR 的已知限制「建立靈魂不驗模型名」「自訂模型只差大小寫改名照樣回 200」「ModelStudio 下拉可能混入 human」「Proxy 排程 CRUD 與 `ENABLE_SCHEDULER`」「`custom_model_service.py` 寫死 Gemini」已修，B4 保留的排程 CRUD 已刪

## 背景

模型溝通層各段留下一批小問題：靈魂頁只有一個「主要模型」欄位，分不出「人指定的」和「模型自己換過的」，存人格會把模型自己換的蓋回去；沒設主要模型時清單回顯示字串、群聊與心跳會當掉；自訂連線的大寫模型名查不到；計費漏算 Gemini 思考 token、DeepSeek 快取命中；幾條規則有漏洞（沙盒外載入、轉址旗標、走不到的排程引擎）；介面上還有不會送出的取樣參數拉桿與寫死的模型名。各項彼此獨立，可逐項撤回。

## 決策

### A1 主要模型兩欄（修訂 ADR-0241 決策 6）

- 「你指定的」＝靈魂 `config.json` 的 `base_model`；「目前在用的」＝`brain_settings` 的 active brain，模型可以自己換（`switch_brain`）。三種模式（模型自己調、system 1 路由、使用者調）之後再定，屆時「你指定的」就是退回的預設值。
- `POST /v1/souls/{id}` 的順序：權限檢查（不變）→ 寫入前讀出已存的 `base_model` 與 active brain → 判斷 → 需要時驗證 → 驗證通過才寫 config 與人格 → 需要時設主要模型。
- 新值的 `None`、空字串、只有空白都當「沒指定」，存成空值。
- 判斷是純函式 `brain_persistence.should_set_active_brain(*, is_human, old, new, has_brain)`，下列條件全部成立才換：
  1. 請求者是人類（只讀已驗證的 `AgentIdentity.is_human`，admin 經 X-Agent-ID 代打不算）；
  2. 新值有指定；
  3. 新值和舊值不同，或目前沒有主要模型（保留 p0 的救援）；
  4. 新值通過 `route_family.is_selectable_brain`，不通過回 404 `MODEL_NOT_FOUND`，config 與人格都不寫（不默默略過）。

| # | 請求者 | 新值 | 和舊值 | 目前主要模型 | 結果 |
|---|---|---|---|---|---|
| 1 | 人類 | 可選 | 不同 | 有 | 寫設定與人格，換主要模型 |
| 2 | 人類 | 可選 | 相同 | 有（可能是模型自己換的） | 寫設定與人格，不動主要模型 |
| 3 | 人類 | 可選 | 相同 | 無 | 寫設定與人格，補設主要模型 |
| 4 | 人類 | 不可選 | 不同 | 任意 | 404 `MODEL_NOT_FOUND`，都不寫 |
| 5 | 人類 | 不可選 | 相同 | 無 | 404 `MODEL_NOT_FOUND`，都不寫 |
| 6 | 人類 | 不可選 | 相同 | 有 | 照存、不驗、不動主要模型 |
| 7 | 人類 | 沒指定 | — | 任意 | 照存（`base_model` 存空），不動主要模型 |
| 8 | 代理人 | 任意 | 任意 | 任意 | 照存、不驗、不動主要模型 |
| 9 | 人類 | 只差大小寫的兩個都在、寫法都不同 | 不同 | 任意 | 409 `MODEL_NAME_AMBIGUOUS`（A5 起） |

- `is_selectable_brain(name, *, manager)` 只用 `RouteCatalog` 的兩個方法：自訂連線查表是啟用中 → 可選；已知但停用 → 不可選；其他名字轉小寫（同 SoulLayer）後必須等於模型清單裡「非代理、`owned_by` 不是 human 或 touka（舊靈魂路由 ID）」的模型 ID。比只看 `route_family` 嚴：3a 拿掉的舊簡稱（如 `claude-4-5`）、只寫家族名、靈魂與 L2 自訂模型 ID 都不可選。A5 改查表規則後，這裡自動得到不分大小寫與「模糊就報錯」。
- 錯誤碼：不可選用既有 `MODEL_NOT_FOUND`（1202／404）；模糊用新的 `MODEL_NAME_AMBIGUOUS`（1220／409），先登記、A5 起使用。
- 前端（SoulStudio）：「你指定的模型」下拉的初值是 `config.base_model`（空值時是停用的「未配置」佔位選項；已存的值不在清單時也以停用選項顯示），選項只列一般模型（不列 `is_agent`、human、touka）；「目前在用的模型」唯讀顯示 `active_brain`，沒設顯示翻譯過的「未配置」，和「你指定的」不同時加一句說明。存檔仍送 `base_model`；只改人格時送回原本的「你指定的」，不會蓋掉模型自己換的。

### A4、L1

- A4：`GET /v1/souls` 沒設主要模型或讀取出錯時 `active_brain` 回 `null`（出錯仍記 exception），不再回「未配置」「錯誤」字串；畫面自己翻譯。`souls` 路由沒有 response_model，OpenAPI 不變。
- L1：`get_active_brain` 遇到含 `toukasoul` 的舊值回 `None` 並記 warning，聊天回 409 `AGENT_BRAIN_NOT_SET`，不再偷換成寫死的 `claude-4.6-sonnet`（和 ADR-0241 決策 6「不偷換」一致）。

### A5 模型名查表不分大小寫（唯一符合才採用＋三條保護）

1. 唯一查表入口 `ModelManager.resolve_custom_route`：同一份快照的啟用與已知自訂連線中，先找完全相同的；不分大小寫只有一個符合就用它（`CustomRouteResolution.original_name` 帶原名）；兩個以上回 409 `MODEL_NAME_AMBIGUOUS`，不挑；停用照舊。`request_model_name` 把靈魂存的寫法轉成請求用的名字：自訂連線回原名，其他回小寫。靈魂層、群聊、子代理、心跳都經這個入口。
   - 欄位名：計畫寫的是 `model_id`，實作改成 `original_name`。原因是 Guard #1 IDENTITY_SSOT 不允許新的 `model_id` 字串欄位；沒有加 noqa、沒有改守衛。
2. 自訂模型和靈魂撞名改成明確的不分大小寫比對（比對靈魂 ID 清單，不靠檔案系統）。比對前先去掉 MonitorLayer 認得的寫法（`@`、`toukasoul` 前綴），所以 `@alice`、`toukasoul-alice` 也算和靈魂 `Alice` 撞名（比計畫嚴）。
3. 新 ID 和現有 L2 自訂模型只差大小寫 → 409 `MODEL_ALREADY_EXISTS`；建立與改名都擋，改名排除自己；已存在的資料不動。

### A3 沒設主要模型時群聊與心跳不當掉

- 群聊：每位成員都有權之後，再檢查主要模型能不能解析；有一位不行，整輪不派發，回串流內訊息（點名成員），並記 warning（錯誤碼名稱與成員 ID）。
- 心跳：`/awaken` 沒有 `model_override` 且沒有主要模型 → 409 `AGENT_BRAIN_NOT_SET`；背景迴圈解析不到模型時記 log、結束這次迴圈。

### A2 介面清理（API 欄位照 ADR-0241 保留）

- 拿掉 SoulStudio、ModelStudio、ModelsTab 的溫度／Top P／Top K 拉桿與唯讀參數格子；`app/create` 整頁刪（沒有任何入口）。
- 寫死的模型名改成空值，清單載入後選第一個可選的一般模型（SoulStudio 建立表單、ModelStudio、ModelProvider）；ChatProvider 找不到一般模型時不切換；IDE 行內補全預設空值、空值時不發請求。
- ChatProvider 與 ModelProvider 共用純函式 `ToukaChat/src/lib/default_model.ts` 的 `pickDefaultModel`：清單中第一個啟用、沒隱藏、非代理、不離線的模型，找不到回 `undefined`。
- `temperature`／`top_p`／`top_k` 的 API 欄位與存放值保留，存檔照原值送回。
- `tests/unit/test_no_hardcoded_model_ids.py` 防回歸：選模型的檔案（SoulStudio、ModelStudio、ChatProvider、ModelProvider、IDE 行內補全與 `package.json`、`brain_persistence`）不得出現廠商模型 ID 字面值；價格表、context window 表不在範圍。

### A6、A7 計費

- Gemini：輸出 token＝`candidatesTokenCount`＋`thoughtsTokenCount`（含回給客戶端的 `completion_tokens`）。
- DeepSeek：快取讀取取 `prompt_tokens_details.cached_tokens`，沒有才用頂層 `prompt_cache_hit_tokens`，兩者都在不相加。
- 成本上限因此照實（Gemini 思考 token 讓上限更早觸發）；只用 mock 驗證。

### 規則漏洞

- B1：重生 openapi 的腳本先開沙盒再載入產品程式（禁令 63）；合併純函式拆到 `scripts/openapi_merge.py`，輸出位置改用腳本位置推出的 repo 根。
- B3：ToukaDispatch 的 httpx client 明寫 `follow_redirects=False`；禁令 22 的 AST 守衛擴到 Dispatch、Memory、Heartbeat（只加不減）。
- B4：刪 Proxy 內走不到的排程引擎（`start_engine` 觸發的整條路徑，含唯一的 `verify=False`）；排程 CRUD 與 `routers/scheduler.py` 保留（ADR-0254 已刪）。
- C2：指令逾時後的回收有時間上限，取消不被吞掉（`reap_process_tree`）。
- C5：研究工具沒有模型時不當掉；Google 來源回結構化的 `validation_error`（禁令 59）。
- C6：`AnthropicThinking` 只定義一次，OpenAPI 的兩個元件合成一個。
- C7：ToukaChat 的 `npm run test` 先產生 artifact runtime。
- K1：Guard #20 的 knip 以 `--no-gitignore` 執行。工作資料夾放在被 `.gitignore` 排除的目錄底下時，knip 會用 `.gitignore` 濾掉入口檔而假紅（ADR-0243 記的已知限制）。原本靠 `.gitignore` 擋掉的產生檔與建置產物，改用 knip `project` 的排除樣式移出相依圖（只放 `ignore` 會變鬆：產生檔的引用會讓沒用到的 export 被當成有用到）；每條規則的分類清單在 `tests/unit/test_knip_worktree_parity.py`，由該測試防漂移。

## 代價

- 舊的無效「你指定的」值在沒有主要模型時會擋下存檔（404），要先在下拉選一個可選模型。
- 靈魂頁的主要模型和模型清單綁在一起：清單沒載入或缺金鑰沒發現到的模型，存檔會被判找不到。
- 大寫自訂連線模型在 A1 之後、A5 之前可以存成主要模型但聊天仍失敗（只存在於整合分支，不單獨合併；A5 已合入，最後的樹沒有這個中間狀態）。

## 實作偏離（審查時接受）

- A5：`CustomRouteResolution` 的新欄位叫 `original_name`，不是計畫的 `model_id`（Guard #1 IDENTITY_SSOT，見 A5 第 1 點）。
- A5 保護 2：和靈魂撞名的比對先去掉 `@`、`toukasoul` 前綴再比（比計畫嚴）。
- A1：`is_selectable_brain` 除了 `is_agent` 與 human，也排除 `owned_by` 是 `touka` 的舊靈魂路由 ID（比計畫嚴）。
- A2：挑預設模型時多排除已隱藏的模型（比計畫嚴）；防回歸測試除了 `claude-`、`gemini-`，也擋 `gpt-`、`deepseek-`、`grok-` 開頭的字面值（比計畫嚴）。
- B4：`NOQA_REGISTRY.md` 統計表的其他列原本寫 39，不是掃描得到的數字；改成用 Guard #19 同一掃描法實際清點的 327（說明欄寫明方法與各代號數量）。這一列不受 Guard #19 檢查。
- 計畫漏列、在單位驗收時經審查准改的既有測試，都只改 setup 或拿掉已刪的東西，斷言沒有放寬：`test_braces_entrypoints.py`（命令列清單多 `--no-gitignore`，K1）、`test_dispatch_callback_trigger.py`（假 client 收建構參數並多斷言 `follow_redirects=False`，B3）、`ToukaChat/tests/guard/a11y_basics.test.ts`（拿掉已刪的 create 頁兩項，A2）、`test_heartbeat_task.py`、`test_auth_heartbeat.py`、`test_background_pulse.py`、`test_heartbeat_awaken_hardening_security.py`（setup 讓靈魂有主要模型，A3）。

## 已知限制

- ~~建立靈魂的 API（`POST /v1/souls`）不驗模型名；畫面下拉只列可選模型~~（ADR-0254 已修）。
- 代理人仍可寫 `config.base_model`（照存、不驗、不動主要模型）。
- 模型清單在存檔當下沒載入時，有效 ID 也會被判無效。
- L2 自訂模型在 Windows 用不同大小寫更新同一資料夾的情況不處理（不改資料，只擋新建與改名）。
- Windows 上把自訂模型改名成只差大小寫的自己時，`custom_io.move_custom_dir` 會失敗，但路由不看回傳值、照樣回 200，名字其實沒變（既有問題，本任務沒改；ADR-0254 已修）。
- 【推測】不是經靈魂、而是直接用小寫名稱（例如 `qwen/qwen2.5`）聊天時，查表現在找得到自訂連線，但送出的模型名仍是使用者寫的小寫，自訂端點可能不認得。
- ~~【推測】ModelStudio 的模型下拉只排除 `is_agent`，可能混入 `owned_by` 是 human 的項目~~（ADR-0254 已改用共用的 `isSelectableBaseModel`）。
- Proxy 的排程 CRUD（`routers/scheduler.py` 與 `SchedulerService`）沒有掛載、走不到，`ENABLE_SCHEDULER` 設定沒有人讀；本任務只刪引擎，整組刪除列待排（ADR-0254 已刪）。
- `services/custom_model_service.py:92` 自訂模型清單的顯示預設 `base_model` 仍寫死一個 Gemini 模型名，只影響列表顯示，列待排（ADR-0254 已拿掉）。
- `scripts/update_docs.py` 改了匯入順序（先開沙盒），但它會跑 mkdocs build，所以沒有實際執行，只用 AST 測試驗證。
- Guard #22 偶爾因 pip-audit 回空輸出而假紅，重跑就過（U1、U2 各遇到一次；推測是查詢服務暫時沒回應）。
- 【推測】禁令 22 的轉址守衛用檔案系統掃描，以後若有人在 ToukaHeartbeat 或 ToukaDispatch 底下建自己的 `.venv`，守衛會掃到第三方套件而變紅（目前沒有這種資料夾）。
- A6、A7 的計費只用 mock 驗證，沒有打付費 API。

## 驗證

本機 Windows、Python 3.12 venv、fnm Node 24.21.0；各單位在自己的工作資料夾，U6 在最後的樹上跑完整檢查。本任務沒有打任何付費 API，也沒有啟動 Touka。

- 固定例外 E1：每次 pytest 都有 `tests/unit/test_local_compose_bindings.py` 的 6 個 failed（測試把 `DOCKER_CONFIG` 指到空目錄，本機 Docker Desktop 找不到 compose 外掛），要在同一個 commit 的 GitHub CI 通過才算。
- U0（K1）：4 個突變都讓 `guard:prune` 變紅（沒被引用的檔、沒用到的 export、只被產生檔引用的 export 兩種：產生檔沒被引用，以及產生檔本身在相依圖裡；後者在只放 `ignore` 時是綠的，所以改用 `project` 排除）；guard_all 43／43；pytest 兩種順序 8631 passed／8 skipped／6 failed（E1）；Vitest 263 檔 1610。
- U1（A1、A4、L1、SoulStudio）：突變 19 個都紅；guard_all 43／43；pytest 兩種順序 8676 passed／17 skipped／6 failed（17 個 skip 裡 9 個是 bash 不在 PATH，之後的單位補上）；Vitest 1616。
- U2（B1、B3、B4、C2、C5、C6）：突變 16 個都紅；B1 時沙盒重生的 `openapi.json` 與 `api.ts` 對 a5d4835a 零差異，C6 後只差兩個 `AnthropicThinking` 元件合併與 `$ref` 改名；guard_all 43／43；pytest 兩種順序 8712 passed／8 skipped／6 failed（E1）；Vitest 1617。
- U3（B2、C7）：突變 4 組都紅；guard_all 43／43；pytest 兩種順序 8688 passed／8 skipped／6 failed（E1）；Vitest 1617；`npm run test` 每次多約 0.75 秒。
- U4（A2 其餘）：突變 17 個都紅；guard_all 43／43；pytest 兩種順序 8714 passed／8 skipped／6 failed（E1）；Vitest 1624；ToukaIDE install、test（87）、typecheck、compile、package 都過。
- U5（A3、A5、A6、A7）：突變 20 組都紅；guard_all 全過；pytest 兩種順序 8765 passed／8 skipped／6 failed（E1）；Vitest 265 檔 1624；openapi 重生與 `tests/proxy/characterization` golden 都零差異。
- U6（最後的樹，只改文件；在 U6 文件 commit `1edcc6c0` 上跑，之後只補這段數字）：guard_all 43／43（Node 24.21.0 preflight 通過；#19、#20 的 vulture 與 knip、#28 都過）；pytest 固定順序與隨機順序（seed 20261014）都是 8765 passed／8 skipped／6 failed，6 個正好是 E1；8 個 skip 都是環境（4 個 Windows 不能建 symlink、POSIX 專用 1、要另外同意才跑的 Docker 部署與 Marketplace 各 1、沒有 `.env` 1）；`test_documentation_contracts.py` 3 passed；ToukaChat Vitest 265 檔 1624 passed。

## 回復

不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。各項可獨立 revert 產品程式與測試；A1 撤回時本 ADR 的 A1 段標「已撤回」，ADR-0241 的反向註記改回。寫進 `config.json` 的值都是可選的模型 ID，資料不需處理。
