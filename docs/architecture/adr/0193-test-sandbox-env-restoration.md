# ADR-0193: 測試沙盒環境變數還原（單例路徑快取外洩修復＋repo 落盤哨兵）

## 狀態

✅ 已接受

## 日期

2026-07-25

## 背景

`python -m pytest tests/` 跑完後 `git status` 恆髒：

```
 M dummy_sandbox_path/config/skills/system-skill-dispatcher/SKILL.md
?? dummy_sandbox_path/config/skills/engine-smoke-warn/
```

每次都要手動 `git checkout` 還原＋清 untracked，否則雜訊混進 commit。這直接違反 AGENTS.md
「沙盒隔離：所有測試必須遵循 `conftest.py` 的沙盒邏輯」。

**根因鏈**（四環，缺一不成立）：

1. `tests/unit/test_dependency_override_{isolation,leak_prevention}.py` 以
   `setup_test_environment.__wrapped__("dummy_sandbox_path")` 直呼 fixture，繞過 pytest。
   傳入的是**字面相對路徑**，以 cwd（repo 根）解析 → `TOUKA_CONFIG_DIR = dummy_sandbox_path/config`。
2. 該 fixture **只注入、不還原** env。手動推進的產生器跑完 teardown 後，env 仍是被覆寫的值。
3. fixture teardown 的單例重置（`skill_service.reset_for_test()`）會**就地重解析並常駐快取**
   `self._skills_dir = get_config_dir() / "skills"`。此刻 env 尚未修好 → 快取住 repo 內路徑。
4. 下一個測試的 fixture setup **只修 env、修不到已快取的單例**（重置在 teardown、不在 setup）。
   於是**緊接著執行的那一個測試**若寫技能，就寫進 repo。

外洩窗口精確為「一個測試」，加上 `pytest-randomly` 隨機次序 → 表現為間歇、且殘留物隨 seed 變化
（`eval-keep` 來自 `test_creation_eval`、`engine-smoke-warn` 來自 `test_creation_engine`）。

決定性重現：

```powershell
python -m pytest "tests/unit/test_dependency_override_leak_prevention.py::test_setup_test_environment_forces_override_cleanup" `
                 "tests/unit/test_creation_engine.py::test_skill_smoke_warns_unregistered_mcp_server" -p no:randomly
```

⚠️ 前一測試若不落盤（如 `test_unknown_kind_raises`）則不重現——這正是它長期沒被抓到的原因。

`dummy_sandbox_path/` 本身經查為**純測試殘留**：全 repo 零引用（僅上述兩處字面字串），
內容全是產生物（`eval-keep` 由測試建立、dispatcher SKILL.md 由 `sync_system_dispatcher()` 重生），
且是分散在 6 個無關 feature commit 中意外累積進版控的（2026-06-19 ~ 07-12）。
對照組：repo 根的 `violations/` 是**真 fixture**（被 `tests/integration/test_static_analysis.py`
讀取、缺檔自癒），刻意保留。

## 決策與實作

**① fixture 對稱化：注入必還原，且還原早於單例重置**（`tests/conftest.py`）

`setup_test_environment` setup 先快照 `TOUKA_CONFIG_DIR` / `MEMORY_APP_DATA_DIR` /
`TOUKA_BOOTSTRAP_ENABLED`，teardown 於**步驟 4 單例重置之前**還原（新增步驟 3c）。
順序是本修復的關鍵：還原若晚於重置，快取住的仍是被覆寫的值＝零效果。

正常 autouse 路徑下新舊值相同＝no-op，既有 3783 測試零行為變動。

**② 兩測試改用 `tmp_path`**：`dummy_sandbox = str(tmp_path)` 取代字面相對路徑。
保留「模擬另一個沙盒」的測試意圖，但落盤處為 pytest 自管的可拋棄目錄。

**③ session 級 repo 落盤哨兵**（`global_test_sandbox`）：session 開始快照 repo 根目錄項目集合，
session 結束比對**增量**並 `raise AssertionError` 列出洩漏項。
- 用增量而非絕對斷言 → 對既有本地未追蹤檔（`.env`、`node_modules`、`violations/`）零誤報。
- 刻意 fail-loud 而非 warning——降級為警告等於保留「悄悄弄髒版控」這個原始問題。
- 刻意**不加 `.gitignore` 條目**：ignore 會讓回歸靜默通過，與哨兵目的相反。

**④ `dummy_sandbox_path/` 移出版控**：`git rm -r` ＋刪實體目錄。

### 變異驗證（逐條隔離、皆已還原）

| 變異 | 結果 |
|------|------|
| M1 拿掉步驟 3c 還原迴圈 | `test_sandbox_env_restoration` 紅（`TOUKA_CONFIG_DIR` 停在 foreign sandbox） |
| baseline（修復前，決定性重現式） | `git status` 髒：dispatcher SKILL.md 被改＋`engine-smoke-warn/` 新增 |
| baseline（修復後，同一重現式） | `git status` 乾淨 |

## 測試

`tests/unit/test_sandbox_env_restoration.py` 新檔 +1：以 `tmp_path` 下的 foreign sandbox 跑完
一輪 fixture 生命週期，斷言 teardown 後 (a) `get_config_dir()` 回 session 沙盒、
(b) `MEMORY_APP_DATA_DIR` 還原、(c) **`skill_service.config_path` 未停在 foreign sandbox**
（(c) 是真正咬住根因的那條——只驗 env 會漏掉「env 對了但單例快取錯了」的實際故障形態）。

env 讀取一律走 `src.config` 的 SSOT 窗口（`get_config_dir()` / `get_full_env()`）而**非直接
`os.environ`**——後者會踩 Guard #1 `ENV_SSOT_VIOLATION`。刻意不用既有的 `# noqa` 前例
（`test_permission_service_sandbox_isolation.py`）：本測試的需求 SSOT 窗口完全滿足，
加豁免只會白白增加 NOQA_REGISTRY 帳目。

修正 `test_dependency_override_isolation.py` / `test_dependency_override_leak_prevention.py`
（沙盒改 `tmp_path`，斷言不變）。

## 誠實邊界

- 哨兵只看 **repo 根目錄第一層**的增量。測試若寫進既有子目錄（如 `ToukaProxy/config/`）
  不會被抓到；那類外洩仍靠 `ResourceManager` 監獄與 code review。全樹掃描對每次 session
  成本過高（node_modules / .venv / .git），刻意不做。
- 哨兵在 session teardown 才報，故訊號滯後於肇事測試；訊息列出洩漏項名稱供人工定位。
- 步驟 3c 只還原這三個 env 變數（fixture 自己注入的那些）。測試在 body 內自行 `monkeypatch`
  的其他變數由 pytest 既有機制負責。
