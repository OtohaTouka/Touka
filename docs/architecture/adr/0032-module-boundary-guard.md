# ADR-0032: Module Boundary Guard — 模組邊界靜態執法

**狀態**: ✅ 已接受（部分修訂 by [ADR-0116](0116-toukamemory-integration-only.md)：ToukaMemory 退役獨立 MCP 模式後，此邊界守衛**保留**，但正當性由「維持 MCP 獨立性」改為「維持依賴反轉與子系統解耦」；另決策中「白名單允許自動降級為本地實作」已不成立——`get_resource_proxy()` 現 fail-loud、`.importlinter` 白名單現含 `legacy_vault` 等直匯入）  
**日期**: 2026-05-18  
**關鍵字**: import-linter, 模組邊界, ToukaMemory, 禁令 6, ADR-0022, ADR-0116

---

## 背景與問題

`AGENTS.md` §18 禁令 6 明文規定：「嚴禁在 ToukaMemory 模組中直接導入 `src.services.resource_manager`，改用 `memory_server.config_utils.get_resource_proxy()` 工廠函數。」

但此禁令沒有靜態執法工具。探索發現 `ToukaMemory/src/memory_server/tools/diag.py` 存在**真實違規**：

```python
# 違規（會在 MCP 獨立模式下崩潰）
from src.services.resource_manager import resource_manager  # ← 直接依賴 ToukaProxy
from src.utils.time_utils import get_current_epoch          # ← 直接依賴 ToukaProxy
```

此違規在 ToukaMemory MCP 獨立模式下會因 `src.*` 模組不在 PYTHONPATH 而直接崩潰，但過去 18 道 guard 全部未發現。

---

## 決策

使用 `import-linter` 工具建立模組邊界合約，整合至 `guard_all.py` 第 19 道關卡。

### 設定（`.importlinter`，專案根目錄）

```ini
[importlinter]
root_packages =
    memory_server
    src

[importlinter:contract:toukamemory-independence]
name = ToukaMemory must not import ToukaProxy internals directly
type = forbidden
source_modules = memory_server
forbidden_modules =
    src.services / src.utils / src.models / src.routers / ...
ignore_imports =
    memory_server.config_utils -> src.*  # 合法的 try-import 整合層
    memory_server.vector -> src.utils.time_utils  # 合法的 try-import
```

**白名單說明**：`config_utils.py` 和 `vector.py` 在 `try-except` 區塊中嘗試 import `src.*`，失敗時自動降級為本地實作。這是 ADR-0022 定義的「MCP 回退機制」，屬合法設計，列入 `ignore_imports`。

### 修復 `diag.py`

```python
# 修復後（符合禁令 6）
from memory_server import get_app_data_dir
from memory_server.config_utils import get_current_epoch, get_resource_proxy

# 使用 get_resource_proxy().write_file_text() 取代 resource_manager.write_json()
# 使用 get_resource_proxy().read_file_text() 取代 resource_manager.read_file_text()
```

---

## 後果

**正向影響**：
- ToukaMemory 對 ToukaProxy 的逆向依賴受靜態攔截
- `diag.py` 在 MCP 獨立模式下不再崩潰
- 未來同類型意外違規在 CI 中立即被發現

**限制**：
- ToukaChat 的模組邊界仍無 `dependency-cruiser` 執法（留待未來迭代）
- `import-linter` 分析 125 個檔案，執行時間約 2-5 秒
