import React, { useState } from "react";
import { useTranslation, Trans } from "react-i18next";
import { toast } from "sonner";
import { History, Save, Loader2, AlertTriangle } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { useSlidingWindowSettings } from "@/hooks/settings/useSlidingWindowSettings";
import { useDirtyGuard } from "@/components/settings/SettingsDirtyContext";

export function SlidingWindowTab() {
    const { t } = useTranslation();
    const { config, loading, saving, saveConfig } = useSlidingWindowSettings();
    const [draft, setDraft] = useState(config);
    const [dirty, setDirty] = useState(false);
    const [error, setError] = useState<string | null>(null);
    useDirtyGuard("sliding-window", dirty);  // C5：未存草稿守衛

    React.useEffect(() => {
        setDraft(config);
        setDirty(false);
    }, [config]);

    const handleChange = <K extends keyof typeof draft>(key: K, value: typeof draft[K]) => {
        setDraft(prev => {
            const next = { ...prev, [key]: value };
            // F8：維持不變式 floor + summary < trigger。調整 trigger/floor 時主動在 state 收斂，
            // 避免滑桿 value 與 max 脫鉤（thumb/label desync）與到存檔才報的無效狀態。
            if (key === "trigger_pct") {
                next.target_floor_pct = Math.min(next.target_floor_pct, Math.max(0, next.trigger_pct - 1));
                next.summary_budget_pct = Math.min(
                    next.summary_budget_pct,
                    Math.max(0, next.trigger_pct - next.target_floor_pct - 1),
                );
            } else if (key === "target_floor_pct") {
                next.summary_budget_pct = Math.min(
                    next.summary_budget_pct,
                    Math.max(0, next.trigger_pct - next.target_floor_pct - 1),
                );
            }
            return next;
        });
        setDirty(true);
        setError(null);
    };

    const handleSave = async () => {
        if (draft.target_floor_pct + draft.summary_budget_pct >= draft.trigger_pct) {
            setError(t("settings.sliding_window_tab.errors.floor_summary_too_high"));
            return;
        }
        const ok = await saveConfig(draft);
        if (ok) {
            setDirty(false);
            setError(null);
            toast.success(t("settings.sliding_window_tab.save_success")); // F4：成功回饋
        } else {
            // F7：伺服器存檔失敗改用 toast（長表單捲動時 inline 易被遮）；inline error 留給用戶端驗證。
            toast.error(t("settings.sliding_window_tab.errors.save_failed"));
        }
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center py-12">
                <Loader2 className="w-6 h-6 animate-spin text-muted-foreground" />
            </div>
        );
    }

    return (
        <div className="space-y-6 animate-in fade-in slide-in-from-bottom-2 duration-300">
            <section className="space-y-4">
                <div className="space-y-1">
                    <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                        <History className="w-4 h-4 text-primary" /> {t("settings.sliding_window_tab.title")}
                    </h4>
                    <p className="text-xs text-muted-foreground max-w-2xl leading-relaxed">
                        {t("settings.sliding_window_tab.description")}
                    </p>
                </div>

                {/* 開關 */}
                <div className="flex items-center justify-between rounded-lg border border-border p-4">
                    <div className="space-y-0.5">
                        <p className="text-sm font-medium text-foreground">{t("settings.sliding_window_tab.enable_label")}</p>
                        <p className="text-xs text-muted-foreground">{t("settings.sliding_window_tab.enable_hint")}</p>
                    </div>
                    <Switch
                        checked={draft.enabled}
                        onCheckedChange={(c) => handleChange("enabled", c)}
                        aria-label={t("settings.sliding_window_tab.enable_label")}
                    />
                </div>

                {/* 數值設定 */}
                <div className={`space-y-4 ${!draft.enabled ? "opacity-50 pointer-events-none" : ""}`}>
                    {/* 觸發百分比 */}
                    <div className="rounded-lg border border-border p-4 space-y-2">
                        <div className="flex items-center justify-between">
                            <div className="space-y-0.5">
                                <p className="text-sm font-medium text-foreground">{t("settings.sliding_window_tab.trigger_pct_label")}</p>
                                <p className="text-xs text-muted-foreground">
                                    {t("settings.sliding_window_tab.trigger_pct_hint")}
                                </p>
                            </div>
                            <span className="text-sm font-mono text-primary w-12 text-right">
                                {draft.trigger_pct}%
                            </span>
                        </div>
                        <input
                            type="range"
                            min={50}
                            max={99}
                            step={1}
                            value={draft.trigger_pct}
                            onChange={e => handleChange("trigger_pct", parseInt(e.target.value))}
                            aria-label={t("settings.sliding_window_tab.trigger_pct_label")}
                            className="w-full"
                        />
                    </div>

                    {/* 地板百分比 */}
                    <div className="rounded-lg border border-border p-4 space-y-2">
                        <div className="flex items-center justify-between">
                            <div className="space-y-0.5">
                                <p className="text-sm font-medium text-foreground">{t("settings.sliding_window_tab.floor_pct_label")}</p>
                                <p className="text-xs text-muted-foreground">
                                    {t("settings.sliding_window_tab.floor_pct_hint")}
                                </p>
                            </div>
                            <span className="text-sm font-mono text-primary w-12 text-right">
                                {draft.target_floor_pct}%
                            </span>
                        </div>
                        <input
                            type="range"
                            min={0}
                            max={Math.max(0, draft.trigger_pct - 1)}
                            step={5}
                            value={draft.target_floor_pct}
                            onChange={e => handleChange("target_floor_pct", parseInt(e.target.value))}
                            aria-label={t("settings.sliding_window_tab.floor_pct_label")}
                            className="w-full"
                        />
                    </div>

                    {/* 摘要百分比 */}
                    <div className="rounded-lg border border-border p-4 space-y-2">
                        <div className="flex items-center justify-between">
                            <div className="space-y-0.5">
                                <p className="text-sm font-medium text-foreground">{t("settings.sliding_window_tab.summary_pct_label")}</p>
                                <p className="text-xs text-muted-foreground">
                                    {t("settings.sliding_window_tab.summary_pct_hint")}
                                </p>
                            </div>
                            <span className="text-sm font-mono text-primary w-12 text-right">
                                {draft.summary_budget_pct}%
                            </span>
                        </div>
                        <input
                            type="range"
                            min={0}
                            max={Math.max(0, draft.trigger_pct - draft.target_floor_pct - 1)}
                            step={5}
                            value={draft.summary_budget_pct}
                            onChange={e => handleChange("summary_budget_pct", parseInt(e.target.value))}
                            aria-label={t("settings.sliding_window_tab.summary_pct_label")}
                            className="w-full"
                        />
                    </div>

                    {/* 設定摘要 */}
                    <div className="rounded-lg bg-muted/50 p-3 text-xs text-muted-foreground space-y-1">
                        <p>
                            {draft.summary_budget_pct > 0 ? (
                                <Trans
                                    i18nKey="settings.sliding_window_tab.summary_line_with_summary"
                                    values={{
                                        trigger: draft.trigger_pct,
                                        floor: draft.target_floor_pct,
                                        summary: draft.summary_budget_pct,
                                        after: draft.target_floor_pct + draft.summary_budget_pct,
                                    }}
                                    components={{ v: <span className="font-mono text-foreground" /> }}
                                />
                            ) : (
                                <Trans
                                    i18nKey="settings.sliding_window_tab.summary_line_no_summary"
                                    values={{
                                        trigger: draft.trigger_pct,
                                        floor: draft.target_floor_pct,
                                    }}
                                    components={{ v: <span className="font-mono text-foreground" /> }}
                                />
                            )}
                        </p>
                    </div>
                </div>

                {/* 錯誤提示 */}
                {error && (
                    <div className="flex items-center gap-2 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-xs text-destructive">
                        <AlertTriangle className="w-4 h-4 shrink-0" />
                        <span>{error}</span>
                    </div>
                )}

                {/* 儲存按鈕 */}
                <div className="flex justify-end gap-2">
                    <button
                        onClick={handleSave}
                        disabled={!dirty || saving}
                        className="flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-xs font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90 transition-colors"
                    >
                        {saving ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        ) : (
                            <Save className="w-3.5 h-3.5" />
                        )}
                        {t("settings.sliding_window_tab.save_button")}
                    </button>
                </div>
            </section>
        </div>
    );
}
