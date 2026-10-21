import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Pencil, X, Plug, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Connection } from "@/lib/types";
import { Checkbox } from "@/components/ui/checkbox";
import { Button } from "@/components/ui/button";

interface Props {
    conn: Connection;
    connectionStatus: Record<string, string>;
    isActive: boolean;
    onToggle: (id: string, enabled: boolean) => void;
    onRemove: (id: string) => void;
    onEdit: (conn: Connection) => void;
    onTest?: (id: string) => Promise<void>;
}

export function ConnectionItem({ conn, connectionStatus, isActive, onToggle, onRemove, onEdit, onTest }: Props) {
    const { t } = useTranslation();
    // 後端 GET /status 以 connection id 為 key，回傳已消毒的狀態碼（禁令 23）；前端映射為在地化標籤。
    const hasError = connectionStatus[conn.id] === 'connection_error';
    const errorLabel = t('settings.connection_item.connection_error');
    const [testing, setTesting] = useState(false);

    const handleTestClick = async () => {
        if (!onTest) return;
        setTesting(true);
        try { await onTest(conn.id); } finally { setTesting(false); }
    };

    return (
        <div className="p-4 bg-muted/40 rounded-2xl border border-border flex items-center justify-between group hover:border-primary/30 transition-all">
            <div className="flex items-center gap-4">
                <Checkbox
                    checked={conn.enabled}
                    onCheckedChange={(checked) => onToggle(conn.id, !!checked)}
                    className="w-5 h-5 rounded-lg border-primary/20 data-[state=checked]:bg-primary data-[state=checked]:border-primary"
                />
                <div className="space-y-1">
                    <div className={cn("text-base font-bold flex items-center gap-2 text-foreground", !conn.enabled && "opacity-50")}>
                        {conn.name}
                        {conn.enabled && !hasError && (
                            <div className="h-2 w-2 rounded-full bg-primary shadow-sm" />
                        )}
                        {conn.enabled && hasError && (
                            <div className="h-2 w-2 rounded-full bg-destructive shadow-sm" title={errorLabel} />
                        )}
                        {conn.enabled && hasError && <span className="ml-1 text-xs font-bold text-destructive">{errorLabel}</span>}
                        {isActive && (
                            <span className="rounded-lg bg-primary/15 px-2 py-0.5 text-xs font-bold uppercase text-primary" title={t('settings.connection_item.active_badge_title')}>
                                {t('settings.connection_item.active_badge')}
                            </span>
                        )}
                        <span className="rounded-lg bg-muted px-2 py-0.5 text-xs font-bold uppercase opacity-70">
                            {conn.protocol || 'openai'}
                        </span>
                    </div>
                    <div className="max-w-48 truncate font-mono text-xs text-muted-foreground opacity-60 sm:max-w-md">
                        {conn.url}
                    </div>
                </div>
            </div>
            <div className="flex items-center gap-1">
                {conn.enabled && onTest && (
                    <Button
                        variant="ghost"
                        size="icon"
                        disabled={testing}
                        aria-label={t('settings.connection_item.test_aria', { name: conn.name })}
                        onClick={handleTestClick}
                        className="text-muted-foreground hover:text-primary hover:bg-primary/10 rounded-xl transition-all opacity-0 group-hover:opacity-100"
                    >
                        {testing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plug className="w-4 h-4" />}
                    </Button>
                )}
                <Button
                    variant="ghost"
                    size="icon"
                    aria-label={t('connections.edit.edit_aria', { name: conn.name })}
                    onClick={() => onEdit(conn)}
                    className="text-muted-foreground hover:text-primary hover:bg-primary/10 rounded-xl transition-all opacity-0 group-hover:opacity-100"
                >
                    <Pencil className="w-4 h-4" />
                </Button>
                <Button
                    variant="ghost"
                    size="icon"
                    aria-label={t('settings.connection_item.remove_connection_aria', { name: conn.name })}
                    onClick={() => onRemove(conn.id)}
                    className="text-muted-foreground hover:text-destructive hover:bg-destructive/10 rounded-xl transition-all opacity-0 group-hover:opacity-100"
                >
                    <X className="w-5 h-5" />
                </Button>
            </div>
        </div>
    );
}
