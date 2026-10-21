import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ContextSnapshotPanel } from "../ContextSnapshotPanel";

// present=true（真實區塊）＋ present=false（佔位區塊）各一，驗證收合列的「未出現」badge。
const mockLoad = vi.fn();
const mockExport = vi.fn();

vi.mock("@/hooks/useContextSnapshot", () => ({
    useContextSnapshot: () => ({
        status: "done",
        error: null,
        load: mockLoad,
        exportMarkdown: mockExport,
        snapshot: {
            agent_id: "DebugAgent",
            model: "claude-opus-4-8",
            window: 200000,
            total_tokens: 42,
            usage_pct: 0.1,
            cache_reordered: false,
            system_text: "<persona>You are Touka.</persona>",
            last_payload: null,
            blocks: [
                {
                    position: 1, tag: "persona", block_name: "Persona (Soul)",
                    layer: "SoulLayer", source: "soul/<agent>/soul.md", kind: "real",
                    present: true, content_length: 14, tokens: 42,
                    preview: "You are Touka.", cache_tier: "stable",
                },
                {
                    position: 2, tag: "project_context", block_name: "Project Context",
                    layer: "ContextLayer", source: "get_project_context(project_id) (DB)",
                    kind: "mock", present: false, content_length: null, tokens: 0,
                    preview: "<project_context>…</project_context>", cache_tier: null,
                },
            ],
        },
    }),
}));

describe("ContextSnapshotPanel", () => {
    it("renders both present and absent blocks in order", () => {
        render(<ContextSnapshotPanel />);
        expect(screen.getByText(/1\. Persona \(Soul\)/)).toBeTruthy();
        expect(screen.getByText(/2\. Project Context/)).toBeTruthy();
    });

    it("shows the ⚪ absent badge on the collapsed row (aligned with the .md export)", () => {
        render(<ContextSnapshotPanel />);
        // 收合狀態（未點開）就要能看到「未出現」訊號——先前只有 opacity 淡化，肉眼難辨。
        const badges = screen.getAllByText(/⚪/);
        expect(badges.length).toBe(1); // 只有 present=false 那列有，真實區塊不得誤標
    });

    it("expanding the absent block reveals the full not-present note", () => {
        render(<ContextSnapshotPanel />);
        fireEvent.click(screen.getByText(/2\. Project Context/));
        expect(screen.getByText(/本次請求未出現|Not in this request/)).toBeTruthy();
    });
});
