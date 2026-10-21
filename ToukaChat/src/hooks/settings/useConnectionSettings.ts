import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { useLocalStorage } from '@/hooks/useLocalStorage';
import { Connection } from '@/lib/types';
import { applyConnectionEdit, type ConnectionDraft } from '@/lib/connectionEdit';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { getSystemEpoch } from '@/lib/time_utils';
import { logSSOTError, errorMsg } from '@/lib/error_handler';

export function useConnectionSettings(onConnectionsUpdated?: () => void) {
    const { t } = useTranslation();
    // useLocalStorage 統一管理 JSON 序列化與持久化（LOCALSTORAGE_SSOT）
    const [connections, setConnections] = useLocalStorage<Connection[]>("touka_connections", []);
    // 後端 GET /status 以 connection id 為 key，值為已消毒的 'ok' / 'connection_error'（禁令 23）。
    const [connectionStatus, setConnectionStatus] = useState<Record<string, string>>({});

    const fetchConnections = useCallback(async () => {
        try {
            const proxyUrl = getActiveProxyUrl();
            const cRes = await fetchTouka(proxyUrl, "/v1/ui/connections");
            if (cRes.ok) {
                const conns = await cRes.json();
                if (conns && Array.isArray(conns) && conns.length > 0) {
                    setConnections(conns); // useLocalStorage 自動持久化
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

    const saveConnections = async (newConns: Connection[]): Promise<boolean> => {
        const prev = connections; // 失敗時還原樂觀更新，避免 UI 與後端不一致
        setConnections(newConns); // useLocalStorage 自動持久化
        if (typeof window !== 'undefined') {
            window.dispatchEvent(new CustomEvent("touka_connections_updated", { detail: newConns }));
        }

        const proxyUrl = getActiveProxyUrl();
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
            setConnections(prev); // 還原
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
