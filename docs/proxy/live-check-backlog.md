# 模型溝通層實測待辦（live check backlog）

模型層 1a～3b 與 4a 全部只用 mock 與本機 loopback 驗證（[ADR-0248](../architecture/adr/0248-model-layer-stage-3b.md)、[ADR-0249](../architecture/adr/0249-model-layer-stage-4a-4b.md)）。這份清單列出下一段要對真廠商驗的事。**本檔不是授權**：每一批都會向廠商送請求、可能計費，照禁令 61（ADR-0214）在執行前把下表的揭露欄位填成精確值，取得維護者對那一批（或明列的有限批次）的同意後才跑；不得放進 pytest、Vitest、smoke runner、CI 或守衛，也不得未經同意改用備援或重試。不確定會不會計費時，一律當成會計費。

## 共同規則

- 每批揭露：①命令或 script；②provider；③精確 model id；④case 數與重試次數；⑤budget 或 token 上限；⑥送出內容類型；⑦本機寫入範圍。表內「待定」的欄位在請求同意時補成精確值。
- 通用的單次文字工具是 `tests/smoke/manual/live_chat_probe.py`（記憶評估另用 `memory_eval_probe.py`，見下方 M1／M2）：單次、固定合成 prompt「請只回覆：LIVE_PROBE_OK」、不帶工具、輸出上限 1～128、主迴圈 `MAX_SEND_ATTEMPTS=1`（retry=0）、本機寫入只到程序專屬 sandbox。命令格式：`python tests/smoke/manual/live_chat_probe.py --provider <id> --model <精確 id> --max-tokens <1..128> --human-approved-live-api [--env-file <金鑰 .env>]`。
- 帶工具、長思考、取消等情境要新寫人工 probe：放在 `tests/smoke/manual/`、檔名不符合自動 discovery、在任何 `src` import 前啟用 sandbox（禁令 63），寫好後先在本檔補上精確命令再請同意。
- 同一批內不自動重試；遇到 429／5xx 就停下回報，不換模型、不換廠商。

## 批次

| 批 | 要驗的事 | ①命令／script | ②provider | ③model | ④case／重試 | ⑤上限 | ⑥送出內容 | ⑦本機寫入 |
|---|---|---|---|---|---|---|---|---|
| **L1** Gemini 自寫串流 | thoughtSignature 跨工具輪原樣回送；SSE 是否用 CRLF、usageMetadata 是否每個 chunk 都帶（重建 fixture 是推測，ADR-0248）；最後一個 chunk 的 finishReason 與用量；長思考時第一個事件前的等待時間，以及首事件之後、長思考期間的最長位元組間隔（閒置 60 秒是推測值） | 新寫工具輪 probe（待寫）；單次文字可用 `live_chat_probe.py --provider gemini` | Gemini | 待定（例：`gemini-2.5-flash`） | 文字 1＋工具輪 2 請求；重試 0 | 每請求輸出 ≤128（工具輪 ≤256）；總請求 ≤3 | 合成 prompt＋1 個假工具 schema；不含真實對話 | 程序專屬 sandbox |
| **L2** DeepSeek／Grok 相容串流 | 是否收 `stream_options.include_usage`、用量在哪個 chunk；`reasoning_content` delta；工具 index；第一個事件前的等待時間；首事件之後（例如只送 role chunk 之後）長思考期間的最長位元組間隔；KF-8 修正後仍收得下 | `live_chat_probe.py`（文字）＋新寫工具輪 probe（待寫） | DeepSeek、Grok（各一批） | 待定 | 每家文字 1＋工具輪 2；重試 0 | 每請求輸出 ≤128；每家 ≤3 請求 | 同 L1 | 同 L1 |
| **L3** Claude 串流 | 長思考時閒置 60 秒（推測值）不誤觸；ping 實際間隔；取消後連線關閉、取消的輸出計不計費；`max_tokens` > 21,333 串流不被壓；default_on 思考型號的 8,192 下限（ADR-0247） | 新寫長思考與取消 probe（待寫） | Anthropic | 待定（需 default_on 思考型號一個） | 長思考 1、取消 1、大上限 1；重試 0 | 大上限那筆輸出上限待維護者定（最貴的一筆，另列金額） | 合成長推理題；不含真實對話 | 同 L1 |
| **L4** 重試 | 只在自然遇到 429／529 時觀察 `retry-after`／`retry-after-ms` 與退避，不做壓力觸發 | 不另跑；只看 L1～L3、L5 執行中的日誌 | — | — | 0 | 0 | — | — |
| **L5** OpenAI Responses | 真 API 事件形狀與 `: ping`；回送鍵集合（含 `status` 等只出現在輸出的欄位）會不會被拒；加密 reasoning 第 2 輪回送；completed 沒帶 output 的情況；第一個事件前的等待時間，以及 `response.created` 之後、長思考期間的最長位元組間隔（R-3b ② M2：生命週期事件之後是否沉默） | `live_chat_probe.py --provider openai`（文字）＋新寫工具輪 probe（待寫） | OpenAI | 待定（規則表標 `uses_responses_api` 的型號） | 文字 1＋工具輪 2；重試 0 | 每請求輸出 ≤128 | 同 L1 | 同 L1 |
| **L6** Claude 插話格式 | 規則表 `mid_conversation_system=True` 的型號（Opus 5、Opus／Sonnet／Haiku 5.5、Opus 4.8、Fable、Mythos 5／5.1；Mythos Preview 為 False）在工具結果之後收 system 訊息會不會被拒、模型有沒有照做；Haiku 5.5 的官方資料兩處說法不一（managed-agents 文件列為支援、prompt-caching 頁沒列），規則表暫列 True，要實測；其他型號在 tool_result 後加 text block 的反應；urgent 那一輪 `tool_choice=none` 時是否先用文字回應 | 新寫插話 probe（待寫） | Anthropic | 待定（兩種規則各一個型號） | 每型號工具輪 2 請求；重試 0 | 每請求輸出 ≤256 | 合成 prompt＋1 個假工具 schema＋合成插話句；不含真實對話 | 同 L1 |
| **L7** 相容、Gemini、Responses 插話格式 | tool 訊息後的 user 訊息（DeepSeek、Grok）、functionResponse 同一個 content 的 text part（Gemini）、function_call_output 後的 user input item（Responses）會不會被拒、模型有沒有看見 | 同 L6 | DeepSeek、Grok、Gemini（Responses 未安排，只列待辦） | 待定 | 每家工具輪 2 請求；重試 0 | 每請求輸出 ≤256；每家 ≤2 請求 | 同 L6 | 同 L1 |
| **L8** 打斷後的訊息形狀與計費 | 串流中途取消後，接在原本提問（或上一批工具結果）之後的插話訊息會不會被拒：Claude 併進同一則 user 訊息、OpenAI 相容另起 user、Gemini 加 text part；取消那一輪的用量回報與計費（併入 L3 的取消觀察）；沒有 tools 的請求不送 `tool_choice` 是否被接受；工具邊界插話後又被打斷時，Claude 併成一則中途 system 是否被接受 | 新寫打斷 probe（待寫） | Anthropic、DeepSeek、Grok、Gemini（各一批；Responses 未安排） | 待定 | 每家串流中途取消 1＋續送 1；重試 0 | 每請求輸出 ≤256 | 同 L6 | 同 L1 |

**L5 目前未安排**，只列待辦；GPT 登入不在下一段。

## 記憶長期穩定性（M1／M2）

工具：`tests/smoke/manual/memory_eval_probe.py`（情境與規則判定在 `tests/memory_eval/`，pytest 只跑離線部分）。合成靈魂（不是 DebugAgent）、合成事實與對話、注入句；主迴圈 `MAX_SEND_ATTEMPTS=1`；四家 send_round 外連前依序核對輸出上限（超過或缺欄就不送）、檢查「已花＋這個請求最壞花費」不超過 `--budget`、核銷請求預算；不換模型；摘要員、審查員一律不呼叫（記成被拒）；任何一輪出錯（含 429／5xx）、最壞花費會超過上限、請求數用完或累計估計金額到 `--budget`，整批停下。本機寫入只在程序專屬暫存沙盒（結束時先放開紀錄檔與資料庫連線再刪除，刪不掉以非零結束），結果只印到 stdout（含每個請求的輸入、快取、輸出含思考 token）。金額照內建價目表估計【推測】。M1 每列是一個同意單位。

ADR-0268 起輸出上限改成比對 Touka 實際要送的值（Anthropic 照規則表，5.5 這類會思考的型號是 8,192；其他廠商照請求值），工具天花板 8,192；核准範圍的上限欄由 `max_tokens_per_request` 改成 `max_tokens_requested`、`wire_max_tokens_per_request`、`wire_max_tokens_ceiling`，另加 `wire_rule`、`pre_send_worst_case`、`worst_case_usd_per_mtok`。M1 四批的同意都針對 ADR-0268 之前的版本（每請求輸出 1..1,024、只在每輪開始前檢查累計金額）；下表 M1 四個型號 Touka 都不拉高輸出上限，實送值等於 `--max-tokens`。

| 批 | 要驗的事 | ①命令／script | ②provider | ③model | ④case／重試 | ⑤上限 | ⑥送出內容 | ⑦本機寫入 |
|---|---|---|---|---|---|---|---|---|
| **M1-a** | 記憶功能改動前的基準：人格與抗改名、跨 session 召回、記憶衝突、惡意記憶三路徑（查記憶、前情摘要、家對話）、經過時間（3 小時、3 天、30 天）、摘要寫「昨天」實為 10 天前、預載 100 輪後召回 | `python tests/smoke/manual/memory_eval_probe.py --provider deepseek --model deepseek-v4-flash --suite smoke --max-provider-requests 40 --max-tokens 512 --budget 0.20 --human-approved-live-api [--env-file <金鑰 .env>]` | DeepSeek | `deepseek-v4-flash` | smoke 11 情境、預計 20 請求；重試 0、備援 0；摘要員／審查員／評審 0 | 請求硬上限 40；每請求輸出 ≤512；USD 軟上限 0.20；估約 $0.04、滿 40 約 $0.09 | 合成人格、合成事實與對話、注入句；`memory_tools`／`mission_tools` 等工具 schema；不含真實對話或人類資料 | 程序專屬暫存沙盒；結果 stdout |
| **M1-b** | 同上 | 同上換 `--provider gemini --model gemini-2.5-flash --max-tokens 1024 --budget 0.30` | Gemini | `gemini-2.5-flash` | 同上 | 硬上限 40；輸出 ≤1,024（含思考【推測】）；0.30；估約 $0.07、滿 40 約 $0.17 | 同上 | 同上 |
| **M1-c** | 同上 | 同上換 `--provider grok --model grok-4.20-0309-non-reasoning --budget 1.30` | Grok | `grok-4.20-0309-non-reasoning` | 同上 | 硬上限 40；輸出 ≤512；1.30；估約 $0.48、滿 40 約 $1.03（價目表落 grok-4 的 $3/$15，可能高估） | 同上 | 同上 |
| **M1-d** | 同上 | 同上換 `--provider anthropic --model claude-haiku-4-5 --budget 0.50` | Anthropic | `claude-haiku-4-5` | 同上 | 硬上限 40；輸出 ≤512；0.50；估約 $0.16、滿 40 約 $0.34（未計快取） | 同上 | 同上 |
| **M2** | 完整批：多次取樣、真跑 100 輪、換模型、心跳恢復 | 待定（記憶功能合併、加情境後補精確命令） | 待定（每家一批） | 待定 | 待定 | 待定 | 同 M1 | 同 M1 |

## Claude 5.5 少想的延遲（T1，ADR-0271）

工具：`tests/smoke/manual/thinking_latency_probe.py`（**寫好、未執行**）。直接呼叫產品 `complete_once`：minimal＝用途 summarizer（用途表少想）、default＝用途 critic（模型預設），啟動時核對用途表；只收三個 5.5 型號且路由是 Anthropic；`complete_once` 不重試、不換廠商。每個請求外連前依序核對實送上限 → 已花＋最壞（該型號價，Haiku 5.5 取貴檔）≤ `--budget` → 核銷請求數；任何一個請求失敗整批停。報告每請求的實送上限、思考設定、耗時、四欄用量與金額，每組最小／中位數／最大耗時；不印回應原文。目的是確認少想實際快多少、審查員等 60 秒夠不夠（ADR-0271；60 秒是推測值，不實測也安全）。

| 批 | 要驗的事 | ①命令／script | ②provider | ③model | ④case／重試 | ⑤上限 | ⑥送出內容 | ⑦本機寫入 |
|---|---|---|---|---|---|---|---|---|
| **T1** | 三個 5.5 型號在少想與模型預設下的耗時、輸出（含思考）token 與金額 | `python tests/smoke/manual/thinking_latency_probe.py --models claude-haiku-5-5,claude-sonnet-5-5,claude-opus-5-5 --profiles minimal,default --repeats 5 --max-provider-requests 30 --max-tokens 1024 --budget 3.0 --human-approved-live-api [--env-file <金鑰 .env>]`（格式，不代表核准） | Anthropic | `claude-haiku-5-5`、`claude-sonnet-5-5`、`claude-opus-5-5` | 3 型號 × 2 強度 × 5 次＝30 請求；重試 0、備援 0 | 請求 ≤30；請求裡 `max_tokens` 1,024，實送 Haiku／Sonnet 5.5 少想＝1,024、其餘 8,192（天花板 8,192）；USD 上限 3.0；工具送前最壞估算合計約 $2.77（輸入估約 6,800 token），一般約 $0.4【推測】 | 一段固定合成段落（約 2,000 token）＋一行 system 指示；不帶工具、不含真實對話 | 程序專屬暫存沙盒；結果 stdout |

## planA §10 的其他批

| 批 | 要驗的事 | 備註 |
|---|---|---|
| P1 基線對照 | 同一天各跑一次 `cc68e3a9` 與整合分支的 Claude、Gemini 帶工具迴圈（ADR-0242 決策 2 改成新舊版本同日對照） | 揭露欄位同上；兩個版本各算一批 |
| P2 健康檢查 | KF-6 修正後三家健康檢查各一次 | 每家 1 請求 |
| P3 結構化輸出 | `complete_once` 與 Auto 審查員候選模型的結構化輸出 | 單次、不帶工具，`complete_once` 本身不重試 |
| P4 Docker | 三個映像完整 `docker build`（含 openai 依賴與 uv 0.12.1 的 `uv sync --locked`） | 不打模型 API，但會連 registry 與套件來源 |
