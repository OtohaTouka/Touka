import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ScorecardPanel } from "../ScorecardPanel";

const mockRun = vi.fn();
const mockReset = vi.fn(() => Promise.resolve({ agent_id: "DebugAgent", backup: { status: "success" }, clean: true, stores: [] }));

vi.mock("@/hooks/useScorecard", () => ({
    useScorecard: () => ({
        status: "done",
        progress: [],
        error: null,
        scorecard: {
            model: "deepseek-chat", budget_usd: 0.5, spent_usd: 0.018,
            passed: 2, total: 3, skipped: 1, total_tokens: 1234,
            results: [
                { scenario: "chat_basic", surface: "chat", pass: true, skipped: false, detail: "ok", tokens: 100, cost_usd: 0.001, latency_ms: 50, react_turns: 2, finish_reason: "stop", tool_trace: [{ name: "memory_tools", args: '{"query":"燈火"}', result: "found 2 memories", status: "success" }] },
                { scenario: "react_multistep", surface: "react", pass: true, skipped: false, detail: "ok", tokens: 200, cost_usd: 0.002, latency_ms: 80, estimated: true },
                { scenario: "extra_one", surface: "chat", pass: false, skipped: true, detail: "skipped: budget_exhausted", tokens: 0, cost_usd: 0, latency_ms: 0 },
            ],
        },
        run: mockRun,
    }),
}));

vi.mock("@/lib/providers/ModelProvider", () => ({
    useModels: () => ({ availableModels: [{ id: "deepseek-chat", name: "DeepSeek", owned_by: "deepseek" }] }),
}));

vi.mock("@/hooks/useDebugReset", () => ({
    useDebugReset: () => ({ status: "idle", report: null, error: null, reset: mockReset }),
}));

// 確認框自動確認
vi.mock("@/hooks/useConfirmToast", () => ({
    useConfirmToast: () => ({ confirmToast: (_m: string, onConfirm: () => void) => onConfirm() }),
}));

describe("ScorecardPanel", () => {
    it("renders the scorecard result rows", () => {
        render(<ScorecardPanel />);
        expect(screen.getByText("chat_basic")).toBeTruthy();
        expect(screen.getByText("react_multistep")).toBeTruthy();
        expect(screen.getByText("extra_one")).toBeTruthy();
    });

    it("clicking run with a model triggers run (via confirm)", () => {
        render(<ScorecardPanel />);
        const input = screen.getByPlaceholderText("deepseek-chat") as HTMLInputElement;
        fireEvent.change(input, { target: { value: "deepseek-chat" } });
        // run 按鈕（i18n 測試固定 zh-TW → 「啟動評分」）
        const btn = screen.getByRole("button", { name: /啟動評分|Run scorecard/ });
        fireEvent.click(btn);
        expect(mockRun).toHaveBeenCalledWith("deepseek-chat", 0.5, undefined, false);
    });

    it("passes clean_slate=true when the toggle is checked", () => {
        render(<ScorecardPanel />);
        const input = screen.getByPlaceholderText("deepseek-chat") as HTMLInputElement;
        fireEvent.change(input, { target: { value: "deepseek-chat" } });
        fireEvent.click(screen.getByLabelText(/乾淨模式|Clean slate/));
        fireEvent.click(screen.getByRole("button", { name: /啟動評分|Run scorecard/ }));
        expect(mockRun).toHaveBeenCalledWith("deepseek-chat", 0.5, undefined, true);
    });

    it("expands a row to reveal the tool trace (masked args + result)", () => {
        render(<ScorecardPanel />);
        // 預設未展開 → 軌跡標題不在
        expect(screen.queryByText("工具軌跡")).toBeNull();
        fireEvent.click(screen.getByText("chat_basic"));
        expect(screen.getByText("工具軌跡")).toBeTruthy();
        expect(screen.getByText(/memory_tools/)).toBeTruthy();
        expect(screen.getByText(/found 2 memories/)).toBeTruthy();
    });

    it("auto-uses the first available model when the input is empty (no hardcoded default)", () => {
        render(<ScorecardPanel />);
        const btn = screen.getByRole("button", { name: /啟動評分|Run scorecard/ }) as HTMLButtonElement;
        // 有可用模型 → 免輸入即可啟動（採第一個能跑的模型，非硬編碼）
        expect(btn.disabled).toBe(false);
        fireEvent.click(btn);
        expect(mockRun).toHaveBeenCalledWith("deepseek-chat", 0.5, undefined, false);
    });

    it("marks estimated rows and shows the estimate note", () => {
        render(<ScorecardPanel />);
        // 估算列以 ～ 前綴顯示 token（react_multistep estimated=true, tokens=200）
        expect(screen.getByText("~200")).toBeTruthy();
        // 出現估算說明（i18n 測試固定 zh-TW）
        expect(screen.getByText(/估算|estimated/)).toBeTruthy();
    });

    it("renders the destructive DebugAgent reset zone", () => {
        render(<ScorecardPanel />);
        // 標題用精確字串（避免比對到含「資料」後綴的按鈕文字）；i18n 測試固定 zh-TW
        expect(screen.getByText("重置 DebugAgent")).toBeTruthy();
        expect(screen.getByRole("button", { name: /重置 DebugAgent 資料|Reset DebugAgent data/ })).toBeTruthy();
    });

    it("reset zone says where the backups are and that only deleting whole snapshots removes them", () => {
        render(<ScorecardPanel />);
        expect(screen.getByText(/「DebugAgent」的資料還留在系統快照裡/)).toBeTruthy();
        expect(screen.getByText(/Touka_Backup_/)).toBeTruthy();
        expect(screen.getByText(/vault\/core_memory_backups/)).toBeTruthy();
        expect(screen.getByText(/刪整份快照/)).toBeTruthy();
        expect(screen.queryByText(/勾選/)).toBeNull();
    });

    it("clicking reset triggers the destructive reset (via confirm)", async () => {
        render(<ScorecardPanel />);
        fireEvent.click(screen.getByRole("button", { name: /重置 DebugAgent 資料|Reset DebugAgent data/ }));
        await waitFor(() => expect(mockReset).toHaveBeenCalledTimes(1));
        // reset 不帶任何 agent id 參數——目標由 hook/後端固定為 DebugAgent（防 IDOR）
        expect(mockReset).toHaveBeenCalledWith();
    });
});
