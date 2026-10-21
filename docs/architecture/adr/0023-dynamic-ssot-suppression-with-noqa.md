# ADR-0023: Dynamic SSOT Suppression via Inline Noqa Comments

## 狀態 (Status)
**Accepted** (2026-05-11)

## 背景 (Context)
Touka 系統高度依賴 `lint_ssot.py` 來強制執行架構主權（例如 I/O 監獄、時間主權、環境變數主權）。然而，在開發底層基礎建設（例如 `ToukaMemory` 的 `manager.py`）時，某些合法的原生操作（如 `path.mkdir()`）會被 SSOT 規則攔截。

過去我們採用的妥協方案是：
1. **全域白名單**：將整個檔案加入 `WHITELIST_FILES`。這會導致該檔案「完全」失去所有 SSOT 保護，產生架構漏洞。
2. **AST 規避 (Hack)**：使用 `getattr(path, "mkdir")()` 來騙過基於語法樹 (AST) 的檢查器。這會產生大量的 IDE 警告 (`B004`)，且嚴重降低代碼可讀性。

隨著基建越來越複雜，這類需求勢必會增加。我們需要一種既能維持 Python 慣例，又能安全、精確地抑制特定檢查規則的機制。

## 決策 (Decision)
我們決定在 `lint_ssot.py` 中導入「戰術性豁免 (Tactical Suppression)」機制：
1. **行級註解支援**：實施 `# noqa: [RULE_ID]` 的正規註解解析。
2. **混合解析架構**：由於 Python 的 `ast` 模組不保存註解，`lint_ssot.py` 將在生成 AST 之前，先利用 Regex (`parse_noqa_comments`) 掃描源代碼，建立 `行號 -> 豁免規則` 的對映表。
3. **廢除規避語法**：全面廢除所有為規避 Linter 而寫的 `getattr` 或反模式，回歸 PEP-8 慣例寫法。

## 影響 (Consequences)
*   **正面**：
    *   **可讀性提升**：開發者可以寫出乾淨的 `path.mkdir()` 而不是彆扭的 `getattr`。
    *   **精確打擊**：`# noqa` 是行級距且綁定特定規則的（例如僅豁免 `IO_PRISON_VIOLATION`），該行的其他違規或該檔案的其他部分依然受 SSOT 嚴格保護。
    *   **IDE 友善**：可以與標準 Linter（如 Ruff）的 `# noqa: B004` 共存，減少無意義的警告。
*   **負面**：
    *   `lint_ssot.py` 變得略微複雜，需要同時處理文字解析與 AST 解析。
    *   開發者必須謹慎使用 `# noqa`，避免濫用。這項機制僅限於「核心基礎建設開發」，一般業務邏輯仍應嚴格遵守 SSOT 封裝。

## 替代方案 (Alternatives Considered)
*   **單獨拆分 Linter 規則為第三方工具（如 Flake8 Plugin）**：開發成本過高，且不符合 Touka 自給自足的基礎建設精神。
*   **維持現狀（使用 getattr Hack）**：會隨著系統擴張導致大量 `type: ignore` 與難以閱讀的底層代碼，不符合長期維護利益。
