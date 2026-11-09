import { useTranslation } from "react-i18next";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useKeyedModels } from "@/hooks/settings/useKeyedModels";

interface Props {
    model: string;
    preludeSummary: boolean;
    onModelChange: (model: string) => void;
    onPreludeSummaryChange: (on: boolean) => void;
}

/**
 * 摘要模型與前情摘要開關（ADR-0305）。模型只列已設定金鑰的；目前的模型不在清單裡時照樣列出並提醒，
 * 存檔不會悄悄換掉它。
 */
export function SummarySettings({ model, preludeSummary, onModelChange, onPreludeSummaryChange }: Props) {
    const { t } = useTranslation();
    const { models, loadFailed } = useKeyedModels();
    const listed = models ?? [];
    const missingKey = !!model && !!models && !models.some(m => m.id === model);
    const label = t("settings.sliding_window_tab.summary_model_label");

    return (
        <div className="space-y-4">
            <div className="rounded-lg border border-border p-4 space-y-2">
                <div className="space-y-0.5">
                    <p className="text-sm font-medium text-foreground">{label}</p>
                    <p className="text-xs text-muted-foreground">{t("settings.sliding_window_tab.summary_model_hint")}</p>
                </div>
                <Select value={model || undefined} onValueChange={onModelChange}>
                    <SelectTrigger aria-label={label} className="w-full md:w-96 text-xs">
                        <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                        {model && !listed.some(m => m.id === model) && (
                            <SelectItem value={model}>{model}</SelectItem>
                        )}
                        {listed.map(m => (
                            <SelectItem key={m.id} value={m.id}>{`${m.name} (${m.provider})`}</SelectItem>
                        ))}
                    </SelectContent>
                </Select>
                {missingKey && (
                    <p className="text-xs text-status-warning">
                        {t("settings.sliding_window_tab.summary_model_missing_key", { model })}
                    </p>
                )}
                {models?.length === 0 && (
                    <p className="text-xs text-status-warning">{t("settings.sliding_window_tab.summary_model_no_models")}</p>
                )}
                {loadFailed && (
                    <p role="alert" className="text-xs text-status-error">
                        {t("settings.sliding_window_tab.summary_model_load_failed")}
                    </p>
                )}
            </div>

            <div className="flex items-center justify-between rounded-lg border border-border p-4">
                <div className="space-y-0.5">
                    <p className="text-sm font-medium text-foreground">{t("settings.sliding_window_tab.prelude_label")}</p>
                    <p className="text-xs text-muted-foreground">{t("settings.sliding_window_tab.prelude_hint")}</p>
                </div>
                <Switch
                    checked={preludeSummary}
                    onCheckedChange={onPreludeSummaryChange}
                    aria-label={t("settings.sliding_window_tab.prelude_label")}
                />
            </div>
        </div>
    );
}
