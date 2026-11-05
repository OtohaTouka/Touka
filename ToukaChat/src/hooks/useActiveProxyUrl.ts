import { useSyncExternalStore } from 'react';
import { getActiveProxyUrl } from '@/lib/connections';

function subscribeProxy(notify: () => void) {
    window.addEventListener('touka_connections_updated', notify);
    window.addEventListener('storage', notify);
    return () => {
        window.removeEventListener('touka_connections_updated', notify);
        window.removeEventListener('storage', notify);
    };
}

/** 目前連到的後端網址；這個分頁或別的分頁改了連線都會跟著變。 */
export function useActiveProxyUrl(): string {
    return useSyncExternalStore(subscribeProxy, getActiveProxyUrl, getActiveProxyUrl);
}
