import { useCallback, useEffect, useState } from 'react';
import { fetchTouka, getActiveProxyUrl } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';
import type { LocalHelperStatus, LocalPairingCreated } from '@/lib/types';

const STATUS_PATH = '/v1/local-bridge/status';
const PAIRING_PATH = '/v1/local-bridge/pairing';

/** 設定頁開著時多久更新一次狀態，貼上配對碼後才看得到變成已連線。 */
export const LOCAL_HELPER_REFRESH_MS = 10_000;

async function requestStatus(): Promise<LocalHelperStatus | null> {
    try {
        const res = await fetchTouka(getActiveProxyUrl(), STATUS_PATH);
        return res.ok ? ((await res.json()) as LocalHelperStatus) : null;
    } catch (err) {
        logSSOTError(err, 'useLocalHelper:status');
        return null;
    }
}

async function requestPairing(method: 'POST' | 'DELETE'): Promise<Response | null> {
    try {
        const res = await fetchTouka(getActiveProxyUrl(), PAIRING_PATH, { method });
        return res.ok ? res : null;
    } catch (err) {
        logSSOTError(err, `useLocalHelper:${method}`);
        return null;
    }
}

/**
 * 本機小幫手設定頁的資料層（ADR-0312）。配對碼只放在這個 hook 的記憶體，
 * 不寫進瀏覽器儲存；只有人類管理員打得通，其他人當成讀取失敗。
 */
export function useLocalHelper() {
    const [status, setStatus] = useState<LocalHelperStatus | null>(null);
    const [failed, setFailed] = useState(false);
    const [code, setCode] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);

    const applyStatus = useCallback((next: LocalHelperStatus | null) => {
        setFailed(next === null);
        if (next !== null) setStatus(next);
    }, []);

    const refresh = useCallback(async () => {
        applyStatus(await requestStatus());
    }, [applyStatus]);

    useEffect(() => {
        let active = true;
        const load = () => {
            void requestStatus().then((next) => {
                if (active) applyStatus(next);
            });
        };
        load();
        const timer = setInterval(load, LOCAL_HELPER_REFRESH_MS);
        return () => {
            active = false;
            clearInterval(timer);
        };
    }, [applyStatus]);

    const createPairing = useCallback(async (): Promise<boolean> => {
        setBusy(true);
        try {
            const res = await requestPairing('POST');
            const body = res ? ((await res.json()) as Partial<LocalPairingCreated>) : null;
            if (!body?.code) return false;
            setCode(body.code);
            await refresh();
            return true;
        } finally {
            setBusy(false);
        }
    }, [refresh]);

    const revokePairing = useCallback(async (): Promise<boolean> => {
        setBusy(true);
        try {
            if ((await requestPairing('DELETE')) === null) return false;
            setCode(null);
            await refresh();
            return true;
        } finally {
            setBusy(false);
        }
    }, [refresh]);

    return { status, failed, code, busy, createPairing, revokePairing };
}
