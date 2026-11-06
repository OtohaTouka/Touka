import { useCallback, useEffect, useRef, useState } from "react";
import { fetchTouka, getActiveProxyUrl } from "@/lib/connections";
import { getErrorMessage, getResponseDetailString, getResponseErrorCode, logSSOTError } from "@/lib/error_handler";
import type { ErrorCode } from "@/lib/error_codes";
import type { AgentResetRequest, AgentResetResponse, LastResetStatus } from "@/lib/types";

/** 可以重置的代理人（`/v1/models` 裡的靈魂，DebugAgent 除外）. */
export interface ResettableAgent {
    id: string;
    name?: string;
    is_agent?: boolean;
}

/** 一次重置的結果：伺服器回報告、伺服器拒絕，或不知道（斷線、逾時）. */
export type AgentResetOutcome =
    | { kind: "done"; report: AgentResetResponse }
    | { kind: "rejected"; code?: ErrorCode; message: string; collidesWith?: string }
    | { kind: "unknown" };

/** 閘道或代理自己回的狀態：請求可能根本沒到伺服器、或伺服器還在跑，結果不明。 */
const GATEWAY_STATUSES: ReadonlySet<number> = new Set([502, 504, 524]);

/**
 * 每靈魂資料重置（ADR-0113）：載入可重置的代理人、送出重置。
 *
 * 重置請求自帶 signal，`fetchTouka` 對自帶 signal 的請求不套連線逾時（勾選清除連備份時可能要幾分鐘）。
 * 元件卸載時不中止請求，伺服器照樣跑完，只是不再更新畫面。斷線或回應讀不完時結果是「不知道」，不是失敗。
 * 「不知道」時可以向伺服器查最後一次重置的結果（`last`；掛載時也查一次，舊審查報告丙-5）。
 */
export function useAgentDataReset() {
    const proxyUrl = getActiveProxyUrl();
    const [agents, setAgents] = useState<ResettableAgent[]>([]);
    const [busy, setBusy] = useState(false);
    const [last, setLast] = useState<LastResetStatus | null>(null);
    const mounted = useRef(true);

    useEffect(() => {
        mounted.current = true;
        return () => {
            mounted.current = false;
        };
    }, []);

    const loadAgents = useCallback(async () => {
        try {
            const res = await fetchTouka(proxyUrl, "/v1/models");
            if (!res.ok) return;
            const data = await res.json();
            const rows: ResettableAgent[] = (data?.data || [])
                .filter((m: ResettableAgent) => m.is_agent && m.id !== "DebugAgent")
                .sort((a: ResettableAgent, b: ResettableAgent) => a.id.localeCompare(b.id));
            if (mounted.current) setAgents(rows);
        } catch (e) {
            logSSOTError(e, "useAgentDataReset:loadAgents");
        }
    }, [proxyUrl]);

    useEffect(() => {
        const timer = setTimeout(loadAgents, 0);
        return () => clearTimeout(timer);
    }, [loadAgents]);

    /** 伺服器記著的最後一次重置（只在記憶體，重啟後是 idle）；查詢本身失敗時回 null，卡上保留上一次查到的. */
    const refreshLast = useCallback(async (): Promise<LastResetStatus | null> => {
        let status: LastResetStatus | null = null;
        try {
            const res = await fetchTouka(proxyUrl, "/v1/admin/agents/reset/last");
            if (res.ok) status = (await res.json()) as LastResetStatus;
        } catch (e) {
            logSSOTError(e, "useAgentDataReset:refreshLast");
        }
        if (status !== null && mounted.current) setLast(status);
        return status;
    }, [proxyUrl]);

    useEffect(() => {
        const timer = setTimeout(refreshLast, 0);
        return () => clearTimeout(timer);
    }, [refreshLast]);

    const reset = useCallback(
        async (agentId: string, body: AgentResetRequest): Promise<AgentResetOutcome> => {
            setBusy(true);
            const controller = new AbortController();
            try {
                const res = await fetchTouka(proxyUrl, `/v1/admin/agents/${encodeURIComponent(agentId)}/reset`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                    signal: controller.signal,
                });
                if (!res.ok) {
                    const code = await getResponseErrorCode(res);
                    // 沒有 Touka 錯誤碼（代理的 HTML 錯誤頁等）或閘道逾時：伺服器可能照樣跑完，不能說失敗
                    if (code === undefined || GATEWAY_STATUSES.has(res.status)) return { kind: "unknown" };
                    const collidesWith =
                        code === "MEMORY_SCRUB_COLLECTION_SHARED"
                            ? await getResponseDetailString(res, "collides_with")
                            : undefined;
                    return { kind: "rejected", code, message: await getErrorMessage(res, "SYSTEM_ERROR"), collidesWith };
                }
                return { kind: "done", report: (await res.json()) as AgentResetResponse };
            } catch (e) {
                logSSOTError(e, "useAgentDataReset:reset");
                return { kind: "unknown" };
            } finally {
                if (mounted.current) setBusy(false);
            }
        },
        [proxyUrl],
    );

    return { agents, loadAgents, busy, reset, last, refreshLast };
}
