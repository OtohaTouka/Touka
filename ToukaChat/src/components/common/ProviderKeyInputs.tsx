import { useTranslation } from "react-i18next";
import { PROVIDER_KEYS } from "@/lib/providerKeys";

interface Props {
    /** env 名 → 是否已設定（boolean，永不含真值）。 */
    statuses: Record<string, boolean>;
    /** env 名 → 目前輸入的新值（write-only；既有金鑰永不帶入）。 */
    values: Record<string, string>;
    onChange: (env: string, val: string) => void;
    /** 選用：清除已設定的金鑰（設定頁傳入；首次精靈 Step3 不傳→不顯示 Clear 鈕）。 */
    onClear?: (env: string) => void;
}

/**
 * 共用呈現元件：列出 provider 金鑰的「已設定/未設定」狀態 ＋ 留空的新值輸入框。
 *
 * 設定頁（ProviderKeysTab）與初次精靈（Step3ApiKeys）共用——金鑰輸入的呈現只有一份（DRY/SSOT）。
 * 安全：只接收 boolean 狀態與「新值」輸入，**永不接收或顯示既有金鑰**。
 */
export function ProviderKeyInputs({ statuses, values, onChange, onClear }: Props) {
    const { t } = useTranslation();
    return (
        <div className="space-y-3">
            {PROVIDER_KEYS.map(p => (
                <div key={p.id} className="rounded-lg border border-border p-4 space-y-2">
                    <div className="flex items-center justify-between">
                        <div className="space-y-0.5">
                            <p className="text-sm font-medium text-foreground">{p.label}</p>
                            <code className="text-xs text-muted-foreground">{p.env}</code>
                            {p.helpUrl && (
                                <a
                                    href={p.helpUrl}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="block text-xs text-primary hover:underline"
                                >
                                    {t("settings.provider_keys_tab.get_key_link")}
                                </a>
                            )}
                        </div>
                        <div className="flex items-center gap-3">
                            <span className={`text-xs font-bold ${statuses[p.env] ? "text-status-success" : "text-muted-foreground"}`}>
                                {statuses[p.env]
                                    ? t("settings.provider_keys_tab.status_set")
                                    : t("settings.provider_keys_tab.status_unset")}
                            </span>
                            {statuses[p.env] && onClear && (
                                <button
                                    type="button"
                                    onClick={() => onClear(p.env)}
                                    className="text-xs font-bold text-status-error hover:underline"
                                >
                                    {t("settings.provider_keys_tab.clear")}
                                </button>
                            )}
                        </div>
                    </div>
                    <input
                        type="password"
                        placeholder={t("settings.provider_keys_tab.new_value_placeholder")}
                        value={values[p.env] ?? ""}
                        onChange={e => onChange(p.env, e.target.value)}
                        className="w-full bg-background border border-border rounded-md px-3 py-2 text-sm"
                    />
                </div>
            ))}
        </div>
    );
}
