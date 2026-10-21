import { useState } from "react";
import { useTranslation, Trans } from "react-i18next";
import { cn } from "@/lib/utils";
import { Search, Eye, EyeOff, RefreshCw, Trash2, Activity, Loader2 } from "lucide-react";
import { useModels, ModelInfo, ModelHealthResult } from "@/lib/providers/ModelProvider";
import { toast } from "sonner";
import { getErrorMessage } from "@/lib/error_handler";
import { useConfirmToast } from "@/hooks/useConfirmToast";

export function ModelsTab() {
    const { t } = useTranslation();
    const {
        allModels,
        updateModelSetting,
        deleteModel,
        triggerBackendRefresh,
        isRefreshing,
        testModel
    } = useModels();
    const { confirmToast } = useConfirmToast();

    const [modelSearch, setModelSearch] = useState("");
    const [modelVisibilityFilter, setModelVisibilityFilter] = useState<'all' | 'visible' | 'hidden'>('visible');
    // 每列健康探測：進行中旗標與結果（皆以 model.id 為 key，互不干擾）。
    const [testing, setTesting] = useState<Record<string, boolean>>({});
    const [healthResults, setHealthResults] = useState<Record<string, ModelHealthResult>>({});

    const handleTest = async (id: string) => {
        setTesting(prev => ({ ...prev, [id]: true }));
        try {
            const result = await testModel(id);
            setHealthResults(prev => ({ ...prev, [id]: result }));
        } finally {
            setTesting(prev => ({ ...prev, [id]: false }));
        }
    };

    // 把後端穩定機器碼（含 api_error:{n}）對映為在地化文字（禁令 58）。
    const healthReasonText = (error?: string): string => {
        if (!error) return "";
        if (error.startsWith("api_error:")) {
            return t("settings.models_tab.health_reason.api_error", { status: error.split(":")[1] });
        }
        return t(`settings.models_tab.health_reason.${error}`, { defaultValue: error });
    };

    const filteredModels = (allModels as ModelInfo[]).filter((m: ModelInfo) => {
        const matchesSearch = m.id.toLowerCase().includes(modelSearch.toLowerCase()) ||
                             (m.name || "").toLowerCase().includes(modelSearch.toLowerCase());
        const visibility = m.visibility || 'visible';
        const matchesVisibility = modelVisibilityFilter === 'all' ||
                                (modelVisibilityFilter === 'visible' && visibility === 'visible') ||
                                (modelVisibilityFilter === 'hidden' && visibility === 'hidden');
        return matchesSearch && matchesVisibility;
    });

    return (
        <div className="animate-in fade-in slide-in-from-bottom-2 space-y-6 duration-300">
            <div className="flex gap-3">
                <div className="relative flex-1">
                    <Search className="text-muted-foreground absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2" />
                    <input
                        type="text"
                        placeholder={t('settings.models_tab.search_placeholder')}
                        value={modelSearch}
                        onChange={e => setModelSearch(e.target.value)}
                        className="bg-muted/40 border-input focus:ring-primary/50 w-full rounded-2xl border py-3 pr-4 pl-10 text-sm transition-all outline-none focus:ring-2"
                    />
                </div>
                <button
                    onClick={() => triggerBackendRefresh()}
                    disabled={isRefreshing}
                    className={cn(
                        "flex items-center gap-2 px-6 py-2 rounded-2xl border font-bold text-sm transition-all",
                        isRefreshing
                            ? "bg-muted text-muted-foreground border-border cursor-not-allowed"
                            : "bg-primary/10 border-primary/20 text-primary hover:bg-primary/20"
                    )}
                >
                    <RefreshCw className={cn("w-4 h-4", isRefreshing && "animate-spin")} />
                    {isRefreshing ? t('settings.models_tab.scanning') : t('settings.models_tab.force_rescan')}
                </button>
                <select
                    value={modelVisibilityFilter}
                    onChange={(e) => setModelVisibilityFilter(e.target.value as 'all' | 'visible' | 'hidden')}
                    className="bg-muted/40 border-input hover:border-primary/50 rounded-2xl border px-4 py-2 text-sm font-bold transition-all focus:outline-none"
                >
                    <option value="all">{t('settings.models_tab.filter_all')}</option>
                    <option value="visible">{t('settings.models_tab.filter_visible')}</option>
                    <option value="hidden">{t('settings.models_tab.filter_hidden')}</option>
                </select>
            </div>

            {/* HAX G14（H3）：變更後果說明——讓「開關/可見性」這類會改變對話可選項的設定，事前對使用者透明。 */}
            <p className="text-muted-foreground px-1 text-xs">
                <Trans i18nKey="settings.models_tab.change_notice">
                    「開啟／關閉」與「可見／隱藏」變更皆<span className="font-bold">即時生效</span>：關閉或隱藏的模型不會出現在對話的模型選單，但不影響既有對話內容。
                </Trans>
            </p>

            <div className="space-y-2">
                {filteredModels.length === 0 ? (
                    <div className="rounded-3xl border-2 border-dashed border-border p-12 text-center text-sm italic text-muted-foreground">
                        {allModels.length === 0
                            ? t('settings.models_tab.empty_no_models')
                            : t('settings.models_tab.empty_no_match')}
                    </div>
                ) : filteredModels.map((model: ModelInfo) => {
                        const isOnline = model.status === 'online';
                        const health = healthResults[model.id];
                        const isTesting = testing[model.id];
                        const healthColor = health?.status === 'healthy' ? 'text-status-success'
                            : health?.status === 'degraded' ? 'text-status-warning' : 'text-status-error';
                        const healthLine = health ? [
                            t(`settings.models_tab.health_status.${health.status}`),
                            ...(health.status === 'healthy' && health.latency_ms != null
                                ? [t('settings.models_tab.health_latency', { ms: Math.round(health.latency_ms) })] : []),
                            ...(health.error ? [healthReasonText(health.error)] : []),
                        ].join(' · ') : '';
                        return (
                            <div key={model.id} className="space-y-2">
                            <div className={cn(
                                "flex items-center justify-between rounded-2xl border border-border bg-muted/20 p-4 transition-all hover:border-primary/30 group",
                                !isOnline && "grayscale opacity-70"
                            )}>
                                <div className="flex min-w-0 flex-col">
                                    <div className="flex items-center gap-2">
                                        <div className={cn(
                                            "h-2 w-2 rounded-full",
                                            isOnline ? "bg-primary shadow-sm" : "bg-destructive shadow-sm"
                                        )} title={isOnline ? t('settings.models_tab.status_online') : t('settings.models_tab.status_offline')} />
                                        <span className="text-foreground truncate font-bold">{model.name || model.id}</span>
                                        {!model.enabled && isOnline && <span className="rounded bg-primary px-1.5 py-0.5 text-xs font-black uppercase text-primary-foreground">{t('settings.models_tab.badge_available')}</span>}
                                    </div>
                                    <span className="text-muted-foreground pl-4 font-mono text-xs tracking-widest uppercase">{model.owned_by || 'Provider'}</span>
                                    {health && <span className={cn("pl-4 text-xs font-bold", healthColor)}>{healthLine}</span>}
                                </div>
                                <div className="flex shrink-0 items-center gap-3">
                                    <button
                                        onClick={() => handleTest(model.id)}
                                        disabled={isTesting}
                                        className="text-muted-foreground hover:bg-primary/10 hover:text-primary rounded-xl p-2 transition-all disabled:opacity-50"
                                        title={t('settings.models_tab.test_button_title')}
                                        aria-label={t('settings.models_tab.test_button')}
                                    >
                                        {isTesting ? <Loader2 size={16} className="animate-spin" /> : <Activity size={16} />}
                                    </button>
                                    <div className="flex rounded-xl border border-border bg-background p-1">
                                        <button
                                            onClick={() => updateModelSetting(model.id, { enabled: true })}
                                            className={cn("rounded-lg px-3 py-1.5 text-xs font-black transition-all", model.enabled ? "bg-primary-button text-white shadow-sm" : "text-muted-foreground hover:text-foreground")}
                                            title={t('settings.models_tab.enable_title')}
                                        >
                                            {t('settings.models_tab.enable')}
                                        </button>
                                        <button
                                            onClick={() => updateModelSetting(model.id, { enabled: false })}
                                            className={cn("rounded-lg px-3 py-1.5 text-xs font-black transition-all", !model.enabled ? "bg-destructive text-white shadow-sm" : "text-muted-foreground hover:text-foreground")}
                                            title={t('settings.models_tab.disable_title')}
                                        >
                                            {t('settings.models_tab.disable')}
                                        </button>
                                    </div>
                                    <button
                                        onClick={() => updateModelSetting(model.id, { visibility: model.visibility === 'visible' ? 'hidden' : 'visible' })}
                                        className={cn("flex items-center gap-2 rounded-xl border transition-all p-2", model.visibility === 'visible' ? "border-primary bg-primary-button text-primary-foreground" : "border-border bg-muted text-muted-foreground")}
                                        title={model.visibility === 'visible' ? t('settings.models_tab.visibility_visible_title') : t('settings.models_tab.visibility_hidden_title')}
                                    >
                                        {model.visibility === 'visible' ? <Eye size={16} /> : <EyeOff size={16} />}
                                        <span className="text-xs font-bold uppercase">{model.visibility === 'visible' ? t('settings.models_tab.visible') : t('settings.models_tab.hidden')}</span>
                                    </button>

                                    <button
                                        onClick={() => {
                                            confirmToast(
                                                t('settings.models_tab.delete_confirm', { id: model.id }),
                                                async () => {
                                                    try {
                                                        await deleteModel(model.id);
                                                        toast.success(t('settings.models_tab.delete_success', { id: model.id }));
                                                    } catch (e) {
                                                        const errMsg = await getErrorMessage(e, "MODEL_DELETE_FAILED");
                                                        toast.error(errMsg);
                                                    }
                                                },
                                                t('settings.models_tab.delete_confirm_label'),
                                            );
                                        }}
                                        className="text-muted-foreground hover:bg-destructive hover:text-destructive-foreground rounded-xl p-2 transition-all"
                                        title={t('settings.models_tab.delete_title')}
                                    >
                                        <Trash2 size={16} />
                                    </button>
                                </div>
                            </div>
                            </div>
                        );
                    })}
            </div>
        </div>
    );
}
