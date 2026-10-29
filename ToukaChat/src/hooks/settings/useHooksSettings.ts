import { useState, useCallback, useEffect } from 'react';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';
import type { HooksConfig, HooksConfigResponse, HookRun } from '@/lib/types';

/** 存檔被拒收的路徑（HOOK_PATH_REJECTED 的 problems）。 */
export interface HookPathProblem {
    event: string;
    where: string;
    reason: string;
    value: string;
}

/** 存檔結果：成功、被別人改過、路徑被拒收、欄位不合規則（第一個錯誤的位置與訊息）、其他失敗。 */
export type HookSaveResult =
    | { kind: 'saved' }
    | { kind: 'conflict' }
    | { kind: 'path_rejected'; problems: HookPathProblem[] }
    | { kind: 'invalid'; where: string; message: string }
    | { kind: 'failed' };

const HOOKS_PATH = '/v1/hooks';
const RUNS_PATH = '/v1/hooks/runs';

function isHooksResponse(data: unknown): data is HooksConfigResponse {
    const value = data as HooksConfigResponse | null;
    return !!value && typeof value.config === 'object' && typeof value.health === 'object'
        && Array.isArray(value.wired_events) && Array.isArray(value.matcher_events);
}

async function readSaveError(res: Response): Promise<HookSaveResult> {
    const data = await res.json().catch(() => null);
    const detail = data?.detail;
    if (detail?.code === 'HOOK_CONFIG_CONFLICT') return { kind: 'conflict' };
    if (detail?.code === 'HOOK_PATH_REJECTED') {
        return { kind: 'path_rejected', problems: Array.isArray(detail.problems) ? detail.problems : [] };
    }
    if (res.status === 422 && Array.isArray(detail) && detail.length > 0) {
        const first = detail[0];
        const where = Array.isArray(first?.loc) ? first.loc.filter((p: unknown) => p !== 'body').join('.') : '';
        return { kind: 'invalid', where, message: String(first?.msg ?? '') };
    }
    return { kind: 'failed' };
}

/** 讀設定；失敗（含非管理員的 403）回 null。 */
async function requestConfig(): Promise<HooksConfigResponse | null> {
    try {
        const res = await fetchTouka(getActiveProxyUrl(), HOOKS_PATH);
        const body: unknown = res.ok ? await res.json() : null;
        return isHooksResponse(body) ? body : null;
    } catch (err) {
        logSSOTError(err, 'useHooksSettings:fetch');
        return null;
    }
}

async function requestRuns(): Promise<HookRun[] | null> {
    try {
        const res = await fetchTouka(getActiveProxyUrl(), RUNS_PATH);
        const body = res.ok ? await res.json() : null;
        return Array.isArray(body?.runs) ? body.runs : null;
    } catch (err) {
        logSSOTError(err, 'useHooksSettings:runs');
        return null;
    }
}

/**
 * Hook 設定頁的資料層（ADR-0292）：讀設定與狀態、存檔（帶載入時的 revision）、讀最近的執行紀錄。
 * 只有人類管理員打得通；其他人後端回 403，這裡當成讀取失敗。
 */
export function useHooksSettings() {
    const [data, setData] = useState<HooksConfigResponse | null>(null);
    const [runs, setRuns] = useState<HookRun[]>([]);
    const [loading, setLoading] = useState(true);
    const [loadFailed, setLoadFailed] = useState(false);
    const [saving, setSaving] = useState(false);

    const applyConfig = useCallback((body: HooksConfigResponse | null) => {
        if (body) setData(body);
        setLoadFailed(body === null);
        setLoading(false);
    }, []);

    const fetchConfig = useCallback(async () => applyConfig(await requestConfig()), [applyConfig]);

    const fetchRuns = useCallback(async () => {
        const list = await requestRuns();
        if (list) setRuns(list);
    }, []);

    const saveConfig = useCallback(async (config: HooksConfig): Promise<HookSaveResult> => {
        if (!data) return { kind: 'failed' };
        setSaving(true);
        try {
            const res = await fetchTouka(getActiveProxyUrl(), HOOKS_PATH, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ config, revision: data.health.revision ?? null }),
            });
            if (!res.ok) return await readSaveError(res);
            const saved = await res.json().catch(() => null);
            const fresh = await requestConfig();
            if (fresh) applyConfig(fresh);
            else if (typeof saved?.revision === 'string') {
                // 存好了但重新讀取失敗：留著剛存的內容和新的 revision，下一次存檔才不會誤判成衝突
                setData(prev => prev && { ...prev, config, health: { ...prev.health, revision: saved.revision } });
            }
            return { kind: 'saved' };
        } catch (err) {
            logSSOTError(err, 'useHooksSettings:save');
            return { kind: 'failed' };
        } finally {
            setSaving(false);
        }
    }, [data, applyConfig]);

    useEffect(() => {
        let alive = true;
        const load = async () => {
            const [config, list] = await Promise.all([requestConfig(), requestRuns()]);
            if (!alive) return;
            applyConfig(config);
            if (list) setRuns(list);
        };
        void load();
        return () => {
            alive = false;
        };
    }, [applyConfig]);

    return { data, runs, loading, loadFailed, saving, fetchConfig, fetchRuns, saveConfig };
}
