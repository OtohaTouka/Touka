# `tests/tool_eval/` — 工具調用 eval 骨架（scaffold）

量化「AI agent 調用原生工具的可靠度」，讓「工具重設計」（見
[`docs/architecture/proposals/native-tool-interface-redesign.md`](../../docs/architecture/proposals/native-tool-interface-redesign.md)）
的每一步都能用**數字**驗收，而不是靠感覺。

> 這是**骨架**：預設 OFFLINE（replay）模式可跑、不需金鑰、CI 綠燈；接真實 proxy 的 LIVE 模式是留好的縫（見下）。

## 檔案結構

| 檔案 | 作用 |
|---|---|
| `cases.py` | `EvalCase` / `ToolCall` 資料模型 + `SEED_CASES`（用真實工具名的種子案例） |
| `metrics.py` | 純函式評分：`score_case`（單回合）/ `score_episode`（多回合）/ `aggregate` / `format_report` |
| `harness.py` | `run_case` / `run_episode` / `run_suite`；`known_builtin_tool_names()`；LIVE 縫 `_run_case_live` |
| `test_tool_eval.py` | offline 綠燈測試（自我驗證指標計算） |
| `test_multi_turn.py` | 多回合語義測試（探索中性、turns_to_success、live 迴圈協定） |

## 指標（fractions 0..1）

| 指標 | 意義 |
|---|---|
| `tool_selection_accuracy` | 有期望工具的案例中，正確選到（且沒碰禁用工具）的比例 |
| `wrong_tool_rate` | 呼叫了「真實但非期望」工具的比例（**探索中性**：read/list/find/grep 非目標呼叫不計——多步規劃的「先看再做」不是選錯，但 selection 仍須在回合內達標） |
| `invalid_param_rate` | 缺少該工具必填參數的呼叫比例 |
| `hallucinated_tool_rate` | 呼叫了「不存在於 known 集合」的工具（幻覺）比例 |
| `tool_error_rate` | 回傳 status=`error` 的呼叫比例 |
| `policy_denial_accuracy` | 應被拒的案例中，禁用工具確實**未成功執行**的比例 |
| `abstention_accuracy` | **relevance（BFCL irrelevance）**：`expect_no_tool` 案（純知識/閒聊/無對應能力）正確「零呼叫」的比例——量過度調用 |
| `avg_turns_to_success` | 達標案例的平均回合數（多回合模式；直行派＝1.00） |
| `reliability`（pass^k） | **τ-bench pass^k**：多樣本模式（`--samples N`）下，跨 k 次試驗「全部成功」的機率＝C(c,k)/C(n,k) 各案例平均。pass^1＝平均選對率、pass^n 最嚴格；**遞減幅度＝可靠度缺口**（多腦切換最有意義） |

## 工具 schema token 成本（P0-D）

`scripts/measure_tool_token_cost.py`（手動、不進 CI）：以真實 Gate 1 組廣告工具、逐工具過 token 估算器 SSOT，印由大到小排行＋總計（Claude Code「工具 schema 佔 token 大半」教訓）。

```powershell
python scripts/measure_tool_token_cost.py            # Owner ＋ Base 各一份
python scripts/measure_tool_token_cost.py --role Owner
```

## 執行

```powershell
# 先確保 .venv 是 3.12 且已 provision
uv sync --all-packages

# 只跑這個骨架（OFFLINE，不需金鑰）
.venv\Scripts\python.exe -m pytest tests/tool_eval/ -o log_cli=false -q

# 單一測試
.venv\Scripts\python.exe -m pytest tests/tool_eval/test_tool_eval.py::test_offline_suite_metrics -q
```

## 新增案例

在 `cases.py` 的 `SEED_CASES` 加一筆 `EvalCase`：

```python
EvalCase(
    id="my-case",
    title="…",
    prompt="使用者訊息",
    context={"agent_id": "DebugAgent", "role": "Owner", "auth_level": "Manual"},
    expected_tools=["research"],
    forbidden_tools=[],            # 這些不該被呼叫
    hallucination_bait=[],         # 誘餌：不存在的工具名
    required_args={"research": ["query"]},
    policy_denied=False,           # 是否為「應被拒」案例
    recorded_tool_calls=[ToolCall("research", {"query": "…", "source": "auto"})],  # OFFLINE 用
)
```

`recorded_tool_calls` 是 **replay 資料**：模型「實際做了什麼」。OFFLINE 用它做決定性評分；要跑 LIVE 就留 `None` 並改用 live 模式（見下）。多回合案例改用
`recorded_turns=[[...第1回合...], [...第2回合...]]`（兩者擇一），並可用
`simulated_results={"list": "…"}` 腳本化回合間餵給模型的工具結果。

## LIVE select-only（已實作 v1：只選不執行）

`live_select_only.py` ＋ 手動腳本 `scripts/record_tool_eval_baseline.py`：
以**真實 Gate 1（ToolLayer）**組出 DebugAgent 的廣告工具 schema → 把 case prompt
連同工具送 LIVE 模型一次 → 收集模型選了哪些工具與參數。**工具永不執行**（零風險
碰 `data/`）。v1 走 OpenAI 相容供應商（openai/deepseek/grok，端點由 provider
registry SSOT 正規化）。

> **ADR-0214 人工閘**：任何會連 provider 的命令均永久排除 pytest／smoke／CI／guard。
> 每次先向人類揭露 exact command、provider、model、cases、samples／turns、最大 request 數、
> 送出內容與 artifact 寫入範圍；取得該次同意後才可加 `--human-approved-live-api`。
> `--list-models` 雖通常不耗 token，仍是外部 API，規則相同。只有 `--dry-run` 零 API、免此旗標。

```powershell
# 只組工具、不打 API（驗證裝配）
python scripts/record_tool_eval_baseline.py --dry-run
# 人類核准後：列出供應商可用模型（外部 API）
python scripts/record_tool_eval_baseline.py --list-models --human-approved-live-api
# 人類核准後：實錄基準（消耗 token；結果落 OS temp，promotion 另行人工處理）
python scripts/record_tool_eval_baseline.py --model <model-id> --human-approved-live-api
# 多回合模式（非目標呼叫後餵模擬結果續跑；強制 samples=1、檔名 -mtN 後綴）
python scripts/record_tool_eval_baseline.py --model <model-id> --turns 3 --human-approved-live-api
# 多樣本（samples>1）→ 逐試分開評分、回報 pass^k 可靠度
python scripts/record_tool_eval_baseline.py --model <model-id> --samples 3 --human-approved-live-api
```

前置：`.env` 要有**真實**的供應商金鑰（如 `OPENAI_API_KEY`）。
目前固定 12 cases；samples／turns 的硬上限都是 4，所以單批最多 48 provider requests。
每 request `max_tokens` 預設 128、硬上限 512；結果寫入
`%TEMP%\touka_tool_eval_result_*`，腳本不自動 promotion，也不保證自動刪除此人工結果目錄。
已知限制（記錄於基準檔 meta）：builtin-only（不掛 MCP、無 memory_tools）、
system prompt 非完整 pipeline——量測的正是「schema 驅動的工具選擇」，
即 Tier 2/3 重設計的改善對象。

## LIVE episode（ADR-0136 已接通：完整 pipeline＋工具真的執行）

`live_episode.py` ＋ 手動腳本 `scripts/run_tool_eval_episode.py`：
跑**真實 in-process producer 子 loop**——重用 Phase 1（ADR-0134）組裝＝完整
ProxyPipeline（persona/soul 依身分注入）、工具真的執行（受 Gate 1/2 權限約束）、
多輪 ReAct；SSE 抽出 `tool_calls`（tool_start/tool_result join，status
ok/denied/error）＋最終文字＋**Phase 2 回報塊**（claims/artifact_refs/confidence，
ADR-0135）＋artifact 存在檢查（顯式 `--artifact-root`）。

此入口會消耗模型 token。執行身分固定為 DebugAgent 以驗證 production 身分／權限路徑，但 CLI
不寫實體 DebugAgent storage：app-data／workspace／config／artifact 全錨定本次 temp sandbox，
`--artifact-root` 必須是 sandbox 內相對路徑。單一 case；requests 預設 3、硬上限 16；每輪
`max_tokens` 預設 128、硬上限 512；wall timeout 預設 180、硬上限 600 秒。Gemini、
`research-auto` 與非 DebugAgent fail-closed。每次執行前仍須揭露 exact scope 並取得人類同意；
禁止自動 retry／fallback。

```bash
python scripts/run_tool_eval_episode.py --list-cases
python scripts/run_tool_eval_episode.py --case read-soul-owner --model gpt-5.2 --human-approved-live-api
```

harness 舊契約同步接通：`run_case(case, mode="live", live_model=..., live_limits=...)` 回
`list[ToolCall]`；真 live 的 model 與 bounded limits 都必填，測試只能走明確 fake stream DI seam。

### 互審 gate 驗收軌（ADR-0136，互審設計文件 §7.1）

- `cases.py`：注錯欄位 `injected_failure`（gate_recall 標籤）＋
  `expected_artifact_check`（oracle 機械判定規格）＋`ground_truth`（人類閱讀）。
- `metrics.py`：episode 級 `gate_recall`／`gate_precision`／`false_block_rate`
  ＋`aggregate_gate`／`format_gate_report`。**軸警告**（`HALLUCINATION_AXIS_NOTE`）：
  `hallucinated_tool_rate`／`selection_ok` 與 artifact 內容幻覺**不同軸**，
  不可作為互審降幻覺主指標。
- `oracle.py`：**非 LLM** 機械裁決（fabrication／unverified_claim／goal_drift／
  missing_artifact）——AST 裸名呼叫檢查、宣稱 vs 磁碟比對、內容規則比對；
  避免「用會幻覺的東西驗幻覺」的循環信任。

### 後續版本（尚未實作）

- **五臂 A/B**（設計文件 §7.2）：待互審子系統（Phase 3 本體）落地後接
  gate 產生 `GateOutcome`，跑 Arm A–E ＋預註冊門檻（§7.3）。
- **RECORD→replay**：把 LIVE 觀測存成 `recorded_tool_calls` 案例，自動變成
  離線回歸（套件自我成長）。

> ⚠️ 執行 LIVE 一律用 **DebugAgent** 身分；episode 版工具會真的執行，但本 CLI 的全部寫入
> 仍限制於 temp sandbox。每個 case 的 prompt、工具與寫入集合須在核准前揭露。
> Claude 原生 brain 需 anthropic_client（v1 走 OpenAI 相容供應商，同 select-only）。

## 用法：基準 → 改動 → 比較

1. 改動前先跑一次，記下六個指標當**基準**。
2. 每做完提案的一層（Tier 1/2/3），重跑、比對數字（幻覺率、選錯率應下降）。
3. 用數據決定是否上線該層。
