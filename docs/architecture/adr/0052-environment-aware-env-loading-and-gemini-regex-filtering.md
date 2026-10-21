# ADR-0052: 環境感知 .env 互斥載入與 Gemini 篩選正則重構 (Environment-Aware .env Loading & Gemini Regex Filtering)

**狀態**：✅ 已接受  
**日期**：2026-05-28  
**影響範圍**：ToukaProxy、ToukaHeartbeat

> 🔧 **決策 §1 `resolve_env_file_path()` 已被 [ADR-0151](0151-env-persistence-anchor-decouple.md) 加高優先分支**：現**先查 `TOUKA_ENV_FILE`**（若設即無條件回傳該路徑），繞過 ADR 原描述的 CWD/`is_integrated` 互斥選擇邏輯（0151 把 .env 錨定 compose 目錄、與 WORKSPACE_ROOT 解耦）。Gemini 篩選正則重構部分仍有效。

---

## 背景

在 Touka 系統中，子專案（如 `ToukaProxy` 與 `ToukaHeartbeat`）需要支持以下兩種運行模式：
1. **全體整合模式**：整個系統在根目錄統一運行（例如 `docker-compose` 或整合測試），要求強制統一讀取根目錄 `.env`，忽略子專案目錄下的 `.env`。
2. **分開解耦模式**：子專案作為獨立的模組被移出或單獨執行，要求強制只讀取子專案目錄自身的 `.env`，不讀取根目錄 `.env`。

**問題**：原有的 Pydantic Settings 設定 `env_file=(".env", "../.env", "../../.env")` 會在不同 CWD（目前工作目錄）下加載多個 `.env` 檔案並進行合併。這不僅無法保證「互斥（Exclusive）載入」的要求，亦導致在整合模式下，子專案的 `.env` 設定被意外混入，造成金鑰與環境參數的 SSOT 衝突。

此外，遠端 Google Gemini API 模型抓取器（`fetcher.py`）使用硬編碼的名單篩選最新模型（`["gemini-3", "gemini-2.5", "gemini-2.0"]`）。這使得被停用的 `gemini-1.5` 系列無法動態篩選，且未來高版本發布時需要手動修改代碼，違反了 Open-Closed Principle (OCP)。

---

## 決策

1. **環境感知 .env 互斥載入**：
   * 在 `ToukaProxy/src/config.py` 與 `ToukaHeartbeat/config.py` 中，定義路徑動態解析函式（`resolve_env_file_path()` 與 `resolve_heartbeat_env_file_path()`）。
   * 藉由檢查 `Path.cwd()` 是否處於專案子目錄、根目錄 `.env` 是否存在，以及 `TOUKA_INTEGRATED` 環境變數，決定載入的唯一路徑。
   * 在 `BaseSettings` 的 `__init__` 建構子中，動態將覆寫之 `_env_file` 參數傳遞給 `super().__init__`，以實現排他性載入。
2. **Gemini 篩選正則重構**：
   * 在 `fetcher.py` 中，使用正規表達式 `gemini-(\d+(?:\.\d+)?)` 動態比對並提取 Gemini 模型的主版本號。
   * 僅放行主版本號 `>= 2.0` 的模型，並過濾掉 `1.5` 及更早之版本。

---

## 實作細節

### 1. ToukaProxy 設定重構

在 [config.py (ToukaProxy)](file:///d:/Touka/ToukaProxy/src/config.py) 中：
```python
def resolve_env_file_path() -> Path:
    """根據執行模式動態解析 .env 檔案路徑 (SSOT)."""
    cwd = Path.cwd().resolve()
    is_proxy_dir = (cwd == PROXY_ROOT) or (PROXY_ROOT in cwd.parents)
    
    # 整合條件：(1) CWD 不在 Proxy 目錄下 且 根目錄 .env 存在；(2) 或 TOUKA_INTEGRATED 環境變數為 true
    is_integrated = (
        not is_proxy_dir and (WORKSPACE_ROOT / ".env").exists()
    ) or (os.environ.get("TOUKA_INTEGRATED") == "true")
    
    if is_integrated:
        return WORKSPACE_ROOT / ".env"
    return PROXY_ROOT / ".env"

class Settings(BaseSettings):
    # ...
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if "_env_file" not in kwargs:
            kwargs["_env_file"] = str(resolve_env_file_path())
        super().__init__(*args, **kwargs)
```

### 2. ToukaHeartbeat 設定重構

在 [config.py (ToukaHeartbeat)](file:///d:/Touka/ToukaHeartbeat/config.py) 中：
```python
def resolve_heartbeat_env_file_path() -> Path:
    """根據執行模式動態解析 .env 檔案路徑 (SSOT)."""
    cwd = Path.cwd().resolve()
    is_heartbeat_dir = (cwd == HEARTBEAT_ROOT) or (HEARTBEAT_ROOT in cwd.parents)
    
    is_integrated = (
        not is_heartbeat_dir and (WORKSPACE_ROOT / ".env").exists()
    ) or (os.environ.get("TOUKA_INTEGRATED") == "true")
    
    if is_integrated:
        return WORKSPACE_ROOT / ".env"
    return HEARTBEAT_ROOT / ".env"

class HeartbeatSettings(BaseSettings):
    # ...
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if "_env_file" not in kwargs:
            kwargs["_env_file"] = str(resolve_heartbeat_env_file_path())
        super().__init__(*args, **kwargs)
```

### 3. Gemini 模型篩選器重構

在 [fetcher.py](file:///d:/Touka/ToukaProxy/src/services/models/fetcher.py) 中：
```python
                    match = re.search(r"gemini-(\d+(?:\.\d+)?)", model_id)
                    if match:
                        version = float(match.group(1))
                        is_recent = version >= 2.0
                    else:
                        is_recent = False
```

---

## 驗證流程 (TDD)

1. **環境感知單元測試**：
   * 建立 [test_config_env_selection.py](file:///d:/Touka/tests/unit/test_config_env_selection.py)，在隔離的 temp 沙盒中 Mock `Path.cwd()` 與 `os.environ` 參數，驗證 `Settings` 與 `HeartbeatSettings` 在整合模式、獨立模式以及環境變數覆寫下，載入的 `.env` 變數與路徑是否嚴格互斥。
2. **Gemini 篩選單元測試**：
   * 在 [test_fetcher_ssot.py](file:///d:/Touka/tests/unit/test_fetcher_ssot.py) 中新增 `test_gemini_fetcher_version_filtering`，以 Mock 響應數據驗證 `gemini-2.0`, `gemini-2.5`, `gemini-3.0` 等正確保留，而 `gemini-1.5` 被準確過濾。
3. **品質與冒煙測試**：
   * 執行全體 923 個 Pytest 測試，並運行 `python scripts/guard_all.py` 確保 26 道靜態與架構守衛全綠通過。
