import { useState, useCallback, useEffect } from 'react';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';
import { PROVIDER_KEYS, isKeySet } from '@/lib/providerKeys';

/**
 * 系統級 provider 金鑰設定 hook（SRP/SSOT）。
 *
 * 讀：`GET /v1/config/env`（admin-only）→ 只取 provider 金鑰的「遮罩值」算 set/unset
 * （前端永遠拿不到真值）。寫：`POST /v1/config/env`，且**只送有輸入的鍵**（空輸入＝不變更）。
 */
export function useProviderKeySettings() {
    // env 名 → 是否已設定（boolean，永不存真值）
    const [statuses, setStatuses] = useState<Record<string, boolean>>({});
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    // 後端存檔後對受影響供應商的金鑰探測結果（env → "valid" | "invalid:<code>" | "cleared"）。
    const [lastProbe, setLastProbe] = useState<Record<string, string> | null>(null);

    const fetchStatuses = useCallback(async () => {
        setLoading(true);
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, '/v1/config/env');
            if (res.ok) {
                const data = await res.json();
                const byKey: Record<string, string> = {};
                for (const d of (data.definitions ?? [])) byKey[d.key] = d.value;
                const next: Record<string, boolean> = {};
                for (const p of PROVIDER_KEYS) next[p.env] = isKeySet(byKey[p.env]);
                setStatuses(next);
            }
        } catch (err) {
            logSSOTError(err, 'useProviderKeySettings:fetch');
        } finally {
            setLoading(false);
        }
    }, []);

    const saveKeys = useCallback(async (entries: Record<string, string>): Promise<boolean> => {
        // 只送有輸入（非空）的鍵——空輸入＝不變更、不覆寫既有金鑰。
        const settings: Record<string, string> = {};
        for (const [env, val] of Object.entries(entries)) {
            if (val && val.trim()) settings[env] = val.trim();
        }
        if (Object.keys(settings).length === 0) return false;
        setSaving(true);
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, '/v1/config/env', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ settings }),
            });
            if (res.ok) {
                // 後端即時探測結果（valid/invalid:code/cleared）；供 UI 當場回報。
                const body = await res.json().catch(() => ({}));
                setLastProbe(body?.key_probe ?? null);
                await fetchStatuses();
                return true;
            }
            return false;
        } catch (err) {
            logSSOTError(err, 'useProviderKeySettings:save');
            return false;
        } finally {
            setSaving(false);
        }
    }, [fetchStatuses]);

    // 清除單一金鑰：明確送空字串（繞過 saveKeys 的空值過濾）。
    // 後端對空字串寫 `KEY=`（!= 遮罩佔位）即清除；只動目標 env，不觸及其他鍵的遮罩回送保護。
    const clearKey = useCallback(async (env: string): Promise<boolean> => {
        setSaving(true);
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, '/v1/config/env', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ settings: { [env]: '' } }),
            });
            if (res.ok) {
                const body = await res.json().catch(() => ({}));
                setLastProbe(body?.key_probe ?? null);
                await fetchStatuses();
                return true;
            }
            return false;
        } catch (err) {
            logSSOTError(err, 'useProviderKeySettings:clear');
            return false;
        } finally {
            setSaving(false);
        }
    }, [fetchStatuses]);

    useEffect(() => {
        fetchStatuses();
    }, [fetchStatuses]);

    return { statuses, loading, saving, lastProbe, fetchStatuses, saveKeys, clearKey };
}
