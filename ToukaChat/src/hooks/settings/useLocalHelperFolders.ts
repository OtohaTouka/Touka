import { useEffect, useState } from 'react';
import { fetchTouka, getActiveProxyUrl } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';
import type { LocalFolderList } from '@/lib/types';
import { LOCAL_HELPER_REFRESH_MS } from './useLocalHelper';

const FOLDERS_PATH = '/v1/local-bridge/folders';
const FORBIDDEN = 403;

export type LocalHelperFolders =
    | { kind: 'loading' }
    | { kind: 'denied' }
    | { kind: 'failed' }
    | { kind: 'loaded'; list: LocalFolderList };

async function requestFolders(): Promise<LocalHelperFolders> {
    try {
        const res = await fetchTouka(getActiveProxyUrl(), FOLDERS_PATH);
        if (res.status === FORBIDDEN) return { kind: 'denied' };
        return res.ok ? { kind: 'loaded', list: (await res.json()) as LocalFolderList } : { kind: 'failed' };
    } catch (err) {
        logSSOTError(err, 'useLocalHelperFolders');
        return { kind: 'failed' };
    }
}

/**
 * 小幫手回報的開放名單（ADR-0330）：完整路徑只給配對的人，只放在這個 hook 的記憶體，
 * 不寫進瀏覽器儲存。不是配對的人就停止讀取，不每 10 秒記一次錯。
 */
export function useLocalHelperFolders(enabled: boolean): LocalHelperFolders {
    const [folders, setFolders] = useState<LocalHelperFolders>({ kind: 'loading' });

    useEffect(() => {
        if (!enabled) return undefined;
        let active = true;
        const load = () => {
            void requestFolders().then((next) => {
                if (!active) return;
                setFolders(next);
                if (next.kind === 'denied') clearInterval(timer);
            });
        };
        // load 的回呼是非同步的，一定在 timer 設好之後才跑
        const timer = setInterval(load, LOCAL_HELPER_REFRESH_MS);
        load();
        return () => {
            active = false;
            clearInterval(timer);
        };
    }, [enabled]);

    return folders;
}
