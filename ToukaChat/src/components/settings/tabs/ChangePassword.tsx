import React, { useState } from "react";
import { useTranslation } from "react-i18next";
import { KeyRound, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { authService } from "@/lib/api/authService";
import { useAuth } from "@/contexts/AuthContext";
import { getResponseErrorCode, logSSOTError } from "@/lib/error_handler";
import { useDirtyGuard } from "@/components/settings/SettingsDirtyContext";

/** 回傳違規規則的 i18n 後綴（errors.*），合格回 null。後端僅強制 min_length=8，其餘為前端強度 UX。 */
function validatePassword(pw: string): string | null {
    if (pw.length < 8) return "too_short";
    if (!/[A-Z]/.test(pw)) return "no_upper";
    if (!/[a-z]/.test(pw)) return "no_lower";
    if (!/\d/.test(pw)) return "no_digit";
    if (!/[^A-Za-z0-9]/.test(pw)) return "no_special";
    return null;
}

const PASSWORD_RULES: { key: string; test: (pw: string) => boolean }[] = [
    { key: "length", test: (pw) => pw.length >= 8 },
    { key: "upper", test: (pw) => /[A-Z]/.test(pw) },
    { key: "lower", test: (pw) => /[a-z]/.test(pw) },
    { key: "digit", test: (pw) => /\d/.test(pw) },
    { key: "special", test: (pw) => /[^A-Za-z0-9]/.test(pw) },
];

/**
 * 自助修改密碼卡片（General 分頁帳號區）。每位已登入使用者皆可輪換自己的密碼。
 * 成功後後端撤銷 refresh token，故必須立即登出重登（避免 session 悄悄失效）。
 */
export function ChangePassword() {
    const { t } = useTranslation();
    const { logout } = useAuth();
    const [current, setCurrent] = useState("");
    const [next, setNext] = useState("");
    const [confirm, setConfirm] = useState("");
    const [error, setError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);
    useDirtyGuard("change-password", !!(current || next || confirm));  // C5：未存草稿守衛

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError(null);
        if (next !== confirm) { setError(t("settings.change_password.errors.mismatch")); return; }
        const ruleKey = validatePassword(next);
        if (ruleKey) { setError(t(`settings.change_password.errors.${ruleKey}`)); return; }

        setSaving(true);
        try {
            const res = await authService.changePassword(current, next);
            if (!res.ok) {
                // 目前密碼打錯是 403 AUTH_REAUTH_FAILED（401 會被 fetchTouka 續期重送、再驗一次）
                setError(await getResponseErrorCode(res) === "AUTH_REAUTH_FAILED"
                    ? t("settings.change_password.errors.current_incorrect")
                    : t("settings.change_password.errors.generic"));
                return;
            }
            toast.success(t("settings.change_password.success"));
            await logout(); // refresh token 已被撤銷 → 強制重新登入
        } catch (err) {
            logSSOTError(err, "ChangePassword");
            setError(t("settings.change_password.errors.generic"));
        } finally {
            setSaving(false);
        }
    };

    return (
        <section className="space-y-3">
            <h4 className="text-sm font-bold text-foreground flex items-center gap-2">
                <KeyRound className="w-4 h-4 text-primary" /> {t("settings.change_password.heading")}
            </h4>
            <form onSubmit={handleSubmit} className="bg-muted/20 border border-border rounded-3xl p-6 space-y-4 max-w-lg">
                <div className="space-y-1.5">
                    <label htmlFor="cp-current" className="text-xs font-black uppercase text-muted-foreground px-1 tracking-widest">{t("settings.change_password.current_password")}</label>
                    <input
                        id="cp-current"
                        type="password"
                        value={current}
                        onChange={e => setCurrent(e.target.value)}
                        autoComplete="current-password"
                        required
                        className="w-full bg-background border-input rounded-xl border py-2.5 px-4 text-sm focus:ring-2 focus:ring-primary/50 outline-none transition-all"
                    />
                </div>
                <div className="space-y-1.5">
                    <label htmlFor="cp-new" className="text-xs font-black uppercase text-muted-foreground px-1 tracking-widest">{t("settings.change_password.new_password")}</label>
                    <input
                        id="cp-new"
                        type="password"
                        value={next}
                        onChange={e => setNext(e.target.value)}
                        autoComplete="new-password"
                        minLength={8}
                        required
                        className="w-full bg-background border-input rounded-xl border py-2.5 px-4 text-sm focus:ring-2 focus:ring-primary/50 outline-none transition-all"
                    />
                </div>
                <div className="rounded-xl bg-muted/40 p-3 space-y-1">
                    <p className="text-xs font-medium text-muted-foreground">{t("settings.change_password.requirements")}</p>
                    <ul className="space-y-0.5">
                        {PASSWORD_RULES.map(r => {
                            const ok = r.test(next);
                            return (
                                <li key={r.key} className={`text-xs flex items-center gap-1.5 ${ok ? "text-status-success" : "text-muted-foreground"}`}>
                                    <span>{ok ? "✓" : "○"}</span>
                                    {t(`settings.change_password.rules.${r.key}`)}
                                </li>
                            );
                        })}
                    </ul>
                </div>
                <div className="space-y-1.5">
                    <label htmlFor="cp-confirm" className="text-xs font-black uppercase text-muted-foreground px-1 tracking-widest">{t("settings.change_password.confirm_password")}</label>
                    <input
                        id="cp-confirm"
                        type="password"
                        value={confirm}
                        onChange={e => setConfirm(e.target.value)}
                        autoComplete="new-password"
                        minLength={8}
                        required
                        className="w-full bg-background border-input rounded-xl border py-2.5 px-4 text-sm focus:ring-2 focus:ring-primary/50 outline-none transition-all"
                    />
                </div>
                {error && <p className="text-destructive text-sm">{error}</p>}
                <div className="flex justify-end">
                    <Button type="submit" disabled={saving} className="px-8 h-10 rounded-xl font-black text-sm">
                        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : t("settings.change_password.submit")}
                    </Button>
                </div>
            </form>
        </section>
    );
}
