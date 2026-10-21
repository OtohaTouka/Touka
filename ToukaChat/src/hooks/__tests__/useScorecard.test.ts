import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useScorecard } from "../useScorecard";

const mockFetchTouka = vi.fn();
vi.mock("@/lib/connections", () => ({
    fetchTouka: (...args: unknown[]) => mockFetchTouka(...args),
    getActiveProxyUrl: () => "http://localhost:8000",
}));

function sseBody(events: object[]): ReadableStream<Uint8Array> {
    const enc = new TextEncoder();
    return new ReadableStream({
        start(controller) {
            for (const e of events) {
                controller.enqueue(enc.encode(`data: ${JSON.stringify(e)}\n\n`));
            }
            controller.enqueue(enc.encode("data: [DONE]\n\n"));
            controller.close();
        },
    });
}

describe("useScorecard", () => {
    beforeEach(() => vi.clearAllMocks());

    it("posts a job then streams progress + result", async () => {
        mockFetchTouka
            .mockResolvedValueOnce({ ok: true, json: async () => ({ job_id: "sc_1" }) })
            .mockResolvedValueOnce({
                ok: true,
                body: sseBody([
                    { type: "progress", index: 1, total: 2, scenario: "a", status: "pass", spent_usd: 0.01 },
                    { type: "progress", index: 2, total: 2, scenario: "b", status: "pass", spent_usd: 0.02 },
                    {
                        type: "result",
                        scorecard: {
                            model: "deepseek-chat", budget_usd: 0.5, spent_usd: 0.02,
                            passed: 2, total: 2, skipped: 0, total_tokens: 30, results: [],
                        },
                    },
                ]),
            });

        const { result } = renderHook(() => useScorecard());
        await act(async () => { await result.current.run("deepseek-chat", 0.5); });

        expect(result.current.status).toBe("done");
        expect(result.current.progress).toHaveLength(2);
        expect(result.current.scorecard?.model).toBe("deepseek-chat");
        expect(result.current.scorecard?.passed).toBe(2);

        const [, path, opts] = mockFetchTouka.mock.calls[0];
        expect(path).toBe("/v1/debug/scorecard/run");
        expect(JSON.parse((opts as { body: string }).body)).toMatchObject({
            model: "deepseek-chat", budget_usd: 0.5,
        });
        // stream endpoint hit with the returned job_id
        expect(mockFetchTouka.mock.calls[1][1]).toBe("/v1/debug/scorecard/stream/sc_1");
    });

    it("sends clean_slate in the run POST body when requested", async () => {
        mockFetchTouka
            .mockResolvedValueOnce({ ok: true, json: async () => ({ job_id: "sc_cs" }) })
            .mockResolvedValueOnce({ ok: true, body: sseBody([]) });

        const { result } = renderHook(() => useScorecard());
        await act(async () => { await result.current.run("m", 0.5, undefined, true); });

        const [, , opts] = mockFetchTouka.mock.calls[0];
        expect(JSON.parse((opts as { body: string }).body)).toMatchObject({ clean_slate: true });
    });

    it("surfaces a backend error event", async () => {
        mockFetchTouka
            .mockResolvedValueOnce({ ok: true, json: async () => ({ job_id: "sc_2" }) })
            .mockResolvedValueOnce({ ok: true, body: sseBody([{ type: "error", message: "boom" }]) });

        const { result } = renderHook(() => useScorecard());
        await act(async () => { await result.current.run("m", 0.5); });

        expect(result.current.status).toBe("error");
        expect(result.current.error).toBe("boom");
    });

    it("handles a failed run POST", async () => {
        mockFetchTouka.mockResolvedValueOnce({ ok: false, status: 403 });
        const { result } = renderHook(() => useScorecard());
        await act(async () => { await result.current.run("m", 0.5); });
        expect(result.current.status).toBe("error");
        expect(result.current.error).toBeTruthy();
    });
});
