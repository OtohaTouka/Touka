import { useCallback, useState } from "react";
import { fetchTouka, getActiveProxyUrl } from "@/lib/connections";
import { logSSOTError, errorMsg } from "@/lib/error_handler";

/** 單一 store 的清除結果（對齊後端 ResetReport.to_dict().stores[]）。 */
export interface ResetStoreRow {
    name: string;
    purged: number;
    remaining: number;
    error: string;
}

/** DebugAgent 重置報告（對齊後端 orchestrator.ResetReport.to_dict）。 */
export interface ResetReport {
    agent_id: string;
    backup: Record<string, string> | null;
    clean: boolean;
    stores: ResetStoreRow[];
}

export type ResetStatus = "idle" | "running" | "done" | "error";

export interface UseDebugResetReturn {
    status: ResetStatus;
    report: ResetReport | null;
    error: string | null;
    /** 觸發破壞性重置；雙重確認載荷由 hook 固定送出（後端再次硬鎖 DebugAgent）。 */
    reset: () => Promise<ResetReport | null>;
}

/**
 * DebugAgent 破壞性重置 hook（軌B Batch 6 Phase 4）。
 *
 * POST /v1/debug/reset，固定送出雙重確認載荷（confirm + 字面 confirm_agent_id="DebugAgent"）。
 * ⚠️ 破壞性、不可復原——呼叫端須先以 confirmToast 取得使用者確認。後端會先全系統備份再清除，
 * 並以安全閘硬鎖：任何非 DebugAgent 目標一律拒絕。
 */
export function useDebugReset(): UseDebugResetReturn {
    const [status, setStatus] = useState<ResetStatus>("idle");
    const [report, setReport] = useState<ResetReport | null>(null);
    const [error, setError] = useState<string | null>(null);

    const reset = useCallback(async (): Promise<ResetReport | null> => {
        setStatus("running");
        setReport(null);
        setError(null);

        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/debug/reset", {
                method: "POST",
                body: JSON.stringify({ confirm: true, confirm_agent_id: "DebugAgent" }),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = (await res.json()) as ResetReport;
            setReport(data);
            setStatus("done");
            return data;
        } catch (e) {
            logSSOTError(e, "useDebugReset:reset");
            setError(errorMsg("SYSTEM_ERROR"));
            setStatus("error");
            return null;
        }
    }, []);

    return { status, report, error, reset };
}
