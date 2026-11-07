import { useState, useCallback, useEffect } from 'react';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError, errorMsg } from '@/lib/error_handler';
import type { SlidingWindowConfig } from '@/lib/types';

// 讀到設定之前（或讀不到時）先顯示的值，對齊後端 SlidingWindowConfig 的預設（ADR-0092、ADR-0305）
const DEFAULT_CONFIG: SlidingWindowConfig = {
    enabled: false,
    keep_rounds: 100,
    upper_limit_rounds: 150,
    trigger_pct: 90,
    target_floor_pct: 50,
    summary_budget_pct: 0,
    min_keep_pairs: 2,
    summary_model: 'claude-haiku-5-5',
    prelude_summary_enabled: true,
    prelude_max_tokens: 4000,
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
