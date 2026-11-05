import { useCallback, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { useConfirmToast } from '@/hooks/useConfirmToast';
import { useLocalStorage } from '@/hooks/useLocalStorage';
import { Connection } from '@/lib/types';
import { applyConnectionEdit, type ConnectionDraft } from '@/lib/connectionEdit';
import {
    CONNECTIONS_STORAGE_KEY,
    PROXY_PROBE_TIMEOUT_MS,
    activeProxyUrlOf,
    captureRequestScope,
    fetchTouka,
    getActiveProxyUrl,
    isProxyReachable,
    isRequestScopeCurrent,
    keepActiveProxy,
    logoutTouka,
    proxyDisplayUrl,
    publishConnections,
    type RequestScope,
} from '@/lib/connections';
import { getSystemEpoch } from '@/lib/time_utils';
import { logSSOTError, errorMsg } from '@/lib/error_handler';

export function useConnectionSettings(onConnectionsUpdated?: () => void) {
    const { t } = useTranslation();
    const { confirmToast } = useConfirmToast();
    // useLocalStorage 統一管理 JSON 序列化與持久化（LOCALSTORAGE_SSOT）
    const [connections, setConnections] = useLocalStorage<Connection[]>(CONNECTIONS_STORAGE_KEY, []);
    // 後端 GET /status 以 connection id 為 key，值為已消毒的 'ok' / 'connection_error'（禁令 23）。
    const [connectionStatus, setConnectionStatus] = useState<Record<string, string>>({});
    const switching = useRef(false);

    const fetchConnections = useCallback(async () => {
        try {
            const proxyUrl = getActiveProxyUrl();
            const cRes = await fetchTouka(proxyUrl, "/v1/ui/connections");
            if (cRes.ok) {
                const conns = await cRes.json();
                // 等回應的時候已換後端，這份清單就不是現在這台的了。
                if (conns && Array.isArray(conns) && conns.length > 0 && proxyUrl === getActiveProxyUrl()) {
                    setConnections(keepActiveProxy(conns)); // useLocalStorage 自動持久化
                }
            }
        } catch (err) {
            logSSOTError(err, "useConnectionSettings:fetch");
        }
    }, [setConnections]);

    const fetchConnectionStatus = useCallback(async (): Promise<Record<string, string>> => {
        try {
            const proxyUrl = getActiveProxyUrl();
            const res = await fetchTouka(proxyUrl, "/v1/ui/connections/status");
            if (res.ok) {
                const data = await res.json();
                if (data && typeof data === 'object') {
                    const statusMap = data as Record<string, string>;
                    setConnectionStatus(statusMap);
                    return statusMap;
                }
            }
        } catch (err) {
            logSSOTError(err, "useConnectionSettings:status");
        }
        return {};
    }, []);

    // 主動測試已儲存的連線：重用既有 refresh→status 鏈（零新後端）。
    // 注意語意：後端被動掃描「抓不到模型不報錯」，故成功＝「連線可達/無掃描錯誤」而非「確認可用」。
    const testConnection = async (id: string): Promise<void> => {
        const proxyUrl = getActiveProxyUrl();
        try {
            await fetchTouka(proxyUrl, "/v1/models/refresh", { method: 'POST' });
            const statusMap = await fetchConnectionStatus();
            if (statusMap[id] === 'connection_error') {
                toast.error(t('settings.connection_item.test_failed'));
            } else {
                toast.success(t('settings.connection_item.test_ok'));
            }
        } catch (err) {
            logSSOTError(err, "useConnectionSettings:test");
            toast.error(errorMsg('NETWORK_CONNECTION_ERROR'));
        }
    };

    // 換後端：確認新網址是 Touka → 向舊後端登出 → 切過去（AuthGuard 回登入頁，登入頁顯示目前網址）。
    // 清單不送給任何後端：舊的快要不算數，新的還沒登入，而且清單裡可能有本機還沒遮罩的金鑰。
    const switchProxy = async (newConns: Connection[], next: string, scope: RequestScope | null) => {
        if (switching.current || !scope || !isRequestScopeCurrent(scope)) return;
        switching.current = true;
        const url = proxyDisplayUrl(next);
        const toastId = toast.loading(t('settings.connections_tab.proxy_checking', { url }));
        try {
            if (!await isProxyReachable(next)) {
                toast.error(t('settings.connections_tab.proxy_unreachable', { url }), { id: toastId });
                return;
            }
            // 探測最長幾秒，這段時間可能已登出或別的分頁已換網址；那時已經不是當初確認的狀態。
            if (!isRequestScopeCurrent(scope)) {
                toast.dismiss(toastId);
                return;
            }
            // 舊後端刪掉 cookie 才切，新網址第一個請求就不帶 cookie；舊後端連不上時等到上限照樣切。
            await logoutTouka(scope.proxyOrigin, AbortSignal.timeout(PROXY_PROBE_TIMEOUT_MS));
            toast.dismiss(toastId);
            if (getActiveProxyUrl() !== scope.proxyOrigin) return;
            publishConnections(newConns);
            setConnections(newConns);
        } finally {
            switching.current = false;
        }
    };

    const saveConnections = async (newConns: Connection[]): Promise<boolean> => {
        if (switching.current) return false;
        const proxyUrl = getActiveProxyUrl();
        const next = activeProxyUrlOf(newConns);
        if (next !== proxyUrl) {
            const scope = captureRequestScope();
            confirmToast(
                t('settings.connections_tab.proxy_switch_confirm', { url: proxyDisplayUrl(next) }),
                () => switchProxy(newConns, next, scope),
                t('settings.connections_tab.proxy_switch_confirm_label'),
            );
            return false;
        }

        const prev = connections; // 失敗時還原樂觀更新，避免 UI 與後端不一致
        publishConnections(newConns);
        setConnections(newConns); // useLocalStorage 自動持久化

        try {
            const res = await fetchTouka(proxyUrl, "/v1/ui/connections", {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newConns)
            });
            if (!res.ok) throw new Error(`save connections ${res.status}`);
            await fetchTouka(proxyUrl, "/v1/models/refresh", { method: 'POST' });
            if (onConnectionsUpdated) onConnectionsUpdated();
            await fetchConnections();
            await fetchConnectionStatus();
            toast.success(t('settings.connections_tab.saved'));
            return true;
        } catch (_err) {
            logSSOTError(_err, "useConnectionSettings:sync");
            if (getActiveProxyUrl() === proxyUrl) setConnections(prev); // 還原；網址已被別處換掉就不蓋回舊的
            toast.error(errorMsg('CONFIG_SAVE_FAILED'));
            return false;
        }
    };

    const addConnection = (conn: Omit<Connection, 'id'>) => {
        const newConns = [...connections, { ...conn, id: "conn-" + getSystemEpoch() }];
        saveConnections(newConns);
    };

    const removeConnection = (id: string) => {
        saveConnections(connections.filter(c => c.id !== id));
    };

    // 編輯：金鑰保留邏輯交給共用純函式 applyConnectionEdit（changeKey=false 時保留既有金鑰）。
    const updateConnection = (id: string, draft: ConnectionDraft, changeKey: boolean) => {
        saveConnections(connections.map(c => c.id === id ? applyConnectionEdit(c, draft, changeKey) : c));
    };

    return {
        connections,
        connectionStatus,
        fetchConnections,
        fetchConnectionStatus,
        testConnection,
        saveConnections,
        addConnection,
        removeConnection,
        updateConnection
    };
}
