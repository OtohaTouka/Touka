# ToukaChat 對話介面文件 (Standardized Chat Interface)

> **TL;DR** — 所有 HTTP 請求走 `fetchTouka()`（FETCH_SSOT）；`localStorage.*` 必須用 `useLocalStorage` hook（LOCALSTORAGE_SSOT）；`console.*` 禁止，唯一白名單 `error_handler.ts`（CONSOLE_BAN）；來回計算 SSOT：`countRounds()` in `export-utils.ts`；ToukaChat 跑在 port 3001。

## 重置卡查最後一次重置的結果（ADR-0289）

- `hooks/useAgentDataReset.ts` 多 `last` 與 `refreshLast()`：經 `fetchTouka` 讀 `GET /v1/admin/agents/reset/last`；掛載時查一次，按「完成」後再查；查詢本身失敗（連不上、403）時回 `null`、卡上保留上一次查到的，伺服器沒有紀錄時是 `idle`。
- `components/settings/tabs/AgentResetLastResult.tsx`：卡上方寫明是哪個靈魂、什麼時候（還在跑寫開始時間，其餘寫結束時間，經 `formatToTaipeiTime`）和結果一句話；結束的那次可以按「查看結果」打開報告，還在跑時按同一個按鈕再查一次。`idle` 與看不懂的狀態不顯示。
- 「結果不明」畫面多「查看結果」：同一個靈魂已結束就換成那份報告（上面寫明靈魂與結束時間，不跳通知）；還在跑、沒有完成（錯誤碼經 `errorMsg`，看不懂的當 `SYSTEM_ERROR`）、中途中斷照實說；沒有紀錄或是別的靈魂的，說查不到這次的結果；查詢本身失敗另說「現在查不到」。不輪詢。
- 對話框顯示結果時，標題與說明換成 `settings.agent_reset.result_title`／`result_body`（只看結果、不叫人輸入密碼）。

## 靈魂的工具清單（ADR-0281 T2）

- SoulStudio 建立與編輯共用 `components/studio/SoulToolPicker.tsx`；清單由 `hooks/useSoulToolCatalog.ts` 讀 `/v1/tools_stats`，回讀取中／讀完／讀不到三種狀態（換連線就重讀，別的連線讀到的清單不給）。沒讀完時照原樣列出存的條目、不判斷「不在清單」、不給改。
- 存檔格式與「勾了沒」由 `lib/soul_tools.ts` 決定：內建與記憶工具存名稱，MCP 存 `伺服器/工具`（用 tools_stats 的 `server`、`tool` 欄位）；舊長名 `伺服器__工具` 照舊算勾了，取消勾選時兩種寫法一起拿掉。比對規則以後端 `utils/soul_tool_selection.py` 為準。
- 清單裡找不到的條目（斷線伺服器的工具、使用者個人授權的工具、舊短名）存檔時保留，標「目前不在清單」，要拿掉得自己按；對不到任何工具的純名稱另外提示可以移除或重新勾選。勾了 `execute_command` 時，tools_stats 的 `tool_companions` 列的配套標「隨 execute_command 一起開」、不能單獨取消。外掛與名稱含斜線的 MCP 工具不在清單裡。不勾任何工具代表全部可用（唯讀頁也寫明，編輯時用警告色）。

## 對話換靈魂與家欄位（ADR-0276）

- 後端允許真正人類在有權存取的非家對話換靈魂；舊、新靈魂都須有驅動權。非空換綁保留原樹，內部建立標記永久降為 0；只有原列與待存列整棵樹無節點、摘要為 NULL、無家欄位且非保留／退休 ID 的嚴格空草稿可保留原標記。一般模型對話轉靈魂也照此規則，不必另建 ID。
- 家對話先透過 `POST /v1/ui/sessions/set_home` 取消家，再換靈魂。一般 save 的 `is_home`／`home_for_agent` 一律忽略，false／null 也不能清家；舊公開欄位仍相容。
- `touka.` 是內部頻道保留空間，UI 不能冒名建立或換綁；舊 `pulse_channel_default`／`sns_*` 保留可讀、不續寫、不遷移。心跳與 SNS 使用每靈魂的新頻道，不把舊共享內容當成新歷史。
- `SESSION_SCOPE_MISMATCH`（1221）已有 zh-TW／en 翻譯。一般 HTTP 歸屬錯誤回 403；條件存檔仍看 `persistence.status="failed"` 與 `error_code=1221`，不能只看 HTTP 成功。stale／unknown／saved 與串流生成終態維持原型別合約。
- 持久串流在生成前完成人類准入與換綁，buffer／producer 採提交後的新 revision；最後保存不能再換綁。`persistDone` 仍用本輪模型存富樹，`completeProducer` 採終態 revision，沒有新增前端換綁流程。
- `session_scope_version` 不在 list／GET／snapshot 的公開內容中。部署加欄及重啟會讓舊 revision 失效一次，應重讀 snapshot；生成失敗、取消或 no_text 不回滾已提交的換綁，舊畫面保存可能衝突，不能自動重送模型。家候選矛盾時不注入歷史；這次沒有新增修復舊資料的 UI。

## MCP 隔離設定與啟動（C2-3）

決策見 ADR-0275。`useMcpIsolation` 封裝來源與額外資料夾的 GET／PUT；`McpIsolationSection`、`McpSandboxPathsEditor` 與 `McpManualStartDialog` 各管自己的畫面。來源只可依 `can_mark_agent` 單向改成代理人建立；第三方／scaffold 可編輯最多 16 筆唯讀或讀寫資料夾。存檔帶 `bound`，成功後背景重連，變動衝突須刷新後重新確認；一般設定存檔的 `isolation_cleared` 提示哪些資料夾授權已清除。

MCP 設定的重啟回應為 `busy`／`waiting` 時，`useMcpSettings` 刷新清單並顯示對應翻譯提示；解除重啟中狀態。隔離徽章區分執行器、Landlock、未隔離、等待與暫停，並呈現原因與恢復方式；只有自測失敗類原因說明約每十分鐘重試。第三方暫時故障等滿 660 秒才標暫停，初次檢查 `probe_pending` 不計時。

側欄只呈現未隔離／等待／暫停的 MCP 台數，不列名稱或路徑；預期為 `none` 時不計未隔離台數。因此原生 Windows 只有未隔離第三方時沒有 MCP 橫幅那一行；有自建等待或暫停仍會顯示。無效計數各自歸零，不影響其他警告；設定讀不到的 `store_issue` 走通用提示。
自建伺服器等待隔離時說明恢復後自動啟動；有 `manual_start` 時徽章旁常駐「啟動…」。按下或重啟先開確認框並重新讀取來源與 `bound`，與清單中的啟動方案相符才可確認；送出畫面上的 `isolation_ack`、`features`、`generation` 與最新 `bound`。確認框明示代理人程式的未隔離風險，缺 `tcp` 時另列埠未封鎖警告。拒絕碼經共用隔離錯誤翻譯，刷新清單；確認框送出後關閉，同意不沿用。

層級 `none` 時每次重啟都要重新確認；降級 Landlock 的自建伺服器失聯後會回到等隔離。只有隔離連線自動退避重試三次（5／10／20 秒），direct 與遠端要人按重啟。

創建工具已接 runner：有隔離時 YOLO 依實際層級執行，scaffold 看本機層級；`none` 的卡仍須人核准且回送卡上的去處。未隔離卡若執行內容不完整（包括任一參數超過 65,536 字元或看不到實作程式）不可核准。已部署自建 MCP 不能再演化更新，須用新名稱或由人移除舊伺服器。

## 送出內容與沒設主要模型（ADR-0241）

`fetchChatStream` 所有分支（proxy 兩種協定、瀏覽器直連 openai／openrouter／litellm／ollama、預設）都不送 `temperature`，ollama 不送 `options`。非 200 時丟 `ToukaApiError`（`Error` 子類別，帶 `status`／`code`）；`StreamRegistryProvider` 的 onError 先交給 `useAgentBrainNotice`：`AGENT_BRAIN_NOT_SET` 跳 toast，按鈕送 `touka_switch_view`（`'soul'`）切到靈魂設定頁，其他錯誤照舊；在那裡由人類存檔時，代理人若還沒有主要模型，選定的模型會成為主要模型（ADR-0250 起的兩欄與救援規則見下方 Touka 待排小任務一節）。只涵蓋聊天畫面。

## 模型溝通層第 2c 段：三種模式的前端（ADR-0246）

- `AuthSwitcher` 只剩 Manual／Auto／YOLO，選 YOLO 仍二次確認；看不懂的值顯示 Manual。存檔後 `ChatProvider` 重讀對話清單、顯示後端實際存下的模式，被降成 Manual 時 toast 說明只有管理員或這個靈魂的擁有者能開；較舊的回應晚到時忽略。新對話還沒有 `agent_id`、目前選的是靈魂時，切模式一起帶上它存檔；選一般模型不綁，還沒有訊息的對話換模型也不被拉回；這時送出第一句會先把對話重綁到選的模型、顯示後端存下的模式再送（R-2c）。無痕對話鎖定顯示 Manual 並說明「無痕對話無法追溯，只能用 Manual」；在無痕對話要求 Auto／YOLO 時維持 Manual、不呼叫存檔，toast 說明原因。
- `ToolApprovalCard`：`tool_approval_request` 的選用欄位 `review_reason` 顯示成「審查員轉給你的理由」，`human_only` 顯示「只有你能核准」；兩個欄位在解算事件後保留（`stream_tree`、`tree_history`），型別不對不顯示。沒有新事件種類。
- 安全設定的「Auto 審查員」（`AutoReviewerSettings`，資料存取在 `useAutoReviewSettings`）：選項只來自 `GET /v1/autonomy/reviewer-models`，預設未設定，建議值只是提示；存檔沿用 `POST /v1/autonomy` 寫 `auto_review.model`（只有人類 admin 能改，R-2c）。說明不寫廠商名稱，也不再寫「無痕對話也一樣」（無痕一律 Manual，不會送審）。
- 心跳排程（`HeartbeatScheduler`、`heartbeat_mode`）：safe／full 顯示成 Auto／YOLO，看不懂顯示 Manual。新增連線表單的直連標示（`DirectConnectionNotice`）：直連沒有工具、不受權限模式管、不記帳。
- 錯誤碼 1118～1120（審查員拒絕、無人在場不跑終端、需要人但沒人在場）兩語翻譯。

## 模型溝通層第 4a 段：生成中插話的前端（ADR-0249）

- 按鍵（DB-20，`composerKeyAction`）：AI 工作中 Enter＝排隊、Ctrl／Cmd+Enter＝優先排隊（urgent：立即中斷正在寫的回答，見第 4b 段）、Esc＝停止；@ 選單開著時 Esc 由 `MentionInput` 先關選單。沒在生成時 Enter 照舊送出、Shift+Enter 換行。生成中送出鈕變成「排隊」，placeholder 提示三個按鍵。
- 資料層 `useInterjectionQueue`：經 `fetchTouka` 打 `POST`／`DELETE /v1/ui/sessions/{id}/interjections[/{iid}]?stream_id=…`，`stream_id` 取自 `touka_stream_started`。還沒拿到 `stream_id`、被拒（錯誤用 `errorMsg` 顯示）或連不上時，文字留在前端（`local`），不遺失。佇列在 `lib/chat/interjectionStore.ts`（每個對話一份，只在記憶體）。
- `QueuedMessageList`：輸入框上方的灰色清單，標示「下一個工具步驟交給 AI」或「回合結束後送出」與「優先」；可收回，或修改（＝收回後把文字放回輸入框，再按 Enter 重新加入）。
- SSE 契約（雙端常數：前端 `lib/chat/interjections.ts`、後端 `services/interjection_queue.py`）：
  - `{"type":"interjection_delivered","interjection_id","step","urgent","text"}`：取出後的下一輪送出成功才送，位置在那一輪送出之後、該輪文字之前；`text` 是使用者自己寫的原文。前端從佇列移除，並寫進該助理節點的 `interjections[]`（文字優先用事件的 `text`，沒帶才退回本地佇列的原文）（欄位同 API `Interjection`：`id`、`text`、`urgent`、`status:"delivered"`、`step`、`created_at`）；`InterjectionMarker` 在工具時間軸下標出「你的補充在第 N 步送達」。
  - `{"type":"interjection_undelivered","interjection_ids":[…]}`：回合結束時（[DONE] 或停止之前）送一次，列出還沒送達的（含下一輪重試全部失敗退回的），前端標成未送達。串流任務被取消時兩個事件都不送，前端照下面的回合結束規則處理本地佇列。
  - 兩個事件都在 `connections.ts` 的自訂事件白名單；重整後從頭重播時，送達事件的 `text` 讓節點與下一輪標註仍有原文。
- 回合結束（`useInterjectionComposer`，同一對話 isSending 由真轉假）：正常完成 → 佇列裡未送達的（含 local、還在排的）用空行合成下一則自動送出，父節點是剛答完的 assistant 節點（`handleSend(text, assistantId)`），附件區暫存的附件會一起送出；按停止或出錯 → 不自動送出，放回輸入框（接在原本的草稿後面）。串流剛結束時插話被拒（404）同樣放回輸入框。切到別的對話不算回合結束（停止旗標歸零）；背景對話的項目留在佇列，回來時可收回或修改。
- 歷史重組（裁定 7，`buildApiHistory`）：組下一輪 API 訊息時，帶 `interjections[]` 的助理節點在 content 前面加固定標註 `〔本回合中使用者補充（第 N 步）：…〕`（只取已送達、有文字的，照步數排、每則一行，與原文以空行分隔），角色仍是 assistant。回合數不變（`countRounds` 兩處、`splitSessionMessages` 都只看角色）。
- 錯誤碼 1216～1219 的畫面文字在 `error.session.interjection_*`。不做心跳插話與 /btw（DB-22）。伺服器存檔時也在助理節點存同一形狀的 `interjections[]`（只有已送達的），伺服器組給模型的歷史用同一個標註（ADR-0249）。

## 模型溝通層第 4b 段：Ctrl+Enter 中斷（ADR-0249）

- 事件 `{"type":"round_interrupted_by_user","step":N}`：Ctrl+Enter 打斷正在接收的那一輪時，在那一輪收掉之後、下一輪的 `interjection_delivered` 之前送，兩者 step 相同；一輪開始前就有 urgent 排隊時不送（那一輪直接不送）。step＝已完成的工具步數，第一輪被打斷是 0（標註與標記會顯示「第 0 步」）。常數 `ROUND_INTERRUPTED_EVENT`（`lib/chat/interjections.ts`），在 `connections.ts` 的白名單。
- 前端在同一個助理節點的 `interruptions[]` 加一筆 `{step}`（同一步被打斷兩次記兩筆；重整後重播時重建，伺服器存檔不帶這個欄位，只靠 full 存檔）；`InterjectionMarker` 標「第 N 步：這一輪被你打斷」，和送達的補充照步數排、同一步先列打斷。Enter、Esc、收回與修改、回合結束的處理照第 4a 段。

## Touka 待排小任務：靈魂頁兩欄、預設模型、拉桿移除（ADR-0250）

- 靈魂頁（`SoulStudio`）主要模型分兩欄：可編輯的你指定的模型（下拉，值是 `config.base_model`；空值時第一個選項是停用的未配置佔位、已存的值不在清單時也以停用選項顯示；選項只列一般模型，不列 `is_agent`、`owned_by` 是 human 或 touka 的項目），與唯讀的目前在用的模型（`GET /v1/souls` 的 `active_brain`，`null` 時顯示翻譯過的未配置）。兩者都有值又不同時加一句說明模型自己換過。存檔照舊送 `base_model`；只改人格時送回原本你指定的，不會把模型自己換的蓋回去。
- 救援：`AGENT_BRAIN_NOT_SET` 通知帶到靈魂頁後，人類存檔時若還沒有主要模型，你指定的模型（必須在模型清單裡）會成為主要模型；名字不在清單時後端回 404 `MODEL_NOT_FOUND`，畫面照既有的保存錯誤流程顯示它的翻譯訊息，設定與人格都沒寫入。只差大小寫無法判斷時回 409 `MODEL_NAME_AMBIGUOUS`（1220，兩語翻譯 `error.model.name_ambiguous`）。
- 預設模型不寫死：`ModelProvider` 的 `selectedModel` 初值是空字串，模型清單載入後若還沒選，選 `pickDefaultModel`（`src/lib/default_model.ts`：第一個啟用、沒隱藏、非代理、不離線的模型）；已選的不被蓋掉，找不到就維持空字串。`ChatProvider` 開新對話時目前選的是靈魂，就換成 `pickDefaultModel` 的結果並提示；找不到就不切換、不提示。SoulStudio 建立表單與 `ModelStudio` 的基礎引擎同樣在清單載入後選第一個一般模型。
- 取樣參數 UI 移除：SoulStudio（唯讀、編輯與建立）、`ModelStudio` 的推理參數欄、設定頁 `ModelsTab` 的參數面板都拿掉了；`/create` 頁（沒有任何入口）整頁刪除。`temperature`／`top_p`／`top_k` 的 API 欄位與存放值保留，存檔照原值送回（ADR-0241 起本來就不送給廠商）。
- ToukaIDE 行內補全：`touka.inlineCompletionModel` 預設留空，留空表示不啟用、不發任何請求；有設才用設定的模型送出。
- 開發：`npm run test` 先產生 artifact runtime（`gen:artifact-runtime`）才跑 Vitest；`guard:prune`（knip）帶 `--no-gitignore`，產生檔與建置產物由 `knip.config.ts` 的 `project` 排除。

## 隔離第一批（iso-c1）：還原結果、模型下拉、設定搜尋（ADR-0254）

- 還原（`EnvSettings` 的系統快照分頁）：送出還原、後端回 accepted 後顯示「系統還原中」對話框；計時器與查詢在領域 hook `useRestoreWatch`（經 `fetchTouka` 打 `GET /v1/system/restore/status`，型別 `RestoreStatus` 從生成的合約取別名）。15 秒後第一次查：
  - `failed`：關掉還原中對話框、回到備份清單，跳常駐錯誤 toast（要手動關，翻譯 `dashboard.env_settings.restore_failed`：還原沒有完成、服務沒有重啟、部分檔案可能已被覆寫，請查看伺服器日誌後再試一次），不重整。用專用翻譯、不用 `errorMsg(SYSTEM_BACKUP_FAILED)`（「系統備份失敗」用在還原會誤導）。
  - `running`：先不重整，5 秒後再查，直到不是 `running`；還原卡住時畫面停在「還原中」。
  - 成功（伺服器重啟後回 `idle`）、查不到（重啟中連不上、未授權或其他非 2xx）：照舊重整頁面，不跳錯誤。
  - 對話框關掉或元件卸載時計時器清掉。
- `ModelStudio` 的基礎引擎下拉和 `SoulStudio` 的主要模型下拉用同一個規則 `isSelectableBaseModel`（`src/lib/default_model.ts`）：不列 `is_agent`、`owned_by` 是 human 或 touka 的項目；預設仍是清單載入後第一個一般模型。`pickDefaultModel` 本身沒改（還沒排除 human／touka，列待排）。
- `ModelStudio` 改名與刪除（ADR-0254 H12 補記，2026-10-25）：改名回任何非 2xx 都顯示錯誤並停下，不再對新 ID 存檔；改名成功就記下新 ID（`originalId`），接下來的存檔失敗時重試直接存到新 ID、不再送一次改名，表單上沒存的修改留著。改名可以用中文名字（後端 `new_id` 規則和建立時一致）。刪除送 `originalId`（後端記著的 ID），後端沒刪成就顯示錯誤、不從畫面拿掉，回 404（已經被刪掉了）照樣拿掉；還沒存過的新模型只在畫面上拿掉、不送請求。
- 設定頁搜尋拿掉已不存在的「取樣參數（溫度／Top P／Top K）」條目，搜「溫度」「Top P」找不到；兩語翻譯 `settings.search.entries.model_params` 一起刪。
- ToukaIDE 行內補全的請求不再帶 `temperature`（後端本來就不送取樣參數，ADR-0241）。
- 已知限制：ToukaChat 與 ToukaIDE 選 anthropic 協定時自己寫死 `max_tokens: 4096`；後端的 Anthropic 形狀入口現在會把它帶給非 Claude 模型（ADR-0254 O20），所以這條路接非 Claude 模型時輸出上限是 4096（預設協定是 openai，不受影響；列待排）。

## 審查員等待秒數、IDE 行內補全（ADR-0271）

- 安全設定「Auto 審查員」卡片多一欄「審查員等待秒數」（數字輸入 1～120、步進 1），寫進 `auto_review.timeout_s`，其他欄位原樣；仍經 `POST /v1/autonomy` 存檔，權限不變（`auto_review` 有變更走人類 admin）。空白、0 以下或超過 120 時顯示提示、存檔鈕停用（對齊後端 `gt=0, le=120`）。上限常數 `REVIEWER_TIMEOUT_MAX_S` 在 `useAutoReviewSettings`，元件經 hook 回傳的 `timeoutMaxS` 取用。新翻譯 key `settings.auto_reviewer.timeout_label`／`timeout_help`／`timeout_invalid`。新安裝預設 60；live 設定若已存 20 會照顯示 20，要自己改成 60。
- ToukaIDE 行內補全送 `X-Touka-Purpose: inline-completion`（Proxy 只拿來讓 Claude 5.5 少想），10 秒逾時與 VS Code 取消合成同一個 signal，取消時回 null，逾時與取消不印錯誤。IDE 中止後 Proxy 端的非串流請求不會跟著停（已知限制）。

## 隔離第二批（iso-c2）第一段：審批卡的隔離標示（ADR-0272）

- `stream_tree` 解析 `tool_approval_request` 的選用欄位 `isolation`（格式見 `PROXY_INTERFACE.md`「隔離第二批（iso-c2）第一段」一節），解算事件與 `tool_start` 之後仍留在步驟上（`tree_history` 同步）。沒帶或是 null 時卡片和以前一樣；看不懂的層級當 `none`；`fallback` 只在 `none` 時保留，只認 `executor`（第二段起）、`landlock`、`direct`。
- `ToolApprovalCard`：層級 `landlock`／`executor` 不顯示標示。`none` 加 `fallback: landlock` 顯示「隔離降級」（中性開頭；有 `tcp` 接「連不到 Touka 內部服務」，沒有接「但擋不住它連到 Touka 內部服務」，沒有 `signal` 再加「可能讓 Touka 重啟」；沒有 `tcp` 可能是核心太舊〔ABI 3〕或網路設定不安全〔`net_tcp_fastopen`、`net_mptcp`〕，所以這句不寫原因，原因那行另外說明）；`fallback: direct` 顯示「未隔離」並寫清楚影響範圍（目前沒有未接 runner 的工具；創建卡依實際層級，`scaffold_mcp` 看本機層級）；`none` 卻沒有合法 `fallback` 時顯示「看不懂這次的隔離狀態」、批准按鈕停用（拒絕仍可按）。
- 原因說明先找細節專屬的（例如 `sandbox_state_unconfigured` 的 `not_writable` 給三個補救步驟），再找原因的，都沒有才顯示「原因：{code}」；每個原因碼兩語各有一行說明（`chat.tool_approval_card.isolation.*`；`net_tcp_fastopen`、`net_mptcp` 附 compose 的 `sysctls` 寫法與 `docker compose up -d`〔提醒帶平常用的 `-f` 檔〕，再加一句不用 Docker、直接在 Linux 主機上跑時改設主機的 sysctl），重建指令與補救步驟各自一行、保留換行。
- 核准 `none` 卡時 `resolve()` 經 `fetchTouka` 送 `{"payload": {"isolation_ack": <卡上的 fallback>}}`；其他卡與所有拒絕照舊只送 `POST`。舊版頁面沒送 ack 時後端回 422、不算核准。

## 隔離第二批（iso-c2）第二段：執行器的卡片文案、隔離橫幅與通用提示（ADR-0272）

- 卡片：隔離標示拆成 `IsolationNotice`，用哪幾句由純函式 `lib/isolation_wording.ts` 決定（卡片與橫幅共用）。主句看 `fallback`：`executor`「會在執行器裡執行」、`landlock`「會在 Touka 主容器內以檔案隔離執行」、`direct`「未隔離」；開頭的原因子句（`isolationClause`）依序是不在執行器範圍、執行器不能用（預期執行器時其他非埠原因也算）、網路設定（去處是執行器時）、讀不到連線設定、要封鎖的埠太多；結尾（`isolationTcpTails`）有 `tcp` 寫擋得住，沒有時分別寫擋不住內部服務與本機上游埠、自訂連線與 MCP 的上游埠可能沒擋，執行器另接「和 Touka 在不同容器、共用同一個核心」；原因那幾行（`isolationReasonCodes`）在去處是 `direct` 時把本機原因放最前，再補網路設定與封鎖埠、去重，每行都附補救方式；網路設定的覆寫要加在 `touka-proxy`，預期執行器時加在 `touka-proxy` 與 `touka-executor`。新原因碼兩語都有說明（`chat.tool_approval_card.isolation.*`，`interpreter_untrusted` 照實際檢查寫）。`stream_tree`／`tree_history` 解析並保留 `expected`、`net`、`blockPortsIssue`、`localIssue`；型別不對的事實丟掉，看不懂的 `expected` 不保留。
- 隔離橫幅：`useIsolationStatus`（登入後經 `fetchTouka` 打 `GET /v1/isolation`，憑證換了或每 30 秒重問，沒有結果回 null）＋`IsolationBanner`（右側欄、`role="alert"`、和降級橫幅同樣式）：層級低於預期時寫「隔離比預期弱」與原因（含補救；預期執行器而原因是連不上或權杖不對時，補一句控制通道的權限可能被改），層級照預期但沒有 `tcp` 時寫埠沒擋（核心不夠新），`repairs.permission_relock` 時寫自癒額度用完；`denied_burst` 不顯示；原因文字共用 `useIsolationReasonText`。原生 Windows 不會出現（預期就是 `none`）。
- 通用提示：`SystemStatusContext` 讀 `/health` 的 `system_notices`（格式不對的略過，未登入也看得到）；`NoticeBar` 成為共用元件，`MemoryNoticeBar` 與新的 `SystemNoticeBar`（右側欄）都用它；左側身分列的 `systemWarning` 標記只反映系統提示（記憶提示照舊用 `hasMemoryWarning`）。翻譯 `sidebar.system_notice.*`（自主設定、安全規則讀不到，附恢復指令）、`sidebar.isolation_banner.*`。

## 模式與登入結果（ADR-0232）

SystemStatus 與登入共用 `getActiveProxyUrl()`，支援預設／同源且未保存 connection 的情形。認證尚未就緒不送 Memory probe；通過 health／Memory 必要欄位驗證、非降級且工具數大於零才為 hybrid；明確降級才為 standalone。401、403、網路／5xx／malformed／零工具維持未知或錯誤，徽章不能以二分法假稱可用。

檢查只在掛載、既有事件與手動操作觸發；origin／scope／generation 撤銷舊回覆，JSON body await 後亦核對。登入 initial refresh／login／logout／profile 同樣錨定操作與 credential epoch；相同 origin 設定更新不登出，正常同 principal refresh 由 module token SSOT 送卡。這只保護 client state／scope，不宣稱取消可回滾 server 或 cookie。

實際登入鏈 `/login → AuthContext → authService`：錯誤只接受既有 known auth id/code（與 HTTP status 一致），未知內容固定 fallback；token/profile 合法且同 principal 才完成登入。登入 pending 只提交一次，安全錯誤無原 body／token／密碼。既有一般帳號的 Soul/Pulse/Vault 導航沿確證 hybrid 顯示，RBAC 不變；source 測試／consumer 審查與人工 GUI 見證分開。

## 記憶提示與舊對話同步（ADR-0253）

- `SystemStatusContext` 把 `/v1/memory` 的 `memory_notices` 當選填欄位解析成 `memoryNotices`：不是陣列、項目格式不對就略過，看不懂的等級當資訊級，`count` 不合法就丟掉。不改變上一節的 hybrid／standalone／degraded 判斷，降級時提示照留。型別 `MemoryNotice` 在前端自己宣告（端點沒有 openapi 回應模型）。
- `MemoryNoticeBar`（右側欄降級橫幅下方）依代碼查翻譯 key，看不懂的代碼不顯示；`info` 平靜顯示（`role="status"`），`warning` 和降級橫幅同色（`role="alert"`）。有警告級提示時，左側 `SidebarIdentity` 顯示帶 `aria-label` 與 tooltip 的警告標記，讓右側欄收起時也看得到；判斷只在 `lib/memoryNotices.ts` 的 `hasMemoryWarning()`。
- 設定頁舊對話同步（`useLegacySync`）只在 HTTP 成功且 `status == "success"` 時顯示同步成功；失敗顯示錯誤並重抓統計，讀統計失敗也顯示錯誤。統計的 Arch 是已處理紀錄數、Pending 是還沒處理的 JSON 數（ADR-0264：同步不搬檔）。
- 「切分存入舊對話」（`useSessionActions.handleSplitExport`）：端點直接歸檔進該靈魂的舊對話庫，回應不含路徑；成功才把前段剪掉（ADR-0264）。
- 端點行為見 [PROXY_INTERFACE](PROXY_INTERFACE.md) §1.10b 與「Legacy 統計端點」一節。

## 首次設定警告（ADR-0053，2026-10-23 補記）

- 管理員密碼還是臨時的、初次設定還沒完成這兩個警告，畫在左側邊欄最上面（`SidebarSetupNotices`）。狀態、「稍後」與設定精靈都在 `app/layout.tsx` 的 `SetupNoticesProvider`（在 `AuthGuard` 裡，只呼叫一次 `useSetupStatus`）；這個提供者不能畫任何佔版面的元素，因為 body 是左右排列。
- 有警告時桌面側欄收合鈕與手機選單鈕顯示紅點（`SetupNoticeDot`），按鈕名稱改成說明有設定警告。手機版的 `LeftSidebar` 收到 `onSetupAction`，按警告按鈕會先關掉 Sheet，精靈才不會被擋住。
- 左側邊欄內層整欄可上下捲動，對話紀錄區有最小高度，警告佔位置時底部導覽與登出仍點得到。
- 精靈第 2 步改完密碼（後端撤銷所有舊卡，ADR-0144）後不前進：`SetupWizard` 改畫 `ReloginNotice`（隱藏上一步與 ×，焦點移到「前往登入」，按下呼叫 `useAuth().logout()`），並由提供者經 `useSetupResume.saveSetupResume` 記下接續點（localStorage `touka_setup_resume`：`{ userId, stepId, savedAt }`，`SETUP_RESUME_TTL_MS`＝1 小時；三欄型別、步驟名稱、時間不在未來都驗過才算有效，能解析但格式不對或過期的整筆刪；`me.id` 由第 2 步傳上來，畫面已卸載也照寫）。登出時 `AuthGuard` 先卸載整棵樹（含 Toaster），到 `/login` 重新掛上；登入後到首頁不重掛，所以 `useSetupResumeAfterLogin` 在管理員的 user.id 出現或改變時取一次（登入、帶有效 session 重新整理都算；別人的不取不刪），提供者等狀態讀到、`hasSetupIssues` 為真才打開精靈。登入頁用 `peekSetupResume`（只看不取）顯示說明。管理員密碼已不是臨時密碼時第 2 步有「保留目前密碼」；前進到第 7 步時呼叫 `onStatusRefresh` 重讀狀態（在 `next()` 裡觸發，不用 effect）。

## 訊息時間戳（ADR-0264）

- `MessageNode.timestamp`（毫秒，經 `time_utils.getSystemEpoch`）：使用者訊息在送出時蓋（`buildUserTurnTree`，同一次送出只取一次，保存重試時重算的樹相同）；助理回覆在 `persistDone` 蓋完成時間（`stampNodeTime`）。節點已有時間（含後端寫的心跳節點、重整後接回的節點）一律不動；寫回整棵樹時照原樣帶回。伺服器不補時間（會和讀回核對衝突）。
- 匯入的舊檔可能是秒：`time_utils.toEpochMs` 統一成毫秒（小於 1e11 視為秒，和後端 `legacy_time` 同一條規則）。
- `formatToOpenWebUI` 的 `timestamp`／`created_at` 是訊息實際時間（秒）；節點沒有時間才照舊用 `createdAt` 或匯出當下。
- 這些時間來自使用者裝置的時鐘；後端給模型看時標明「來自對話紀錄」，不當可信事件。

### 記憶庫結構提示與記憶寶庫讀取失敗（ADR-0256）

- `memory_notices` 另有 5 個警告級的結構提示代碼（升級空間不足、被佔用、記憶庫讀不到、升級沒有完成、全新記憶庫但旁邊有升級前的備份），由後端每次請求即時算；`MemoryNoticeBar` 的代碼對應表與兩語系文字必須涵蓋全部代碼（Vitest 釘住），看不懂的代碼仍不顯示，但左側警告標記照亮。
- 前端不輪詢 `/v1/memory`：記憶寶庫頁（`VaultDashboard`）讀 `/v1/memory/list` 失敗（不是權限不足）時，顯示錯誤框（`role="alert"`）、記憶筆數顯示「—」、不顯示空狀態，並呼叫 `useSystemStatus().checkStatus()` 讓側欄重抓提示；權限不足時只在錯誤框顯示錯誤原文，不叫側欄重抓。


## 對話保存、終態與恢復（可靠性 R2，來源候選）

`SaveResult`／snapshot／started／terminal 以 Pydantic→OpenAPI→api.ts→types.ts alias 為唯一型別。
`syncSession`／`updateSession` 回 typed 結果；只有 saved(full) 或無痕 skipped(incognito) 完成前端保存屏障。
text／no_text 不是 full；completed no_text 的富內容仍補存，error／cancelled 不自動保存部分結果。
同 scope/session writer 按已確認樹套用純語意命令，logical operation 去重；React updater 不做 HTTP。
純轉換例外回 failed、釋放 pending／producer 並保留草稿，不讓 rejected promise 永久阻斷後續操作。

列表沒有 revision；首次 strict snapshot 必核對內容或綁定明確缺席，不能替舊樹配上最新 token。
POST 送 confirmed revision；DELETE 送 If-Match，缺席仍等待明確 DELETE 200 才移除。
`setSessions`／ref 共用 transition，外部 ingress 不視為保存證據；pending／failed／unknown／unconfirmed
草稿在 state、refresh returned list 與 resume 都受到保護。省略保留，取消 project 綁定送 explicit null。

scope 綁 proxy origin、實際 credential principal／epoch；明確登入／clear 退休舊操作，同 principal
內部 refresh 可維持 epoch。wire、refresh、body await 後重驗並 best-effort abort；清除 registry、
history、input、attachment、Debug payload 與 artifacts，舊 callback 不得回填。這不取代 server auth。

user full 成功才送模型。只有同 generation typed terminal 能證生成結果；EOF／DONE不是保存成功。
full 補存未確認保留非 active registry 富樹。Notice 提供唯讀核對；同內容才 saved，不同保留 conflict。
確認 failed 且取得新 revision 後可按「僅重試保存」單送一次 POST，不重送模型／工具。
恢復成功後同 session 可開下一回合；無痕所有終態只留 memory，不要求持久核對或 retry。

匯入部分失敗不報全部成功；split 匯出後縮減綁開始時的內容，等待期間 drift 拒絕覆蓋。
無痕 split 拒絕縮減，提示使用既有本機匯出。提示具 en／zh-TW 資源，不能由 fallback 假通過。
詳 [ADR-0231](adr/0231-session-persistence-outcomes-and-revisions.md)；完整來源驗收當時記在 HANDOVER，現在只留在開源前的私人歷史。

## 對話刪除結果（可靠性 R2／B2，來源候選）

`SessionListProvider.deleteSession(id): Promise<boolean>` 只在 HTTP200、JSON `status=success` 與本筆 `session_id` 一致後回 true 並移除本地項目；無痕只在本地移除，HTTP為零。
403／404／500、畸形內容或網路失敗回 false，保留項目與其他 session 的並行變更，提示使用既有固定 `DB_DELETE_FAILED`。false 包含結果未知，不證明遠端未提交，也不自動重送。
既有後端「已不存在亦200成功」契約維持，404不得類推成成功。home／所有權保護不變。
`useSessionActions` 第二次確認 await 結果，同步 ref 防連按；pending取消原確認timer，失敗保留確認。
`SessionItem` 的兩次選取皆阻止 Radix 自動關閉，成功移除項目；使用者按Escape或主動關閉仍清確認。
跨 scope、保存順序與 revision 屏障依上節；來源候選尚未發布。

## Heartbeat 排程管理（ADR-0230，來源候選）

`useHeartbeatSchedules` 透過 `fetchTouka` 呼叫 Proxy 真人橋接，使用生成型別、裸 list 與 ETag。
POST upsert／DELETE 必帶 If-Match；mutation mutex 防重送，stale generation 不能覆蓋新資料。
載入失敗保留舊列表並停用無可靠revision的修改，UI可重新讀取；一般user只選owned agents，admin可全域。

修改成功才清除編輯內容；daily_times 先複製再排序，失敗不改 React state array。
已儲存／刪除但刷新失敗顯示獨立警告，不當作未寫入、不自動重送mutation。
已知錯誤沿生成ERRORS訊息，未知detail或Error.message用固定安全fallback，不能把後端原文反射到alert／toast。
`sync_to_clock`／`max_runs` 本次沒有新增功能，也不顯示成功生效的承諾。

`guard:typecheck` 涵蓋 tsconfig.guard 的 app source、排除測試；另執行本地 tsc 的
`tsconfig.json --noEmit` 才涵蓋完整 app/test。詳 [ADR-0230](adr/0230-heartbeat-transaction-and-credential-contracts.md)。

本文件定義了 ToukaChat 前端對話系統的核心介面、佈局規範以及單一事實來源 (SSOT)。

## Soul Studio 保存與回饋契約

`useSoulSave` 經原 `fetchTouka` 單一 POST 同送密碼與修改；輸入及回應 alias 引用生成 API 契約。只有 HTTP 成功、合法 string-map 且 `status=success`、`soul_id` 等於目標，才顯示「密碼驗證通過，修改已儲存」。關閉密碼框後回到同一靈魂的唯讀詳細頁，保留選取與捲動、焦點回編輯入口；成功／同步警告在捲動區外持續可見，不依賴 toast。

canonical `AUTH_REAUTH_FAILED` 以視窗內 alert 呈現並清密碼；其他權限、欄位驗證或保存錯誤保留正確分類。只有 code/id/status 一致的既有錯誤碼與合法 422 欄位 detail 才是已知拒絕；畸形／衝突回應、網路失敗或逾時顯示結果未知，不展示原始後端文字，不推定密碼正確或尚未寫入。失敗保留草稿；取消清已知錯誤，但保留未知結果與重送鎖。

保存使用 Time SSOT 的 epoch、局部 30 秒 UI 等待上限（有效連線期限更短時取較短者），涵蓋認證前置 refresh、POST 與 body 解析；瀏覽器暫停不是硬即時期限。AbortSignal 使過期傳送失效，不代表伺服器取消寫入；共用 refresh 可自行完成。同步 ref 防雙擊／Enter 重送。換魂、換 Proxy、unmount 與保存起點使舊讀寫回應失效；沿既有 `touka_connections_updated`／storage 通知訂閱 Proxy，舊 Proxy 的清單不能作新編輯來源，必須先載入新 Proxy。

已確認保存先呈現本次提交值；背景讀回成功才更新為服務端值，不以全頁 loading 掩蓋成功區。`AgentProvider.refreshAgents(): Promise<boolean>` 回報原刷新成功與否，原初始化、authSelfHeal、create/delete 可繼續忽略結果；保存流程消費 boolean。`loadSouls` 的所有普通／背景讀取均受 generation 約束，保存前未完成的 GET 不得覆蓋新值。後續讀回失敗只顯示「已儲存，但重新整理未完成」，按鈕只重新讀取，不重送 POST。

未知結果須由使用者手動讀回同一 Proxy／唯一同魂目標，唯讀比較草稿與服務端值的一致性／差異欄位；不覆蓋草稿或把一致性當作密碼驗證證據。有效核對後仍須使用者再次明確提交。密碼只留記憶體並於終態／取消／離開清除；不落 storage、URL、log。測試僅用 mock/sandbox，沒有新端點、認證 authority、資料 schema 或 ADR。實作範圍見[核准計畫](proposals/soul-studio-save-feedback-plan.md)。

## SNS 憑證表單與退役儲存（ADR-0224）

SNS 設定由單一 `useSnsBotSettings` 實例管理純記憶體表單。只有成功載入目前 Proxy 的 canonical 設定後才能儲存；載入失敗／未知狀態、跨 Proxy 舊設定、重複送出與送出中的編輯均不得提交。成功 POST 後重新載入伺服器遮罩值；失敗清除 token 編輯並保留非秘密欄位，離開表單則使舊請求失效並釋放 draft。既有 bot CRUD、穩定 ID 與後端 write-only placeholder 語意保留。

`RetiredSnsStorageCleanup` 在 I18nProvider 內、AuthGuard 外呼叫 `useLocalStorage.ts` 的精確退役 key 清除接縫；只移除 `touka_sns_bots`，不讀回秘密、不清除無關 storage，跨 tab 重寫時再次移除。Storage 無法存取時顯示翻譯 alert，不記錄 raw 例外。關閉／離線的舊 client 須載入新版才能清除；React state 釋放不等於 OS／瀏覽器 secure erase。契約與實測見 [ADR-0224](adr/0224-sns-secret-memory-and-retired-storage.md)。

---
## 🛡️ 認證導航與路由守衛 (Auth Navigation) ⭐

前端實施了全域的身分驗證攔截機制：

### 1. AuthProvider (狀態中樞)
- 負責管理 `accessToken`、`isAuthenticated` 與 `isLoading` 狀態。
- **靜默刷新**: 頁面重新整理或初始載入時，自動呼叫 `/auth/refresh` 嘗試從 Cookie 恢復會話。
- **Credentials**: 所有的 `authService` 調用均強制帶入 `{ credentials: 'include' }`。

### 2. AuthGuard (組件級攔截)
- 包裹於 `RootLayout`。
- **路由導向**: 
    - 未登入時：除了 `/login` 與 `/register`，其餘路徑自動跳轉至登入頁。
    - 已登入時：訪問 `/login` 或 `/register` 自動跳轉至首頁。
- **載入過度**: 使用 `Skeleton` 或 `Loader2` 元件覆蓋載入中的空白期，確保 UX 韌性。

---
## 🎨 UI 規範與設計即代碼 (Design as Code) ⭐

系統導入了 **Shadcn UI** 並建立了統一的展示頁面：
*   **展示位址**: `/design-system`
*   **原則 (Legal Architecture)**: 嚴禁使用硬編碼數值 (如 `text-[9px]`)。代碼品質由 **ESLint 法律** 強制執行偵測 `no-arbitrary-value`，確保視覺樣式 100% 來自 `tokens.ts` 或 Tailwind 定義。
*   **雙欄預覽 (WYSIWYG)**: 在「主題設定」分頁中實作了即時預覽 Showcase，實體渲染當前主題的所有設計 Token（背景色、邊框、圖示色），達成所見即所得的配置體驗。
*   **AGENT_TOKENS 鍵值設計 (匿名化)** ⭐：`src/lib/design-system/tokens.ts` 的 `AGENT_TOKENS` 物件不使用真實 Agent 名稱作為鍵值。主題名稱以顏色命名（`Amber`、`Rose`、`Emerald`）而非具體 Agent ID，確保設計系統對外不洩露系統內部人格識別資訊。
*   **Storybook 已移除（2026-05-22）**：Storybook 相關套件與 story 檔案已完整移除。視覺元件文件由 `/design-system` 頁面（`ThemeShowcase.tsx`）替代。`knip.config.ts` 已更新，移除 Storybook entry 條目。
*   **換行保留 (Newline Consistency)** ⭐: 聊天訊息渲染必須使用 `whitespace-pre-wrap` 類名，確保系統中的換行、縮進在前端與後端保持視覺一致性 (SSOT)。
*   **時間主權 (Time SSOT)** ⭐: 所有的時間處理必須透過 `src/lib/time_utils.ts`。嚴禁使用原生 `Date.now()` 或 `new Date()`。由 `guard_time.ts` 進行強制稽核。所有時間戳記以 UNIX Epoch 為基準，顯示時統一轉換為台北時間。
*   **錯誤碼主權 (Error Code SSOT)** ⭐: 嚴禁在 UI 中寫死提示字串。必須引用 `lib/error_codes.ts` 中的 `ERRORS` 常數。由 `guard_errors.ts` 進行強制稽核。
*   **記憶系統相容性 (Memory System Compatibility)** ⭐：前端通過 Proxy API 調用記憶系統工具。ToukaMemory 依 ADR-0116 僅以同行程函式庫整合於 Proxy，不再以獨立 MCP server 對外；前端無需感知底層實作。

---

## 🔀 聊天串流背景續跑 (Background Session Streaming) ⭐（ADR-0080）

對話串流以 **per-session 登錄表（`StreamRegistryProvider`）** 為進行中串流的唯一事實來源，使「切走後背景續跑、切回看到結果」成為可能：

*   **進行中串流 SSOT**：`StreamRegistry`（`Record<sessionId, StreamState>`），每個進行中對話各自持有 `AbortController` + 累積樹 + status。串流回調以函數式更新「對應 sid」的 entry，與「前端顯示哪個對話」無關。
*   **顯示衍生（不另存）**：ChatProvider 的 `history` = `activeStream?.tree ?? storedSession.messages ?? staticHistory`；`isSending`/`isWaiting` 衍生自 `activeStream.status`；`streamingSessionIds` 供側欄顯示生成指示（`Loader2` spinner）。
*   **防污染＝per-session 隔離**：chunk 只寫進自己 sid 的累積樹，碰不到別的對話（取代早期「切換即 abort」的防污染）。切換對話**不再中止**串流。
*   **完成寫回**：typed terminal 後經 writer conditional 補存完整樹，saved(full)才清 registry；failed／unknown 保留富樹及恢復入口。
*   **並發上限**：`MAX_CONCURRENT_STREAMS = 5`（模組級 SSOT）；超過時 `startStream` 回 `false`，呼叫端 toast 提示先停其一。
*   **重整後恢復（ADR-0081 游標續傳）** ⭐：後端 `StreamBufferService` 為進行中串流維護伺服器端緩衝（生成與連線解耦——重整/斷線後生成續跑）。前端：
    - `startStream` **先以 `buildUserTurnTree` 從受控 current 樹追加 user 回合**，await writer saved(full) 才開串流——重整後 user 訊息不丟、resume 有重建基底。
    - 掛載時 `resumeActiveStreams()`：`GET /v1/ui/streams/active` → 對每條進行中串流以 `resumeChatStream`（走 `fetchTouka`，FETCH_SSOT 合規）從 `starting_after=-1` 重播重建（共用真 UTF-8 decoder／typed terminal 驗證）→ 側欄 spinner 隨之恢復。
    - 持久化所有權：後端 conditional 保存 text → terminal revision → 前端 conditional 補存 full；trim使用自己的基底CAS，不覆蓋新富樹。前端未接回時僅有已確認的後端保存。
*   **選定對話續接**：URL 選定 session 等清單載入後查一次 `/stream/state`，可接回 TTL 內已完成項目；不 fan-out／poll。
*   **已知限制**：server 重啟＝進行中生成與緩衝俱滅（in-memory）；完成後緩衝保留 10 分鐘，過期靠已持久化歷史（不重播）；直連供應商串流與 DebugAgent 模擬不在恢復範圍。
*   **職責切分（SOLID-SRP）**：`stream_tree.ts`（樹變更純函式）→ `StreamRegistry`（生命週期+累積+寫回+resume 接回）→ `useChatStream`（發送前準備+委派）→ ChatProvider（顯示綁定 active）。

---

## 🏗️ 響應式佈局架構 (Responsive Layout)

系統針對不同螢幕尺寸採用了不同的渲染策略，確保無巢狀結構衝突：

### 1. 手機版 (Mobile < 768px)
*   **模式**: **Drawer-First**。
*   **導航**: 左右側邊欄統一採用 Shadcn UI `Sheet` (抽屜) 元件。
*   **觸發**: 點擊頂欄 Menu 圖示開啟。切換視圖後自動收合。

### 2. 桌面版 (Desktop)
*   **模式**: **Flattened PanelGroup**。
*   **結構**: `[Left Panel] | [Center Panel] | [Right Panel]` 處於同一層級。
*   **優點**: 調整左側寬度時不會影響右側寬度，徹底解決巢狀連動問題。
*   **折疊狀態提升 (State Lifting)**: 側邊欄的折疊狀態 `isSidebarCollapsed` 統一提升至 `page.tsx` 層級以 `useLocalStorage` 管理，使 page 為唯一折疊事實來源，並透過 `isCollapsed` 屬性單向向下傳遞給 `LeftSidebar`，消除雙重 State 的 SSOT 違規。

---

## 🔌 IDE 通訊適配 (Bridge Adapter)

所有的環境判定與訊息發送統一透過 `src/lib/bridge.ts`：
*   **`isIDE()`**: 判定目前是否運行於 VS Code 擴充功能環境。
*   **`postToBridge(message)`**: 統一訊息發送入口。
*   **`bridgeApplyArtifact(...)`**: 針對 Artifact 的環境感知型套用邏輯。

## ⚙️ 模組化設定架構 (Modular Settings Architecture)

系統的設定頁面 (`SettingsView.tsx`) 遵循 **SOLID 原則** 進行了深度重構，消除了傳統的上帝元件 (God Component)：

1. **開放封閉註冊表 (OCP Registry)**
    *   檔案：`src/components/settings/config.ts`
    *   所有分頁 (Tabs) 的定義與圖示在此處集中管理。新增設定頁面時，只需修改此陣列，完全不需改動核心排版代碼。
2. **單一職責子元件 (SRP Components)**
    *   設定頁的各個區塊（如：一般、連線、模型、群聊權限、工具、MCP、技能、安全、SNS Bots）皆拆分為獨立的視圖元件放置於 `src/components/settings/tabs/` 中。
3. **領域資料鉤子 (SSOT Hooks)**
    *   資料的讀取 (`fetchTouka`)、儲存 (`localStorage`) 與更新邏輯不再混雜於 UI 元件中。
    *   系統透過 `src/hooks/settings/` 內專屬的鉤子 (如 `useConnectionSettings`, `useSecuritySettings`) 確保各領域狀態獨立互不污染。
    *   MCP P5 將 `useMcpTools`、`useMcpResources`、`useMcpPrompts` 分離；設定面由
        `McpFeatureExplorer` 依後端 `supported` capability 只顯示真實存在的
        Tools／Resources／Prompts。所有型別引用 `openapi.json` 生成的
        `src/types/api.ts` alias，不得手刻重複 wire contract。
    *   resource URI 只回傳後端 `resources/read`，前端不解析成本機路徑；binary
        只顯示 MIME／大小。prompt preview 固定顯示「不可信外部內容」警告，不能當
        system instruction。
    *   MCP P8-A 的 `useMcpOAuth` 是 OAuth UI 資料與生命週期 SSOT：只經 `fetchTouka`
        呼叫 start/status/disconnect；`McpOAuthStatus`、`McpOAuthSelection` 與 popup controller
        分離，避免把 OAuth 狀態塞回 `McpServerItem` 成為上帝元件。
    *   popup correlation 只留 React component memory；禁止 URL、localStorage、sessionStorage、
        tree/history 與 console。只接受 exact proxy origin、exact popup `source` 且 payload 精確為
        `{type:"touka:mcp-oauth", operationId, status}`；收到訊號後仍以 authenticated status API
        作權威確認。duplicate connect／server switch／unmount 必須取消舊 polling loop並關 popup。
    *   authorization URL 僅用於 `window.open`，不得渲染、記錄或持久化；callback 頁先清除當前
        history entry，再關閉 popup。授權伺服器／瀏覽器自行管理的跨來源 history 不在 Touka
        可控制範圍。
4. **RBAC 開放式授權**
    *   `RbacMatrix` 的原生工具、MCP 伺服器與技能三軸共用同一 toggle＋明確清單元件。
    *   toggle 開啟寫入 `["*"]`（包含未來項目）；關閉寫入 `[]` 並解鎖明確清單。所有角色更新必須採不可變更新，不得直接修改傳入的 `permissions.roles`。

---

## 🏗️ 核心 Hook: `useSession()`

對話系統的核心邏輯封裝在 `SessionProvider` 中，透過 `useSession()` 勾子暴露給所有 UI 組件。

### 1. 暴露狀態 (State)
| 狀態名稱 | 類型 | 說明 |
| :--- | :--- | :--- |
| `activeSessionId` | `string` | 當前選中的對話會話 ID |
| `history` | `TreeHistory` | 樹狀訊息結構。 |
| `isSending` | `boolean` | 是否正在發送/等待 AI 流式回應。 |

### 2. 數據流與 SSOT
系統遵循 「後端持久化為最終事實」原則，但前端維持反應式快照。
*   **路由 SSOT**: `?view={mode}&session={id}` 為當前視圖狀態的唯一 URL 表徵。

### 3. 安全上下文 (Security Context) ⭐
系統現在落實「環境感知型」權限控制。發送訊息時，`useSession` 會自動注入當前身分的安全上下文：
*   **auth_level**: `Manual | Auto | YOLO`（ADR-0246）。存在對話上、預設 Manual，使用者在 `AuthSwitcher` 切換；存檔時後端判定授權（不是管理員或該靈魂的擁有者就存成 Manual），舊值讀取時對照（ReadOnly、SafeAuto→Manual，FullYOLO→YOLO）。無痕對話一律 Manual。送出訊息時帶的 `auth_level` 不是後端的模式來源。
*   **角色感知**: 前端會根據 `/v1/identity` 的映射結果，動態調整工具面板的可見性與可用性。

### 4. 工具執行安全 (HITL Enforcement)
當工具狀態為 `CONFIRM` (需人工確認) 時，對話流會暫停並渲染專屬的 **Approval Node**，使用者必須點擊確認後，指令才會發送至 `ToukaProxy` 執行。

- `ToolApprovalCard` 的參數換行完整顯示（`command` 後端最多送 8 KB），不再單行省略。互審 `request_review` 要跑的驗證指令各自是一個步驟（`call_id`＝`<request_review 的 call_id>:verify:<n>`），所以 Manual 下會先後出現兩張卡（ADR-0246）。

#### MCP multi-round interaction（ADR-0205）

- `mcp_interaction_request` 事件建立獨立 `ToolStep`，`McpInteractionCard` 只渲染
  local interaction ID、訊息與扁平 primitive JSON Schema form；上游 continuation
  state 永遠不進前端 tree/history。
- 資料提交由 `useMcpInteraction` 封裝：accept 傳 typed payload，decline 與 cancel
  是不同動作；resolved 事件收斂卡片終態，deadline 倒數僅供顯示，後端才是逾時 SSOT。
- 數值／布林 enum 必須保留原始 JSON 型別；巢狀／composition schema 在 Proxy
  投影前拒絕，前端不得猜測或降格成自由文字。

## 🗂️ 自主任務看板介面 (Mission Board UI Contract) ⭐
前端控制台支援自主任務看板的實時渲染與互動，對齊後端的 Task 模型與 dispatch 協議：
1. **任務進度感知與標籤渲染**：
   - 當對話流程中觸發 `mission_tools` 時，前端會解析並在聊天流中渲染任務看板狀態（Mission Tab 或看板面板）。
   - 支援 `Task` 擴展欄位渲染：
     - `verification_cmd`：顯示各子任務的自動化驗證命令（如 `pytest`），提供複製與快速執行的快捷按鈕。
     - `boundary_rules`：顯式列出任務需要遵循的法律規則徽章（Bridges/Badges），如 `NO_HARDCODED_PATHS`。
     - `test_log`：若任務有測試執行輸出，提供程式碼折疊區塊 (Accordion) 供使用者查看 stdout 與測試通過/失敗詳情。
     - `blocker_detail`：針對 `BLOCKED` 狀態的任務，顯式標註其阻礙異常訊息。
2. **多產出物展示 (Artifact List)**：
   - 前端型別與元件（`TaskViewer` 等）已適配 `artifact_ref` 的多元結構（支援單一 URI 或 list 陣列）。
   - 顯示任務更動的所有實體檔案連結，點擊可直接在 VS Code 虛擬編輯器中開啟或在前端 Artifact Tab 中預覽。

---

## 📜 介面合約主導開發 (Contract as Code) ⭐
前端型別系統已全面由後端 OpenAPI 合約驅動：
1. **型別來源**：`src/types/api.ts` 是與後端通訊的唯一事實來源。
2. **自動同步**：透過執行 `npm run sync-api` 獲取最新的後端 Schema。
3. **API 守衛 (Parity Guard)**：透過 `guard_frontend.ts` 自動檢查所有 `fetchTouka` 調用，確保與後端端點強對齊（包含 `/v1` 前綴標準）。
4. **編譯時鎖定**：嚴禁手動定義與後端端點相關的 Interface。這確保了當後端模型變更時，前端會在編譯階段立即反映錯誤。

---
### 3. 配置主權 (Env SSOT)
- 前端嚴禁直接讀取 `process.env`，所有環境變數與全域常數必須透過 `lib/config.ts` 進行引用，確保配置的單一入口。
- **Cookie Secure 預設生產安全 (P15 fix)** ⭐：後端 `SECURE_COOKIES` 預設值已改為 `True`。前端 auth 流程的 refresh cookie 在生產環境要求 HTTPS。本機 HTTP 開發需在 `.env` 明確設定 `SECURE_COOKIES=false`。

### 4. HTTP 客戶端主權 (Fetch SSOT) ⭐
- **唯一授權客戶端**：前端所有 HTTP 請求必須透過 `src/lib/connections.ts` 的 `fetchTouka(proxyUrl, path, options?)` 發送。
- **覆蓋範圍**：`src/hooks/**`、`src/lib/**`、`src/app/**`、`src/contexts/**`。`src/components/**` 層則由 `guard_hooks.ts` 負責偵測（DIP 守衛）。
- **靜態執法**：由 `guard_fetch.ts` 進行 AST 掃描，整合至 `guard_all.py`。
- **合法豁免**：`fetchTouka` 核心定義、多供應商 SSE 串流、外部完整 URL 入參三種情境允許使用 `// noqa: FETCH_SSOT`，須登記於 `NOQA_REGISTRY.md`。
- **架構依據**：[ADR-0024](adr/0024-fetch-ssot-guard.md)
- **Header 注入消毒 (M5 fix) ⭐**：任何寫入 HTTP header 的使用者控制值**必須**先通過 `sanitizeHeaderValue()`，剝除 `\r`、`\n`、`\x00`。目前已套用至 `X-Touka-Soul-ID`，避免 HTTP Response Splitting 攻擊。

### 5. localStorage 存取主權 (localStorage SSOT) ⭐
- **唯一儲存定義**：`src/hooks/useLocalStorage.ts`；領域 Hook（如 `useConnectionSettings`）透過此接縫存取。SNS 的 `useSnsBotSettings` 只用記憶體，禁止持久化或讀回舊憑證（ADR-0224）。
- **覆蓋範圍**：`src/hooks/**`、`src/lib/**`、`src/app/**`、`src/contexts/**`（`src/components/**` 已由 `guard_hooks.ts` 覆蓋）
- **靜態執法**：由 `guard_localStorage.ts` 進行 AST 掃描，整合至 `guard_all.py`
- **白名單**：`useLocalStorage.ts`（SSOT 定義本體）、`connections.ts`（Auth Token 管理，非 React 函數環境）
- **合法豁免**：Theme SSOT 初始化（純字串/CSS 同步需求）、純字串值儲存（`useLocalStorage` 的 JSON 序列化不相容）允許 `// noqa: LOCALSTORAGE_SSOT`，須登記於 `NOQA_REGISTRY.md`

### 5.5 身份主權前端靜態守衛 (X-Agent-ID Identity Guard) ⭐
- **禁止手動注入身份**：前端程式碼中禁止手動設置 `X-Agent-ID` Header。所有 Agent 身份識別與權限判斷必須經由後端解析 JWT Token payload 獲得。
- **靜態執法**：由 `guard_xAgentId.ts` 進行 AST 掃描，找出 PropertyAssignment 與 ElementAccess 中的大小寫無關 `"x-agent-id"`。已整合至 `guard_all.py` 第 13 道關卡 (Guard #13)。
- **合法豁免**：支援 `// noqa: IDENTITY_SSOT` 豁免，須登記於 `NOQA_REGISTRY.md`。
- **架構依據**：[ADR-0039](adr/0039-x-agent-id-identity-guard.md)

### 5.6 串流 Token 認證守衛 (EventSource Token Guard) ⭐
- **串流 Token 傳遞**：由於原生 `EventSource` 不支援自定義 Headers，建立連線時必須將 `token` 以 URL Query 參數的形式攜帶（如 `?token=${token}`），以完成安全身份驗證。
- **靜態執法**：由 `guard_eventSource.ts` 進行 AST 掃描，檢查所有 `new EventSource(...)` 實例的 URL 表達式或其 Identifier 變數 initializer 中是否包含 `"token"`。已整合至 `guard_all.py` 第 14 道關卡 (Guard #14)。
- **合法豁免**：不含敏感資料且無須認證的連線（如本地 LSP 探針）允許 `// noqa: EVENTSOURCE_SSOT`，須登記於 `NOQA_REGISTRY.md`。
- **架構依據**：[ADR-0040](adr/0040-eventsource-token-query-guard.md)

---
### 6. 對話剪斷匯出（來回計算 SSOT）⭐

`splitSessionMessages(session, keepCount=30)`（`src/lib/export-utils.ts`）的語意於 2026-05-21 變更：

- **舊**：`keepCount` = 訊息節點數（user+assistant 各算 1，30 nodes ≈ 15 輪）
- **新**：`keepCount` = 來回數（user+assistant pair = 1 round，30 rounds = 60 nodes）

輔助函式：
- `countRounds(messages)` — 計算來回數（TypeScript SSOT，與後端 `count_rounds_from_linear` 邏輯一致）
- `splitByRounds(messages, keepRounds)` — 以來回為單位分割，保證不切斷 pair

**參考**：ADR-0036

*Updated to Sliding Window, Round Count SSOT (2026-05-21)*

## 🖱️ UI 交互與體驗準則 (UX Sovereignty) ⭐

為了確保複雜交互下的狀態一致性，前端元件遵循以下設計準則：

### 1. 下拉選單雙重確認 (Double-Confirmation Dropdowns)
當下拉選單項需要「二次確認」（如刪除操作）時：
- **事件攔截**: 必須使用 `onSelect` 替代 `onClick`。
- **防止關閉**: 在切換至「確認狀態」的點擊中，必須呼叫 `e.preventDefault()` 阻止選單自動收起。
- **狀態持久化**: 僅在最後一次確認點擊時執行實際動作，並允許選單正常關閉。
- **狀態重置 (State Reset)**: 必須監聽 `DropdownMenu` 的 `onOpenChange` 事件。當選單關閉時（`open === false`），即時重置選單內部的臨時狀態（如將刪除確認 `deleteConfirmId` 設為 `null`），防止下次開啟時殘留上一次的確認紅框，維持 UX 韌性。

### 2. 換行保留 (Newline Preservation)
- 所有訊息渲染組件必須強制設定 `whitespace-pre-wrap`，確保 Agent 輸出的結構化文本（如代碼塊、列表）縮排與換行 100% 還原。
