# 資源管理 (ResourceManager & Resource Factory)

## 🏭 資源工廠函數 (Resource Factory - SSOT) ⭐

所有 ToukaMemory I/O 操作應透過**依賴反轉工廠函數**進行，呼叫端不得直接耦合 ToukaProxy 的 `ResourceManager` 實作：

```python
from memory_server.config_utils import get_resource_proxy

# 依賴反轉入口（只回傳注入的真實 ResourceManager）
proxy = get_resource_proxy()

# 使用統一介面
proxy.make_dir(path)
proxy.read_file_text(path)
proxy.write_file_text(path, content)
proxy.move_file(src, dst)
# ... 等
```

### 整合契約

| 部署契約 | 返回值 | 失敗行為 |
|---------|-------|---------|
| **ADR-0116 整合模式單一部署** | ToukaProxy 注入的真實 `ResourceManager`（單例） | 取不到 `ResourceManager` 即拋錯（fail-loud） |

**優勢**：
- 領域邏輯經 `IResourceProxy` 協定與 ToukaProxy 內部實作解耦
- 所有 I/O 保留完整身分隔離與路徑保護
- 已退役獨立 MCP 模式與 `_FallbackResourceProxy`，依賴缺失不會靜默降級

---

## 🧱 原子寫入與壞檔可見 (Atomic Write — ADR-0078) ⭐

`write_file_text`（連帶 `write_json`）採**原子寫入**，杜絕「寫到一半當機 → 目標檔損毀 → 下次讀取靜默回空」：

1. 同目錄 `tempfile.mkstemp` 建暫存檔
2. 寫入 → `flush` → `os.fsync`（落盤）
3. `os.replace`（同檔系統原子置換）
4. 失敗則清暫存並拋出（不留半成品）

一處改、**全體 JSON 寫入者受惠**（autonomy_log / token_usage / mission / sessions-JSON / schedules）。`read_json` 對壞檔（`JSONDecodeError`）改記**明確 corrupt 告警**（有別於單純讀取失敗），不再靜默歸零導致配額/成本失準。`is_protected_path`（soul/legacy 保護）與 `make_parents` 行為不變。

---

## 📖 ResourceManager 詳細 API

::: services.resource_manager.ResourceManager
    options:
      show_root_heading: true
      show_source: true
