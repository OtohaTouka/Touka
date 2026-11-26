# ADR-0333：子代理在專用空間的專案裡跑指令（窄開關）

- 狀態：✅ 已實作（融合段 3）。一頁計畫第 3.1 版由維護者核准（2026-11-26，總覽板第 113 項全部照建議：核准開工、YOLO 一起放寬、對抗式複審一次）。
- 日期：2026-11-26
- 決策者：維護者
- 相關（本 ADR 修訂）：
  - [ADR-0246](0246-model-layer-stage-2b-2c.md) 決定 8：全域 `headless_terminal` 不變，另加一個只給這種子代理的窄例外。
  - [ADR-0245](0245-model-layer-stage-2a.md) P4：Subagent 的工具不再只照抄 Guest，多三個指令工具。
  - [ADR-0309](0309-subagent-project-and-read-only.md)：子代理看得到專案之外，在窄開關下也能在專案裡跑指令。
  - [ADR-0332](0332-dedicated-project-space.md) 決定 2：「有人在場」對專用空間的專案改看 `commands_attended`。
  - [ADR-0134](0134-in-process-subagent-dispatch.md)：「子代理一律沒人在場」對指令有一個例外。
- 其他相關：[ADR-0318](0318-stop-timeout-failure-made-clear.md)（子代理跟著停止）、[ADR-0269](0269-advertised-tool-list-enforcement.md)（只執行這一輪列出的工具）、[ADR-0331](0331-complexity-ratchet.md)（複雜度棘輪）
- 影響範圍：
  - 判斷：`utils/presence.py`（`subagent_project_commands`、`commands_attended`、四個 ctx 欄位名、`TURN_STREAM_ID` 與 `PARENT_TURN_ENDED`）；新 `services/parent_turn.py`（`current_stream_id`、`turn_active`、`parent_turn_active`、`subagent_turn_over`、`ParentTurnEndedError`）；`services/stream_buffer_service.py`（`active_stream_id`、`is_stream_active`）；`services/dispatcher.py`（建好串流緩衝後把編號寫進這一輪的 ctx）
  - 政策：`models/safety.py`（`SUBAGENT_DENY_PREFIX`、`SUBAGENT_COMMAND_TOOLS`、`_subagent_command_denial`）；`services/isolation/policy.py`（`commands_attended`、子代理只在專用空間的專案、`denial_text` 的子代理說法）；`middleware/layers/tool.py` 與 `services/tool_executor.py` 的政策 environment
  - 審查員：`services/auto_reviewer.py`（`ReviewRequest.subagent_command`）；`services/tool_executor.py`（記帳身分、擋下次數的鍵、deny 的建議）；`services/approval_flow.py`（`auto_review_denied_error` 的 `suggestion`）
  - 終端：`tools/terminal.py`（`commands_attended`、子代理不建個人空間、開指令前再確認、子代理不走舊審批通道、`stop_agent_commands(..., error=)`、前景被收掉的原因）；`services/process_registry.py`（代理人登記表加鎖）
  - 子代理：`tools/subagent_tools.py`（`SubagentScope`、`_subagent_scope`、`_needs_turn`、`_TurnCommands`、監看、結束時一律收指令、`COMMANDS_NOTE`、串流編號從派工工具一路傳下）；`tools/builtin/schemas.py`（`dispatch_subagent` 說明）
  - 權限：`ToukaProxy/config/permissions.json` 與 `.example`（Subagent 加 `execute_command`、`get_command_result`、`stop_command`）
  - Guard #45 基準：`scripts/native_tool_contract_baseline.json`

## 背景

第 7 項驗收要照 ToukaDev 分工：子代理寫程式、自己跑測試看到紅燈與綠燈，靈魂再審、自己重跑。段 2（ADR-0332）之後靈魂能在專用空間的專案裡跑指令，子代理不行：Subagent 角色沒有指令工具，而且子代理一律算沒人在場，專用空間的專案沒人在場時不進執行器（`project_unattended`），Auto 審查員在沒人在場時也不審指令（`headless_terminal` 預設關）。盤點時維護者選了「做段 3 的窄開關、用 Auto 審查員」（2026-11-06，第 32 項 C2），不打開全域 `headless_terminal`（心跳、SNS 會一起放寬）。

## 決定

### 1. 窄開關：派出時判斷一次

`presence.subagent_project_commands(parent_ctx, dedicated=, read_only=, stream_id=)`，下面全部成立才為真：

- 派它的那一輪有人在場（`is_human_present`：聊天端點的對話，不是心跳、SNS、子代理、派工）；
- 父 ctx 有 `session_id`（停止監看看得到；IDE 沒選對話時沒有）；
- 那一輪有回覆串流（`stream_id`；沒開串流的 API 腳本沒有，也沒有停止鈕）；
- 不是無痕；派出時的模式是 Auto 或 YOLO（Manual 每條要按卡，子代理那邊沒有卡）；
- 專案在專用空間，而且結構層通過（`structural_scope(...).kind == "ok"`；路上有連結、第二層以下這類每條指令都會被拒的形狀不打開）；
- 不是只能看的子代理。

`subagent_tools._subagent_scope` 在派出時算一次，存進 `SubagentScope.project_commands`；`_mark_subagent_run` 把四個欄位寫進子代理的組裝與執行 ctx：`subagent_project_commands`、`parent_session_id`、`parent_stream_id`、`parent_identity`。每次都覆寫，不從父 ctx 繼承；都由伺服器寫，模型改不到。派出之後改模式不影響已派出的子代理（原本就是這樣），要停就按停止。

### 2. 只認派它的那一輪（串流編號）

- 那一輪自己的串流編號由 `dispatcher` 在建好串流緩衝後寫進這一輪的 ctx（`TURN_STREAM_ID`，伺服器寫，模型與用戶端寫不到）。`parent_turn.current_stream_id` 只認這個編號，而且它還得是這個對話正在回覆的那一輪；不是就 None。不去查「這個對話現在哪一輪在回覆」：那樣沒有串流的請求（`stream:false` 的 API 腳本）會借到網頁那一輪，被新一輪取代時舊一輪的工具也可能記到新編號（對抗式複審第 1～3 位）。
- 派工工具（`dispatch_subagent`、`dispatch_subagent_group`）一進入、第一個 await 之前就記下編號，往下傳到 `_run_subagent`。
- `parent_turn_active(ctx)`：窄開關打開，而且 `stream_buffer_service.is_stream_active(parent_session_id, parent_stream_id)`（同一個串流編號、而且還是 active）。新的一輪會換新的編號，所以這個條件只會從真變假，不會再變回真；切成 Manual 再送新訊息也一樣。對話頁的停止鈕在那一輪回覆時都在；關掉分頁不算結束（ADR-0081：那一輪照樣跑完），這段時間子代理照樣能跑，回到對話頁看得到停止鈕。
- 能跑指令的子代理一律等它做完才回（`_needs_turn`）：這一輪列了看板工具（`mission_tools`）時，`dispatch_subagent` 原本先回憑證、子代理在背景跑，你這一輪先結束的話，它的指令就被收掉。派出前先算窄開關，打開就走同步那條路（回覆說明為什麼等）；群組派工本來就等全部做完（對抗式複審第 4 位）。

### 3. 政策（兩道關卡都擋）

`safety._subagent_command_denial`（在 `_evaluate_rules` 裡、只能看那條之前）：environment 的 `subagent_run` 為真，而且是終端（`terminal_execute`）或 `SUBAGENT_COMMAND_TOOLS`（`execute_command`、`bash`、`get_command_result`、`stop_command`）時，下面任一不成立就 DENY，三種模式都一樣：

- `subagent_project_commands`（`subagent_commands:not_allowed`）；
- `parent_turn_active`（`subagent_commands:parent_turn_ended`）；
- 執行階段 `isolation == "executor"`（`subagent_commands:needs_executor`）。專用空間的專案被拒絕（`isolation_refused`）時不在這裡判，交給 `_require_isolation` 帶它自己的原因。

工具清單（`ToolLayer`）與執行（`ToolExecutor`）的政策 environment 都多三個鍵（`subagent_run`、`subagent_project_commands`、`parent_turn_active`），寫成字面量，不加分支。打開時照決策表：Auto 是 REVIEW、YOLO 是 ALLOW。底線（HUMAN_ONLY）與黑名單照舊擋下；Auto 下的動態 shell 寫法照舊只有人能核准；YOLO 下含串接符號的指令照舊要人確認。子代理沒人在場，這些一律被拒；終端裡要人確認的指令，子代理不走舊的 ToukaUI 審批通道（`terminal.execute_command`，那條路核准後不會再檢查那一輪），直接 `ui_unavailable`。拒絕的說法由 `policy.denial_text` 挑，建議一律是「不要重試，寫進報告交給派你的主代理」；專用空間的專案被拒時，子代理（角色 `Subagent`）也換成這個說法。

### 4. 在場與執行器

`presence.commands_attended(ctx)`：有人在場，或 `subagent_run` 與 `subagent_project_commands` 都是 True。`policy._decide` 與 `terminal.execute_command` 改用它，所以窄開關打開的子代理在專用空間的專案拿到 `executor`，指令在執行器裡跑，範圍和靈魂一樣（這個專案與它的暫存區）。只有 `_dedicated_level` 看 attended，專用空間以外的路徑不受影響；互審的 `evidence.py` 照舊看有沒有人能回卡。

子代理只在專用空間的專案裡跑：`policy._decide` 遇到子代理，而這次的專案根不在專用空間（派出後專案被刪、查不到、改指到別處）時直接回層級 `none`（`project_not_in_executor`），政策再以 `needs_executor` 拒絕。以前這種情況會走一般代理人的路，能不能擋住要看執行器有沒有掛子代理的個人空間（對抗式複審第 2 位、一般審查）。

子代理呼叫終端時不建個人空間的資料夾（`terminal._command_drive`）：它只在專用空間的專案裡跑，起點是專案根；以前每個跑過指令的臨時身分都會在 `data` 留一個空資料夾。

### 5. 審查員

- `ReviewRequest.subagent_command`（由 `ToolExecutor._run_auto_review` 算：終端，而且窄開關打開）：沒人在場時照樣送審，不看全域 `headless_terminal`。非指令的高風險工具照舊不送審（`_HEADLESS_TOOL`）。給審查員看的 environment 不改，`human_present` 照實是 false。
- 記帳身分：窄開關的子代理用 `parent_identity`，費用與花費上限記在派它的靈魂身上（以前會記在臨時身分 `__subagent_xxx__`，靈魂的上限管不到）。
- 擋下次數（P7）的對話鍵：窄開關的子代理用 `parent_session_id`，和派它的對話合併計算，所以多派幾個子代理不能多送審。副作用：子代理連續被擋 3 次，同一個對話裡靈魂的指令也改成跳卡，直到重開。
- 審查員判 deny 時，子代理看到的建議是交回派它的主代理（`auto_review_denied_error` 的 `suggestion`）；其他代理人的說法不變。

### 6. 停止與存活

- 按停止停住子代理（ADR-0318）已經做好：監看每 0.5 秒看父對話的停止次數，取消子代理，它的指令一起收掉；執行器那邊 relay 被殺時收到 EOF 就殺整個行程群組。
- 那一輪結束：`_cancel_when_parent_stops` 帶 `_TurnCommands`，那一輪結束後每次都呼叫 `stop_agent_commands(子代理, error=ParentTurnEndedError())`，直到子代理結束；只收指令、不取消子代理，它還能把報告寫完。政策檢查之後、等審查員期間才開的指令也收得到。收的時候出錯只記 log、下次再收，監看本身不停（停止鈕也靠它）。
- 代理人的行程登記表加鎖（`process_registry`）：收指令在執行緒裡取出登記，開指令在事件迴圈上加入；不鎖的話剛開的行程可能加進已經取出的集合，之後都收不到（對抗式複審第 3 位）。
- 開指令前再確認一次（`terminal.execute_command` 在政策通過之後）：那一輪已經結束就不開，回 `parent_turn_ended`。
- 收掉的原因要對：`stop_agent_commands` 多一個 `error` 參數，預設維持「正在重置或刪除」（`agent_busy`）；那一輪結束時背景與待審的紀錄寫 `parent_turn_ended`。前景指令被殺掉時（結束碼小於 0，而且那一輪已經結束）也改回這個原因（`_foreground_result`）。
- 子代理結束時（做完或沒做完）一律收掉它的指令：子代理身分之後不再用，還在跑的背景指令沒人能取結果（以前只有沒做完才收）。

### 7. 權限檔

`ToukaProxy/config/permissions.json` 被 git 追蹤、Docker 直接掛整個資料夾，而且測試要求它和 `.example` 整份相同，所以兩份一起改：Subagent 加 `execute_command`、`get_command_result`、`stop_command`。更新 Docker 後就生效，維護者不用自己勾。角色只是第一道；能不能真的列出與執行由上面的窄開關決定。`get_command_result`、`stop_command` 只看得到自己的紀錄（依 `agent_id` 隔離）。設定頁的權限表改的也是這個被追蹤的檔，下次更新可能因此卡住，所以要關掉這個功能時切 Manual，或改程式。

## 不採用

- 程式直接把指令工具塞給子代理：繞過權限表，權限表看不出子代理有這個工具。
- 只改 `.example`、讓維護者自己勾：CI 會紅（測試要求兩份相同），而且從設定頁存檔會弄髒主資料夾裡被追蹤的檔。
- 打開全域 `headless_terminal`：心跳、SNS 會一起放寬。
- Manual 下把子代理的卡轉到父對話：子代理多半在背景跑，父那一輪可能已經結束，卡沒有地方顯示。
- 用 `is_busy`（對話有沒有在回覆）當條件：不分哪一輪，新的一輪會讓舊的子代理重新能跑（計畫審查第 2 輪）。
- 讓子代理的行程用父 session 登記：監看已經會收，不需要。

## 已知限制

- 那一輪結束後，還沒做完的子代理仍可以用檔案工具改專案裡的檔，最長 10 分鐘，這段時間沒有停止鈕（原本就有的行為，只有指令會被收掉）。能跑指令的子代理一律同步派出，所以只會在逾時、斷線這類情況碰到。
- 開指令前再確認之後、行程登記之前那一輪結束：這條指令要等下一次收（最多約 0.5 秒加上準備的時間）。要完全關掉得在登記後再查一次，會加深已經凍結在複雜度基準的函式，沒有做。
- Auto 的審查員看的是你自己說的話：規則寫明「執行代理人自己寫的程式要轉人，除非人類原話明確要求」，子代理跑它剛寫的測試很可能被轉人，而子代理沒人能按卡，轉人就是拒絕。被擋 3 次後（和派它的對話合併計算），同一個對話裡靈魂的指令也改成跳卡，直到 Touka 重開或換一段對話。要子代理跑測試，訊息裡要寫明；要不要調整審查員或另訂做法，留給維護者決定。
- 子代理整趟最長 10 分鐘（`SUBAGENT_TIMEOUT_SECONDS`），前景指令最長 30 秒；跑測試夠不夠，第 7 項驗收時看。
- 同時派好幾個子代理改同一個專案，檔案會互相踩到；分工是靈魂的事。
- 子代理自己的模型費用記在哪沒有改（只有審查員費用改記在靈魂身上）；記成候選。
- 段 2 的風險照樣適用：專案裡的金鑰，指令看得到；執行器能上網。

## 驗證

- 新 `tests/unit/test_subagent_project_commands_security.py`：判斷矩陣（每個條件逐一拿掉）、`commands_attended`、串流編號只會從真變假、`parent_turn_active`、政策兩道關卡的三種 DENY 與被拒專案保留原因、底線與動態 shell 照擋、審查員在全域開關關著時也審子代理的指令、記帳在靈魂、擋下次數合併、那一輪結束後不審也不跑、deny 交回主代理、終端在專案裡跑而且不建個人空間、那一輪結束後不開、前景被收掉說原因、`stop_agent_commands` 的原因、監看一直收、做完也收、派工與群組在第一個 await 前記編號、整條路的工具清單（Manual、只能看、沒有串流、心跳、專用空間外都不列）。
- 審查後補的測試：只認這一輪自己的串流（含沒有串流的請求、被取代之後）、`dispatcher` 兩條緩衝路徑都寫編號（`test_dispatcher_resumable.py`）、取結果與停指令跟著開關、專用空間被拒時子代理的說法、沒有專用空間專案時不交給一般判定、子代理不走舊審批通道、監看收失敗照樣繼續、`_run_subagent` 真的建監看並記下派它的靈魂、專用空間裡被拒的形狀不打開、能跑指令的子代理一律同步派出（同步回覆說明原因）、登記表取出與加入互斥（`test_process_registry.py`）。
- 改壞驗證（先跑綠基準）：49 處逐一改壞，結果記在段 3 的進度紀錄。
- 照新行為更新的既有測試：`test_permission_matrix_baseline.py`、`test_guest_role_retirement.py`（多三個工具）、`test_subagent_stop.py`（做完也收）、`test_user_intent_presence_security.py`（沒有窄開關時任何指令都 DENY）。
- 隔離本身沒變（同一個 kind `project`），容器與 Linux 真核心測試沿用段 2。
