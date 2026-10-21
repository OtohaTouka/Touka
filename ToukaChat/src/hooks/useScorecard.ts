import { useCallback, useState } from "react";
import { fetchTouka, getActiveProxyUrl } from "@/lib/connections";
import { logSSOTError, errorMsg } from "@/lib/error_handler";

/** 單一工具調用的軌跡（後端已遮罩截斷）。 */
export interface ScorecardToolTrace {
    name: string;
    args: string;
    result: string;
    status: string;
}

/** 單一情境的評分結果（對齊後端 formatter.to_json）。 */
export interface ScorecardResultRow {
    scenario: string;
    surface: string;
    pass: boolean;
    skipped: boolean;
    detail: string;
    tokens: number;
    cost_usd: number;
    latency_ms: number;
    estimated?: boolean;
    // 軌B 加強（telemetry）：供前端攤開「為何過/敗」。
    tool_trace?: ScorecardToolTrace[];
    react_turns?: number;
    finish_reason?: string;
    early_stop?: boolean;
    // 軌B Batch 5（成品驗證）：docgen 等產檔情境的開檔驗證結果（null＝該情境不驗成品）。
    artifact_valid?: boolean | null;
    artifact_detail?: string;
}

/** 完整成績單（對齊後端 formatter.to_json）。 */
export interface ScorecardData {
    model: string;
    budget_usd: number;
    spent_usd: number;
    passed: number;
    total: number;
    skipped: number;
    total_tokens: number;
    results: ScorecardResultRow[];
}

export interface ScorecardProgress {
    index: number;
    total: number;
    scenario: string;
    status: string;
    spent_usd?: number;
    cost_usd?: number;
}

export type ScorecardStatus = "idle" | "running" | "done" | "error";

export interface UseScorecardReturn {
    status: ScorecardStatus;
    progress: ScorecardProgress[];
    scorecard: ScorecardData | null;
    error: string | null;
    run: (
        model: string,
        budgetUsd: number,
        scenarioIds?: string[],
        cleanSlate?: boolean,
    ) => Promise<void>;
}

/**
 * 模型計分卡執行 hook（軌B 批次2）。
 *
 * 流程：POST /v1/debug/scorecard/run 取得 job_id → 以 fetchTouka 讀取
 * /v1/debug/scorecard/stream/{job_id} 的 SSE streaming body（沿用 useAgentSSE 的 reader 樣式，
 * 但走 fetchTouka 以滿足 Fetch SSOT 與自動帶 Auth），逐事件更新進度／成績單。
 * ⚠️ 觸發真實模型，會花費 API 費用——呼叫端須先取得使用者確認。
 */
export function useScorecard(): UseScorecardReturn {
    const [status, setStatus] = useState<ScorecardStatus>("idle");
    const [progress, setProgress] = useState<ScorecardProgress[]>([]);
    const [scorecard, setScorecard] = useState<ScorecardData | null>(null);
    const [error, setError] = useState<string | null>(null);

    const run = useCallback(async (
        model: string,
        budgetUsd: number,
        scenarioIds?: string[],
        cleanSlate?: boolean,
    ) => {
        setStatus("running");
        setProgress([]);
        setScorecard(null);
        setError(null);

        const proxyUrl = getActiveProxyUrl();
        try {
            const startRes = await fetchTouka(proxyUrl, "/v1/debug/scorecard/run", {
                method: "POST",
                body: JSON.stringify({
                    model,
                    budget_usd: budgetUsd,
                    scenario_ids: scenarioIds ?? null,
                    clean_slate: cleanSlate ?? false,
                }),
            });
            if (!startRes.ok) throw new Error(`HTTP ${startRes.status}`);
            const { job_id: jobId } = (await startRes.json()) as { job_id: string };

            const streamRes = await fetchTouka(proxyUrl, `/v1/debug/scorecard/stream/${jobId}`, {
                method: "GET",
                headers: { Accept: "text/event-stream" },
            });
            const reader = streamRes.body?.getReader();
            if (!reader) throw new Error("no stream body");

            const decoder = new TextDecoder();
            let buffer = "";
            let sawResult = false;
            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split("\n");
                buffer = lines.pop() || "";
                for (const line of lines) {
                    const trimmed = line.trim();
                    if (!trimmed.startsWith("data:")) continue;
                    const payload = trimmed.slice(5).trim();
                    if (!payload || payload === "[DONE]") continue;
                    let ev: Record<string, unknown>;
                    try {
                        ev = JSON.parse(payload);
                    } catch {
                        continue;
                    }
                    if (ev.type === "progress") {
                        setProgress(prev => [...prev, ev as unknown as ScorecardProgress]);
                    } else if (ev.type === "result") {
                        setScorecard(ev.scorecard as ScorecardData);
                        setStatus("done");
                        sawResult = true;
                    } else if (ev.type === "error") {
                        setError(String(ev.message ?? "error"));
                        setStatus("error");
                    }
                }
            }
            if (!sawResult) setStatus(prev => (prev === "running" ? "done" : prev));
        } catch (e) {
            logSSOTError(e, "useScorecard:run");
            setError(errorMsg("SYSTEM_ERROR"));
            setStatus("error");
        }
    }, []);

    return { status, progress, scorecard, error, run };
}
