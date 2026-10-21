import { useState, useCallback, useEffect } from 'react';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError, errorMsg } from '@/lib/error_handler';

export interface SlidingWindowConfig {
    enabled: boolean;
    // ADR-0092：token 百分比規則（現行）
    trigger_pct: number;
    target_floor_pct: number;
    summary_budget_pct: number;
    min_keep_pairs?: number;
    summary_model?: string;
    // deprecated 相容欄位（後端仍接受）
    keep_rounds?: number;
    upper_limit_rounds?: number;
    version?: string;
    author?: string;
    description?: string;
}

const DEFAULT_CONFIG: SlidingWindowConfig = {
    enabled: false,
    trigger_pct: 90,
    target_floor_pct: 50,
    summary_budget_pct: 0,
};

/**
 * Hook for reading and writing the sliding-window config (SRP, SSOT).
 * Config is persisted server-side in sliding_window.json.
 */
export function useSlidingWindowSettings() {
    const [config, setConfig] = useState<SlidingWindowConfig>(DEFAULT_CONFIG);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);

    const fetchConfig = useCallback(async () => {
        setLoading(true);
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, '/v1/system/sliding_window');
            if (res.ok) {
                const data = await res.json();
                setConfig({ ...DEFAULT_CONFIG, ...data });
            }
        } catch (err) {
            logSSOTError(err, 'useSlidingWindowSettings:fetch');
        } finally {
            setLoading(false);
        }
    }, []);

    const saveConfig = useCallback(async (next: SlidingWindowConfig): Promise<boolean> => {
        if (next.target_floor_pct + next.summary_budget_pct >= next.trigger_pct) {
            logSSOTError(
                new Error(errorMsg("SYSTEM_ERROR")),
                'useSlidingWindowSettings:validate',
            );
            return false;
        }
        setSaving(true);
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, '/v1/system/sliding_window', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(next),
            });
            if (res.ok) {
                const data = await res.json();
                setConfig({ ...DEFAULT_CONFIG, ...data.config });
                return true;
            }
            return false;
        } catch (err) {
            logSSOTError(err, 'useSlidingWindowSettings:save');
            return false;
        } finally {
            setSaving(false);
        }
    }, []);

    useEffect(() => {
        fetchConfig();
    }, [fetchConfig]);

    return { config, loading, saving, fetchConfig, saveConfig };
}
