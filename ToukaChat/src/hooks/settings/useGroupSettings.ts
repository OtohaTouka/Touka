import { useState, useCallback } from 'react';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { Model } from '@/lib/types';
import { logSSOTError, errorMsg } from '@/lib/error_handler';
import { toast } from 'sonner';

// 群聊成員只限靈魂（ADR-0245）：只有 ToukaSoul（別名 → 靈魂 ID）一區
export interface GroupWhitelist {
    ToukaSoul: Record<string, string>;
}

export function useGroupSettings() {
    const [whitelist, setWhitelist] = useState<GroupWhitelist>({ ToukaSoul: {} });
    const [availableModels, setAvailableModels] = useState<Model[]>([]);
    const [isLoading, setIsLoading] = useState(false);

    const fetchWhitelist = useCallback(async () => {
        setIsLoading(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/group/whitelist");
            if (res.ok) {
                const data = await res.json();
                setWhitelist(data);
            } else {
                toast.error(errorMsg("CONFIG_LOAD_FAILED"));
            }
        } catch (e) {
            logSSOTError(e, "useGroupSettings:fetchWhitelist");
            toast.error(errorMsg("CONFIG_LOAD_FAILED"));
        } finally {
            setIsLoading(false);
        }
    }, []);

    const fetchModels = useCallback(async () => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/models");
            if (res.ok) {
                const data = await res.json();
                setAvailableModels(data.data || data);
            } else {
                toast.error(errorMsg("CONFIG_LOAD_FAILED"));
            }
        } catch (e) {
            logSSOTError(e, "useGroupSettings:fetchModels");
            toast.error(errorMsg("CONFIG_LOAD_FAILED"));
        }
    }, []);

    const saveWhitelist = async (newData: GroupWhitelist) => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/group/whitelist", {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newData)
            });
            if (res.ok) {
                await fetchWhitelist();
                return true;
            }
        } catch (e) {
            logSSOTError(e, "useGroupSettings:saveWhitelist");
        }
        return false;
    };

    return {
        whitelist,
        availableModels,
        isLoading,
        fetchWhitelist,
        fetchModels,
        saveWhitelist
    };
}
