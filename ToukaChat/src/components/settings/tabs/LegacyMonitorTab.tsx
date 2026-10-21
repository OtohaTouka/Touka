import { useTranslation, Trans } from "react-i18next";
import { Database, RefreshCw, HardDrive, FileText, CheckCircle2, AlertCircle, Loader2 } from "lucide-react";
import { useLegacySync } from "@/hooks/useLegacySync";

export function LegacyMonitorTab() {
    const { t } = useTranslation();
    // Agent list is loaded dynamically from the backend via useAgents()
    const agents: string[] = [];
    const { 
        stats, 
        loading, 
        isGlobalLoading, 
        fetchAllStats, 
        handleSync 
    } = useLegacySync(agents);

    return (
        <div className="space-y-6 animate-in fade-in slide-in-from-bottom-2 duration-300">
            <section className="space-y-4">
                <div className="flex items-center justify-between">
                    <div className="space-y-1">
                        <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                            <Database className="w-4 h-4 text-primary" /> {t('settings.legacy_monitor_tab.title')}
                        </h4>
                        <p className="text-xs text-muted-foreground max-w-2xl leading-relaxed">
                            {t('settings.legacy_monitor_tab.description')}
                        </p>
                    </div>
                    <button 
                        onClick={fetchAllStats}
                        disabled={isGlobalLoading}
                        className="p-2 hover:bg-muted rounded-full transition-colors disabled:opacity-50"
                    >
                        <RefreshCw className={`w-4 h-4 ${isGlobalLoading ? 'animate-spin' : ''}`} />
                    </button>
                </div>
            </section>

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {agents.map(agentId => {
                    const s = stats[agentId];
                    const isSyncing = loading[agentId];

                    return (
                        <div key={agentId} className="group relative bg-muted/20 border border-border rounded-2xl p-5 hover:border-primary/50 transition-all shadow-sm">
                            <div className="flex items-start justify-between mb-4">
                                <div className="space-y-1">
                                    <h5 className="font-black text-sm text-foreground uppercase tracking-wider">{agentId}</h5>
                                    <div className="flex items-center gap-1.5">
                                        {s && s.pending_files === 0 ? (
                                            <span className="flex items-center gap-1 rounded-full bg-status-success/10 px-2 py-0.5 text-xs font-bold text-status-success">
                                                <CheckCircle2 className="w-3 h-3" /> {t('settings.legacy_monitor_tab.status_synced')}
                                            </span>
                                        ) : (
                                            <span className="flex items-center gap-1 rounded-full bg-status-warning/10 px-2 py-0.5 text-xs font-bold text-status-warning">
                                                <AlertCircle className="w-3 h-3" /> {t('settings.legacy_monitor_tab.status_pending')}
                                            </span>
                                        )}
                                    </div>
                                </div>
                                <button
                                    onClick={() => handleSync(agentId)}
                                    disabled={isSyncing}
                                    className="p-2 bg-background border border-border rounded-xl hover:bg-primary-button hover:text-white transition-all disabled:opacity-50 shadow-sm"
                                    title={t('settings.legacy_monitor_tab.sync_button_tooltip')}
                                >
                                    {isSyncing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                                </button>
                            </div>

                            <div className="space-y-3">
                                <div className="flex items-center justify-between text-xs">
                                    <span className="text-muted-foreground flex items-center gap-1.5">
                                        <FileText className="w-3.5 h-3.5" /> {t('settings.legacy_monitor_tab.message_count_label')}
                                    </span>
                                    <span className="font-mono font-bold">{s?.message_count ?? '---'}</span>
                                </div>
                                <div className="flex items-center justify-between text-xs">
                                    <span className="text-muted-foreground flex items-center gap-1.5">
                                        <HardDrive className="w-3.5 h-3.5" /> {t('settings.legacy_monitor_tab.session_count_label')}
                                    </span>
                                    <span className="font-mono font-bold">{s?.session_count ?? '---'}</span>
                                </div>
                                <div className="mt-4 border-t border-border/50 pt-4">
                                    <div className="flex items-center justify-between text-xs font-bold uppercase tracking-tighter text-muted-foreground">
                                        <span>{t('settings.legacy_monitor_tab.archive_status_label')}</span>
                                        <span className={s?.pending_files ? "text-status-warning" : "text-status-success"}>
                                            {s?.archived_files ?? 0} Arch / {s?.pending_files ?? 0} Pending
                                        </span>
                                    </div>
                                    <div className="w-full bg-muted/50 h-1.5 rounded-full mt-2 overflow-hidden">
                                        <div 
                                            className="h-full bg-primary transition-all duration-1000" 
                                            style={{ width: s ? `${(s.archived_files / (s.archived_files + s.pending_files || 1)) * 100}%` : '0%' }}
                                        />
                                    </div>
                                </div>
                            </div>
                        </div>
                    );
                })}
            </div>

            <div className="bg-primary/5 border border-primary/20 rounded-2xl p-4 flex gap-4 items-start">
                <AlertCircle className="w-5 h-5 text-primary shrink-0 mt-0.5" />
                <div className="space-y-1">
                    <p className="text-xs font-bold text-primary">{t('settings.legacy_monitor_tab.guard_tip_title')}</p>
                    <p className="text-xs leading-relaxed text-muted-foreground">
                        <Trans i18nKey="settings.legacy_monitor_tab.guard_tip_body" components={{ code: <code /> }} />
                    </p>
                </div>
            </div>
        </div>
    );
}
