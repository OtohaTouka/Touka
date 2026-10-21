"use client";

import { createContext, useContext, useState, useEffect, useCallback, useRef, ReactNode } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { fetchTouka, getActiveProxyUrl, getAccessToken, captureRequestScope,
    isRequestScopeCurrent, subscribeCredentialScope } from "./connections";
import { getSystemEpoch, formatToTaipeiTime } from "./time_utils";
import i18n from "./i18n";
import type { components } from "@/types/api";

export type SystemCheckState = "checking" | "auth_required" | "forbidden" | "unavailable" | "degraded" | "ready";
/** 記憶提示（/v1/memory 的 memory_notices）：只有代碼與等級，文字由前端翻譯；不影響狀態判斷。 */
export type MemoryNoticeLevel = "info" | "warning";
export interface MemoryNotice { code: string; level: MemoryNoticeLevel; count?: number; }
/** 系統提示（/v1/health 的 system_notices，例如設定讀不到而停掉的功能）：同樣只有代碼與等級。 */
export type SystemNotice = Omit<MemoryNotice, "count">;
interface SystemStatus {
    isConnected: boolean;
    isMemoryActive: boolean;
    systemDegraded: boolean;
    degradedReason: string;
    latency: string;
    mode: "unknown" | "standalone" | "hybrid";
    checkState: SystemCheckState;
    lastChecked: string;
    memoryNotices: MemoryNotice[];
    systemNotices: SystemNotice[];
}
interface SystemStatusContextType { status: SystemStatus; checkStatus: () => Promise<void>; }
const SystemStatusContext = createContext<SystemStatusContextType | undefined>(undefined);
const record = (value: unknown): value is Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value);
function validHealth(value: unknown): value is components["schemas"]["HealthResponse"] {
    return record(value) && (value.status === "healthy" || value.status === "degraded") &&
        typeof value.timestamp === "string" && typeof value.permission_config_degraded === "boolean" &&
        typeof value.permission_config_degraded_reason === "string";
}
function emptyStatus(checkState: SystemCheckState): SystemStatus {
    return { isConnected: false, isMemoryActive: false, systemDegraded: false, degradedReason: "",
        latency: "", mode: "unknown", checkState, lastChecked: formatToTaipeiTime(getSystemEpoch()), memoryNotices: [], systemNotices: [] };
}
/** 欄位可有可無；格式不對的項目略過，不認得的等級當成資訊級顯示（寧可顯示也不藏起來）。 */
function parseNotices(value: unknown): MemoryNotice[] {
    if (!Array.isArray(value)) return [];
    return value.filter(record).filter(item => typeof item.code === "string" && item.code !== "").map(item => {
        const notice: MemoryNotice = { code: String(item.code), level: item.level === "warning" ? "warning" : "info" };
        if (typeof item.count === "number" && Number.isSafeInteger(item.count) && item.count >= 0) notice.count = item.count;
        return notice;
    });
}
const httpState = (status: number): SystemCheckState => status === 401 ? "auth_required" : status === 403 ? "forbidden" : "unavailable";

export function SystemStatusProvider({ children }: { children: ReactNode }) {
    const { isAuthenticated, isLoading } = useAuth();
    const [status, setStatus] = useState<SystemStatus>(() => emptyStatus("checking"));
    const request = useRef<{ generation: number; controller?: AbortController }>({ generation: 0 });
    const checkStatus = useCallback(async () => {
        request.current.controller?.abort();
        const controller = new AbortController();
        const generation = ++request.current.generation;
        request.current.controller = controller;
        const proxyUrl = getActiveProxyUrl();
        const scope = captureRequestScope();
        const token = getAccessToken();
        const current = () => !controller.signal.aborted && request.current.generation === generation &&
            proxyUrl === getActiveProxyUrl() && (scope ? isRequestScopeCurrent(scope) : token === getAccessToken());
        const result = emptyStatus("checking");
        setStatus({ ...result });
        const start = getSystemEpoch();
        const options = { cache: "no-store" as const, signal: controller.signal,
            nonStreaming: true, ...(scope ? { requestScope: scope } : {}) };
        try {
            const health = await fetchTouka(proxyUrl, '/v1/health', options);
            if (!current()) return;
            if (!health.ok) { setStatus({ ...result, checkState: httpState(health.status) }); return; }
            const healthBody: unknown = await health.json();
            if (!current()) return;
            if (!validHealth(healthBody)) { setStatus({ ...result, checkState: "unavailable" }); return; }
            result.isConnected = true;
            result.systemNotices = parseNotices(healthBody.system_notices);
            result.latency = `${getSystemEpoch() - start}ms`;
            if (isLoading || !isAuthenticated || !scope) {
                setStatus({ ...result, checkState: "auth_required" }); return;
            }
            const memory = await fetchTouka(proxyUrl, '/v1/memory', options);
            if (!current()) return;
            if (!memory.ok) { setStatus({ ...result, checkState: httpState(memory.status) }); return; }
            const body: unknown = await memory.json();
            if (!current()) return;
            if (!record(body) || typeof body.system_degraded !== "boolean" || typeof body.degraded_reason !== "string" ||
                typeof body.memory_tools_count !== "number" || !Number.isSafeInteger(body.memory_tools_count) || body.memory_tools_count < 0) {
                setStatus({ ...result, checkState: "unavailable" }); return;
            }
            result.memoryNotices = parseNotices(body.memory_notices);
            if (body.system_degraded) {
                setStatus({ ...result, mode: "standalone", checkState: "degraded", systemDegraded: true,
                    degradedReason: i18n.t("sidebar.degraded_banner.default_detail") });
            } else if (body.memory_tools_count > 0) {
                setStatus({ ...result, mode: "hybrid", checkState: "ready", isMemoryActive: true });
            } else {
                setStatus({ ...result, checkState: "unavailable" });
            }
        } catch {
            if (current()) setStatus({ ...result, checkState: "unavailable" });
        }
    }, [isAuthenticated, isLoading]);

    useEffect(() => {
        const activeRequest = request.current;
        const timer = setTimeout(() => { void checkStatus(); }, 0);
        const update = () => { void checkStatus(); };
        const unsubscribe = subscribeCredentialScope(update);
        window.addEventListener("touka_connections_updated", update);
        window.addEventListener("touka_auth_changed", update);
        return () => {
            clearTimeout(timer);
            activeRequest.controller?.abort();
            activeRequest.generation++;
            unsubscribe();
            window.removeEventListener("touka_connections_updated", update);
            window.removeEventListener("touka_auth_changed", update);
        };
    }, [checkStatus]);
    return <SystemStatusContext.Provider value={{ status, checkStatus }}>{children}</SystemStatusContext.Provider>;
}
export function useSystemStatus() {
    const context = useContext(SystemStatusContext);
    if (context === undefined) throw new Error("useSystemStatus must be used within a SystemStatusProvider");
    return context;
}
