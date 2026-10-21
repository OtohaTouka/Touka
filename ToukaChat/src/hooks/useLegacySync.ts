import { useState, useCallback, useEffect } from "react";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { logSSOTError, getErrorMessage } from "@/lib/error_handler";
import { toast } from "sonner";
import { useTranslation } from "react-i18next";

export interface LegacyStats {
    agent_id: string;
    message_count: number;
    session_count: number;
    archived_files: number;
    pending_files: number;
    status?: string;
}

const isSuccess = (data: unknown): boolean =>
    typeof data === "object" && data !== null && (data as { status?: unknown }).status === "success";

/** 讀回應 JSON；非 JSON 回 null（錯誤訊息改用預設錯誤碼）。 */
const readJson = (res: Response): Promise<unknown> => res.json().catch(() => null);

/**
 * Hook for managing Legacy Sync State and Actions (SOLID: SRP)
 */
export function useLegacySync(agents: string[] = []) {
    const { t } = useTranslation();
    const [stats, setStats] = useState<Record<string, LegacyStats>>({});
    const [loading, setLoading] = useState<Record<string, boolean>>({});
    const [isGlobalLoading, setIsGlobalLoading] = useState(false);

    const fetchStats = useCallback(async (agentId: string) => {
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, `/v1/stats/legacy/${agentId}`);
            const data: unknown = await readJson(res);
            if (res.ok && isSuccess(data)) {
                setStats(prev => ({ ...prev, [agentId]: data as LegacyStats }));
            } else {
                toast.error(await getErrorMessage(data ?? res, "SYSTEM_ERROR"));
            }
        } catch (e) {
            logSSOTError(e, `useLegacySync:fetchStats:${agentId}`);
            toast.error(await getErrorMessage(e, "SYSTEM_ERROR"));
        }
    }, []);

    const fetchAllStats = useCallback(async () => {
        setIsGlobalLoading(true);
        try {
            await Promise.all(agents.map(id => fetchStats(id)));
        } finally {
            setIsGlobalLoading(false);
        }
    }, [agents, fetchStats]);

    const handleSync = useCallback(async (agentId: string) => {
        setLoading(prev => ({ ...prev, [agentId]: true }));
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, `/v1/stats/legacy/${agentId}/sync`, {
                method: "POST"
            });
            const data: unknown = await readJson(res);
            // 只有 HTTP 成功且後端明說 success 才算同步成功；部分檔案失敗時後端回錯誤碼
            const succeeded = res.ok && isSuccess(data);
            if (succeeded) {
                toast.success(t("hooks.use_legacy_sync.sync_success", { agentId }));
            } else {
                toast.error(await getErrorMessage(data ?? res, "SYNC_FAILED"));
            }
            // 失敗時也重抓統計：已搬好的檔案與留在原地的檔案都要照實顯示
            await fetchStats(agentId);
            return succeeded;
        } catch (e) {
            const errMsg = await getErrorMessage(e, "SYNC_FAILED");
            toast.error(errMsg);
            return false;
        } finally {
            setLoading(prev => ({ ...prev, [agentId]: false }));
        }
    }, [fetchStats, t]);

    // 初始化載入
    useEffect(() => {
        fetchAllStats();
    }, [fetchAllStats]);

    return {
        stats,
        loading,
        isGlobalLoading,
        fetchStats,
        fetchAllStats,
        handleSync
    };
}
