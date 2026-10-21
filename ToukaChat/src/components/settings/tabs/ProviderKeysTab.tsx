import { useState } from "react";
import { useTranslation } from "react-i18next";
import { KeyRound, Save, Loader2, AlertTriangle } from "lucide-react";
import { useProviderKeySettings } from "@/hooks/settings/useProviderKeySettings";
import { ProviderKeyInputs } from "@/components/common/ProviderKeyInputs";
import { PROVIDER_KEYS } from "@/lib/providerKeys";
import { errorMsg } from "@/lib/error_handler";
import { useConfirmToast } from "@/hooks/useConfirmToast";
import { useDirtyGuard } from "@/components/settings/SettingsDirtyContext";

export function ProviderKeysTab() {
    const { t } = useTranslation();
    const { statuses, loading, saving, lastProbe, saveKeys, clearKey } = useProviderKeySettings();
    const { confirmToast } = useConfirmToast();
    // 新值輸入（env → 字串）。永遠由空開始；既有金鑰絕不回填（hook 只給 boolean 狀態）。
    const [inputs, setInputs] = useState<Record<string, string>>({});
    const [saved, setSaved] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const handleChange = (env: string, val: string) => {
        setInputs(prev => ({ ...prev, [env]: val }));
        setSaved(false);
        setError(null);
    };

    const hasInput = Object.values(inputs).some(v => v && v.trim());
    useDirtyGuard("provider-keys", hasInput);  // C5：未存草稿守衛

    const handleSave = async () => {
        const ok = await saveKeys(inputs);
        if (ok) {
            setInputs({});
            setSaved(true);
            setError(null);
        } else {
            // 儲存失敗（最常見為非管理員 403／後端不可達）—— 不再靜默，明示原因。
            setError(errorMsg('CONFIG_SAVE_FAILED'));
        }
    };

    const handleClear = (env: string) => {
        const label = PROVIDER_KEYS.find(p => p.env === env)?.label ?? env;
        confirmToast(
            t("settings.provider_keys_tab.clear_confirm", { provider: label }),
            async () => {
                const ok = await clearKey(env);
                if (ok) {
                    setInputs(prev => { const nextInputs = { ...prev }; delete nextInputs[env]; return nextInputs; });
                    setSaved(false);
                    setError(null);
                } else {
                    setError(errorMsg('CONFIG_SAVE_FAILED'));
                }
            },
        );
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
                        <KeyRound className="w-4 h-4 text-primary" /> {t("settings.provider_keys_tab.title")}
                    </h4>
                    <p className="text-xs text-muted-foreground max-w-2xl leading-relaxed">
                        {t("settings.provider_keys_tab.description")}
                    </p>
                </div>

                <ProviderKeyInputs statuses={statuses} values={inputs} onChange={handleChange} onClear={handleClear} />

                <div className="flex items-center gap-2 rounded-lg border border-border bg-muted/40 p-3 text-xs text-muted-foreground">
                    <AlertTriangle className="w-4 h-4 shrink-0" />
                    <span>{t("settings.provider_keys_tab.restart_notice")}</span>
                </div>

                {lastProbe && Object.keys(lastProbe).length > 0 && (
                    <div className="space-y-1 rounded-lg border border-border bg-muted/30 p-3 text-xs">
                        {Object.entries(lastProbe).map(([env, verdict]) => {
                            const label = PROVIDER_KEYS.find(p => p.env === env)?.label ?? env;
                            const isInvalid = verdict.startsWith("invalid:");
                            const msg = verdict === "valid"
                                ? t("settings.provider_keys_tab.probe_valid")
                                : isInvalid
                                    ? t("settings.provider_keys_tab.probe_invalid", { code: verdict.slice("invalid:".length) })
                                    : t("settings.provider_keys_tab.probe_cleared");
                            return (
                                <div key={env} className={isInvalid ? "text-destructive" : "text-muted-foreground"}>
                                    <span className="font-medium">{label}</span>: {msg}
                                </div>
                            );
                        })}
                    </div>
                )}

                <div className="flex items-center justify-end gap-3">
                    {error && <span className="text-xs text-destructive">{error}</span>}
                    {saved && <span className="text-xs text-status-success">{t("settings.provider_keys_tab.saved")}</span>}
                    <button
                        onClick={handleSave}
                        disabled={!hasInput || saving}
                        className="flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-xs font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90 transition-colors"
                    >
                        {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
                        {t("settings.provider_keys_tab.save")}
                    </button>
                </div>
            </section>
        </div>
    );
}
