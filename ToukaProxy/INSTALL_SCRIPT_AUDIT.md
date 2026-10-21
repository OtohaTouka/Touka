> **歷史安裝腳本稽核，非目前操作指引。** 2026-09-14 更新：setup.ps1/setup.sh 現只按 root uv lock 佈建開發依賴，不建立 data/config/.env；install_torch.ps1/.sh 已退役，CPU torch 由 uv.lock 管理。下列舊版「已修復」、待辦、資料目錄與 pip／GPU 建議保留為歷史，不可照做。產品安裝見 [根指南](../README_QUICKSTART.md)，開發見 [CONTRIBUTING](../CONTRIBUTING.md)。

# ToukaProxy 安裝腳本審計報告

> [!IMPORTANT]
> **修復狀態更新 (2026-03-20)**:  
> - ❌ **問題 1 已修復**: `requirements.txt` 已移除 `torch` 衝突行，改由 `install_torch.ps1/sh` 獨立安裝。
> - ❌ **問題 2 已修復**: `setup.ps1/sh` 已整合依賴安裝與環境變數動態搜尋邏輯。
> - ❌ **問題 3 已處理**: `mcp_config.json` 改由系統啟動時自動生成或從範本複製。

---

## ⚠️ 關鍵問題

### 1. ❌ **requirements.txt 格式錯誤**
**位置**: `requirements.txt`（已依 ADR-0117 退役；依賴改由 `ToukaProxy/pyproject.toml`＋根 `uv.lock` 管理）  
**問題**: torch 依賴行格式不正確
```
torch>=2.0.0 --index-url https://download.pytorch.org/whl/cpu
```

**為什麼有問題**:
- pip 在 requirements.txt 中不能在同一行混合版本指定和 `--index-url` 參數
- 這會導致 pip install 失敗

**解決方案**:
選項 A（推薦）：創建 `requirements.txt.cpu` 和 `requirements.txt.gpu` 分檔:
```bash
# 分離出 torch 依賴，在安裝時指定索引
pip install -r requirements.txt
pip install --index-url https://download.pytorch.org/whl/cpu torch>=2.0.0
```

選項 B：使用 pip constraints 檔案或環境變數

---

### 2. ❌ **setup.ps1 和 setup.sh 缺少依賴安裝**
**位置**: `setup.ps1` 和 `setup.sh`\
**問題**: 腳本只檢查環境，沒有安裝 Python 依賴
- 沒有執行 `pip install -r requirements.txt`
- 用戶在本機運行 Proxy 時將無法啟動（缺少 fastapi、anthropic 等）

**預期流程**:
```
1. 檢查 .env ✓ (已實現)
2. 建立資料目錄 ✓ (已實現)
3. 檢查設定檔 ✓ (已實現)
4. 檢查 Docker ✓ (已實現)
5. ❌ 安裝 Python 依賴 (缺失)
6. ❌ 驗證依賴版本 (缺失)
```

**建議修復**:
```powershell
# 添加到 setup.ps1 中
Write-Host "[5/6] 安裝 Python 依賴..." -ForegroundColor Yellow
if ($null -eq (python --version 2>$null)) {
    Write-Host "錯誤：找不到 Python！" -ForegroundColor Red
    exit 1
}
pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "錯誤：依賴安裝失敗！" -ForegroundColor Red
    exit 1
}
```

---

### 3. ❌ **缺少 mcp_config.json**
**位置**: 腳本檢查 `config/mcp_config.json`，但該檔不存在  
**現有檔案**:
- ✓ agents_registry.json
- ✓ group_whitelist.json
- ✓ models.json
- ✓ models_blacklist.json
- ✓ server_config.json
- ❌ **mcp_config.json** (缺失)

**問題**: 代理伺服器在啟動時可能會失敗，因為缺少 MCP 工具配置

**需要確認**:
1. mcp_config.json 是否應包含在版本控制中？
2. 是否需要範本檔案 .mcp_config.json.example？
3. 該檔案應該如何初始化？

---

### 4. ⚠️ **setup.sh 錯誤處理不完整**
**位置**: `setup.sh`\
**問題**: 
```bash
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        # ...
    else
        echo -e "${RED}錯誤：找不到 .env 或 .env.example 檔案！${NC}"
    fi  # 沒有 else 分支處理 .env 存在的情況
else
    echo -e "${GRAY}.env 檔案已存在。${NC}"
fi
```

實際上這裡邏輯是正確的，但建議添加返回代碼檢查:
```bash
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env || exit 1
        echo -e "${GREEN}已從 .env.example 建立 .env 檔案。${NC}"
    else
        echo -e "${RED}錯誤：找不到 .env.example！${NC}"
        exit 1
    fi
fi
```

---

## 📝 警告

### ⚠️ **缺少 Python 版本檢查**
- README.md 說明需要 Python 3.12
- 安裝腳本未驗證 Python 版本
- 若使用舊版本 Python 會導致隱晦的錯誤

**建議添加檢查**:
```powershell
# setup.ps1
$PythonVer = python --version 2>&1
if ($PythonVer -notmatch "3\.(1[2-9]|\d{2,})") {
    Write-Host "錯誤：需要 Python 3.12+，但找到 $PythonVer" -ForegroundColor Red
    exit 1
}
```

### ⚠️ **缺少本機運行操作流程文檔**
- setup.ps1/sh 都只為 Docker 推薦用戶
- 沒有清楚的「本機開發環境配置」指南
- README 只有 Docker compose 啟動命令

---

## ✅ 通過檢查

| 項目 | 狀態 | 備註 |
|------|------|------|
| .env.example 存在 | ✓ | 檔案存在，腳本可正常複製 |
| 資料目錄建立邏輯 | ✓ | setup.ps1 和 setup.sh 都正確 |
| Docker 檢測 | ✓ | 使用 docker version 驗證 |
| 設定目錄結構 | ✓ | config/ 目錄與必需檔案存在 |

---

## 🔧 建議修復優先順序

1. **優先級 1（立即修復）**:
   - [ ] 修復 requirements.txt 中的 torch 依賴格式
   - [ ] 將依賴安裝步驟添加到 setup.ps1 和 setup.sh

2. **優先級 2（重要）**:
   - [ ] 添加 Python 版本檢查
   - [ ] 創建 mcp_config.json 或範本檔案
   - [ ] 添加錯誤碼檢查

3. **優先級 3（改善）**:
   - [ ] 編寫本機開發環境配置文檔
   - [ ] 添加依賴版本驗證步驟
   - [ ] 提供故障排除指南

---

## 📊 對比 ToukaMemory

ToukaMemory 的 setup.sh 參考:
- 同樣缺少依賴安裝步驟（一致的問題）
- 也缺少 Python 版本檢查

**建議**: 為兩個模塊統一安裝檢查邏輯

