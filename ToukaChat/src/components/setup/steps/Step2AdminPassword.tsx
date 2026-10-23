'use client';

import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';
import type { SetupStatus } from '@/hooks/useSetupStatus';

interface Props {
  status: SetupStatus | null;
  /** 保留目前密碼時前進。 */
  onNext: () => void;
  /**
   * 密碼改好了。後端已撤銷這個人所有舊卡（ADR-0144），所以不直接前進，交給上層請使用者重新登入。
   * 傳的是實際被改密碼的使用者；畫面已經卸載（例如背景請求先觸發登入逾期）也照樣呼叫。
   */
  onPasswordChanged: (userId: string) => void;
}

/** 回傳違規規則的 i18n key（setup.step2.errors.*），合格回 null。 */
function validatePassword(pw: string): string | null {
  if (pw.length < 8) return "too_short";
  if (!/[A-Z]/.test(pw)) return "no_upper";
  if (!/[a-z]/.test(pw)) return "no_lower";
  if (!/\d/.test(pw)) return "no_digit";
  if (!/[^A-Za-z0-9]/.test(pw)) return "no_special";
  return null;
}

// 事前顯示的密碼需求清單（隨輸入即時更新 ✓/○）。
const PASSWORD_RULES: { key: string; test: (pw: string) => boolean }[] = [
  { key: "length", test: (pw) => pw.length >= 8 },
  { key: "upper", test: (pw) => /[A-Z]/.test(pw) },
  { key: "lower", test: (pw) => /[a-z]/.test(pw) },
  { key: "digit", test: (pw) => /\d/.test(pw) },
  { key: "special", test: (pw) => /[^A-Za-z0-9]/.test(pw) },
];

export function Step2AdminPassword({ status, onNext, onPasswordChanged }: Props) {
  const { t } = useTranslation();
  // 只有後端說已經不是臨時密碼時才能保留；狀態還沒讀到時不給略過。
  const passwordAlreadySet = status?.is_admin_temp_password === false;
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (newPassword !== confirm) { setError(t("setup.step2.errors.mismatch")); return; }
    const pwKey = validatePassword(newPassword);
    if (pwKey) { setError(t(`setup.step2.errors.${pwKey}`)); return; }

    setSaving(true);
    setError(null);
    try {
      const meRes = await fetchTouka(getActiveProxyUrl(), "/v1/auth/me");
      if (!meRes.ok) { setError(t("setup.step2.errors.profile_fetch_failed")); return; }
      const me = await meRes.json();

      const res = await fetchTouka(getActiveProxyUrl(), `/v1/admin/users/${me.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: newPassword }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        setError(d.detail ?? t("setup.step2.errors.update_failed"));
        return;
      }
      onPasswordChanged(me.id);
    } catch (err) {
      logSSOTError(err, "Step2AdminPassword");
      setError(t("setup.step2.errors.generic"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <p className="text-sm text-muted-foreground">
        {t(passwordAlreadySet ? "setup.step2.description_already_set" : "setup.step2.description")}
      </p>
      <div>
        <label htmlFor="new-password" className="block text-sm font-medium mb-1">{t("setup.step2.new_password")}</label>
        <input
          id="new-password"
          type="password"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          aria-describedby={error ? "step2-error" : undefined}
          className="w-full border rounded px-3 py-2 text-sm bg-muted"
          minLength={8}
          required
        />
      </div>
      <div className="rounded-md bg-muted/40 p-3 space-y-1">
        <p className="text-xs font-medium text-muted-foreground">{t("setup.step2.requirements")}</p>
        <ul className="space-y-0.5">
          {PASSWORD_RULES.map((r) => {
            const ok = r.test(newPassword);
            return (
              <li key={r.key} className={`text-xs flex items-center gap-1.5 ${ok ? "text-status-success" : "text-muted-foreground"}`}>
                <span>{ok ? "✓" : "○"}</span>
                {t(`setup.step2.rules.${r.key}`)}
              </li>
            );
          })}
        </ul>
      </div>
      <div>
        <label htmlFor="confirm-password" className="block text-sm font-medium mb-1">{t("setup.step2.confirm_password")}</label>
        <input
          id="confirm-password"
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          className="w-full border rounded px-3 py-2 text-sm bg-muted"
          minLength={8}
          required
        />
      </div>
      {error && <p id="step2-error" className="text-destructive text-sm">{error}</p>}
      <button
        type="submit"
        disabled={saving}
        className="px-4 py-2 bg-primary-button text-primary-foreground rounded hover:opacity-90 disabled:opacity-50"
      >
        {saving ? (
          <span className="flex items-center justify-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />{t("setup.common.saving")}</span>
        ) : t("setup.step2.submit")}
      </button>
      {passwordAlreadySet && (
        <button
          type="button"
          onClick={onNext}
          disabled={saving}
          className="ml-2 px-4 py-2 text-sm text-muted-foreground hover:text-foreground disabled:opacity-50"
        >
          {t("setup.step2.keep_current")}
        </button>
      )}
    </form>
  );
}
