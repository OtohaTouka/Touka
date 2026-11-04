# Chat交班

範圍：`ToukaChat/**`：網頁介面。

這份只放這個範圍沒做完、有未解限制、做完還沒部署或要特別操作的事。總覽見 [HANDOVER](../../HANDOVER.md)，舊內容見 [封存](../changelog/handover-archive-2026-11.md)。做完一項就從這裡刪掉，紀錄寫進 [CHANGELOG](../CHANGELOG.md)。

## 現況

- **模型輸出的外部圖片不自動載入（2026-10-30，ADR-0294，PR A）**：聊天訊息與 artifact 的 Markdown 只經 `SafeMarkdown` 顯示；前端與 Touka 後端自己的圖片照常顯示（不帶 Referer），外部圖片改成「外部圖片（未載入）＋網站名稱」、點了才在新分頁開。流程圖改在隱藏的沙箱框裡畫（沿用 artifact 預覽的 CSP，畫圖當下的外部載入被擋），設定指令改不了樣式欄位，輸出再消毒掉外部網址，文字標籤現在看得到；同一張圖一次只送一個請求進框，串流時最後的完整圖不會排隊逾時（PR A2）。ToukaChat 的 `npm run dev` 只開網頁，開發終端伺服器已刪除；它用到的套件（`node-pty` 等）留到下一批依賴升級。
- **網頁版家對話不再重複送給模型（2026-10-29）**：網頁版一般（非無痕）聊天、目前對話就是家對話時，SoulLayer 不再插家對話（前端送的就是這個對話完整的線性路徑）；條件是請求帶 persistent `ui_persistence` 且路由核准 `ui_authorized`，兩者都要。子對話、SNS 家對話模式、IDE 與沒帶 `ui_persistence` 的請求照舊插入。約定寫在 PROXY／CHAT／MEMORY_INTERFACE：前端改成只送最近幾輪之前要先改後端。
- **首次設定精靈改密碼後請重新登入、登入後接續（2026-10-23，ADR-0053 補記）**：第 2 步改完密碼不再直接前進（舊卡已被撤銷，ADR-0144），改請使用者重新登入；接續點存在瀏覽器（`useSetupResume`，1 小時、用過即刪），同一位管理員登入且還有沒設定完的項目時精靈開在下一步，登入頁看到有效接續點會說明。非臨時密碼時第 2 步可「保留目前密碼」；第 7 步會重讀狀態。只改 ToukaChat；登入頁其餘文字仍是寫死的英文（原本就這樣）。尚未部署。

- **同一段對話的下一則存不進去（2026-11-04 實際驗證找到，既有問題，還沒修；維護者 2026-11-05 決定下次發版前修）**：畫面帶舊版本存檔，訊息留在輸入框，要按「重新核對」與「僅重試保存」才能繼續。兩種情況：設為家對話之後（`set_home` 換了版本，畫面不知道）；回覆結束 10 分鐘內重新整理頁面再回到那段對話（接回已結束的串流時，結尾的舊版本把新的蓋掉）。對照跑在沒有丙-2 改動的程式上。見 ADR-0295「實際驗證找到的既有問題」。
- **模型失敗之後要按兩個按鈕才能繼續（既有問題，還沒修；不擋發版）**：丙-2 只補上「這一輪回覆失敗」的標示；ADR-0231 把這種情況算成保存結果不確定，所以「儲存工作階段失敗」的通知與保存提示都還在。

## 也和這裡有關

- 實際驗證走真前端、真後端、假模型；目前走 Hook 全部事件與審批卡，先不列為必過：見 [tests.md](tests.md)。
- 管理員可設定 hook 擋下或要人確認，全部事件都接好了：見 [proxy.md](proxy.md)。
- 刪除湧現指數與端點；外部客戶端要自己拿掉這些欄位：見 [proxy.md](proxy.md)。
- 刪靈魂前先清隔離家目錄；舊 ID 要手動刪，密碼錯回 403：見 [isolation.md](isolation.md)。
- 心跳與 SNS 改每靈魂頻道；所有 writer 要一起升級：見 [heartbeat.md](heartbeat.md)。
- 不送取樣參數、沒設模型回 409、uv 冷卻期；有後續未做：見 [proxy.md](proxy.md)。
- 全停 Gemini 顯式快取、補金鑰範本；uv 映像待確認：見 [proxy.md](proxy.md)。
- 聊天要登入、Guest 退役、群聊只限靈魂；舊設定讀取時略過，群聊頁存檔會刪掉舊區塊：見 [proxy.md](proxy.md)。
- 互審指令逐條審批、卡片完整顯示；看板分不出等人核准：見 [proxy.md](proxy.md)。
- 三種模式、底線與審查員上線；繞過與準確率未解：見 [proxy.md](proxy.md)。
- AI 工作中可插話；部分廠商格式待實測，有已知限制：見 [proxy.md](proxy.md)。
- Ctrl+Enter 直接中斷這一輪；伺服器存檔不帶中斷紀錄：見 [proxy.md](proxy.md)。
- 主要模型分兩欄、模型名不分大小寫；現有值要自己確認：見 [proxy.md](proxy.md)。
- 修記憶「壞了卻顯示正常」；其餘限制列在待排小項：見 [memory.md](memory.md)。
- 演化先問人、MCP 不再拿金鑰；升級要檢查 MCP 連線：見 [isolation.md](isolation.md)。
- 記憶庫升級、向量失敗不刪事實、記憶來源；有限制與回復步驟：見 [memory.md](memory.md)。
- 對話有時間戳、舊對話直接歸檔；剪裁會開始真的生效：見 [memory.md](memory.md)。
- 模型只能叫這一輪列出的工具；舊短名靈魂要重新勾選：見 [proxy.md](proxy.md)。
- 可選擇一併清除備份裡的記憶；VPS 的 nginx 要補設定：見 [memory.md](memory.md)。
- 5.5 小工作少想、價目修正；審查員等待要手動改成 60：見 [proxy.md](proxy.md)。

## 待排小項

- `useLegacySync` 不帶參數呼叫時，預設的 `agents=[]` 每次 render 都是新陣列，載入統計的 effect 會無限重跑、一直打 API；目前唯一的呼叫端有帶參數，所以沒有發生（ADR-0253）。
- ToukaChat 與 ToukaIDE 選 anthropic 協定時寫死 `max_tokens: 4096`（`ToukaChat/src/lib/connections.ts`、`ToukaIDE/src/providers/ChatViewProvider.ts`）；Anthropic 入口現在會把它帶給非 Claude 模型，這條路的輸出會被截在 4096（Gemini 2.5 連思考 token 一起算）。預設協定是 openai，只影響改選 anthropic 的人（ADR-0254）。
- `pickDefaultModel`（`ToukaChat/src/lib/default_model.ts`）沒排除 `owned_by` 是 human／touka 的項目；兩個下拉已改用 `isSelectableBaseModel`，預設模型的挑法還沒統一（ADR-0254）。
- 模型工坊的改名在後端成功、回應卻在網路上遺失時，畫面還記著舊 ID，重新整理前存不進去（ADR-0254 2026-10-25 補記）。
- 重置卡的「將清除」清單沒有寫活動時間軸（段 4 起重置會清），要不要補由段 4 另排（ADR-0267）。
- 這輪助理節點的後端完成時間會被前端完成時間取代（前端整棵寫回，兩者差網路延遲）；要保留得讓串流終態事件帶後端時間（改合約）（ADR-0264）。
- h2 附帶發現、未修：ToukaUI 的 `openDiff`（不帶伺服器前綴）回 `resource_not_found`；ModelStudio 的工具選單是空的（端點沒回它讀的 `data.tools`）（ADR-0269）。
