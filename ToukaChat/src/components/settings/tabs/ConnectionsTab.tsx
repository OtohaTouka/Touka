import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { useConnectionSettings } from "@/hooks/settings/useConnectionSettings";
import { useConfirmToast } from "@/hooks/useConfirmToast";
import { Connection } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { ConnectionItem } from "./connections/ConnectionItem";
import { AddConnectionForm } from "./connections/AddConnectionForm";

interface Props {
    isSystemRefreshing: boolean;
    onRefresh: () => void;
}

export function ConnectionsTab({ isSystemRefreshing, onRefresh }: Props) {
    const { t } = useTranslation();
    const {
        connections,
        connectionStatus,
        fetchConnections,
        fetchConnectionStatus,
        testConnection,
        saveConnections,
        addConnection,
        removeConnection,
        updateConnection
    } = useConnectionSettings();
    const { confirmToast } = useConfirmToast();
    const [editing, setEditing] = useState<Connection | null>(null);

    // 使用中連線：與 getActiveConnection()（connections.ts:87）相同判定，改自 React state 推導以即時反映勾選。
    const activeId = connections.find(c => c.enabled && (c.provider === 'proxy' || c.id === 'proxy'))?.id ?? null;

    useEffect(() => {
        fetchConnections();
        fetchConnectionStatus();
    }, [fetchConnections, fetchConnectionStatus]);

    const handleToggle = (id: string, enabled: boolean) => {
        const updated = connections.map(c => c.id === id ? { ...c, enabled } : c);
        saveConnections(updated);
    };

    const handleRemove = (id: string) => {
        const conn = connections.find(c => c.id === id);
        confirmToast(
            t('settings.connections_tab.delete_confirm', { name: conn?.name ?? id }),
            () => {
                if (editing?.id === id) setEditing(null);
                removeConnection(id);
            },
            t('settings.connections_tab.delete_confirm_label'),
        );
    };

    return (
        <div className="space-y-8 animate-in fade-in slide-in-from-bottom-2 duration-300">
            <div className="flex items-center justify-between">
                <h4 className="text-sm font-bold text-foreground">{t('settings.connections_tab.heading')}</h4>
                <Button
                    variant="outline"
                    onClick={onRefresh}
                    disabled={isSystemRefreshing}
                    className="flex items-center gap-2 text-sm px-6 h-11 rounded-full font-bold transition-all shadow-sm active:scale-95 border-primary/20 bg-primary/5 text-primary hover:bg-primary/10"
                >
                    <RefreshCw className={cn("w-4 h-4", isSystemRefreshing && "animate-spin")} />
                    {isSystemRefreshing ? t('settings.connections_tab.refreshing') : t('settings.connections_tab.refresh_system_info')}
                </Button>
            </div>

            <div className="space-y-4">
                {connections.map(conn => (
                    <ConnectionItem
                        key={conn.id}
                        conn={conn}
                        connectionStatus={connectionStatus}
                        isActive={conn.id === activeId}
                        onToggle={handleToggle}
                        onRemove={handleRemove}
                        onEdit={setEditing}
                        onTest={testConnection}
                    />
                ))}
                {connections.length === 0 && (
                    <div className="rounded-3xl border-2 border-dashed border-border p-12 text-center text-sm italic text-muted-foreground">
                        {t('settings.connections_tab.empty_state')}
                    </div>
                )}
            </div>

            <AddConnectionForm
                key={editing ? editing.id : 'add'}
                onAdd={addConnection}
                editing={editing}
                onSave={(id, draft, changeKey) => {
                    updateConnection(id, draft, changeKey);
                    setEditing(null);
                }}
                onCancelEdit={() => setEditing(null)}
            />
        </div>
    );
}