'use client';

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';
import { SetupStatus } from '@/hooks/useSetupStatus';

interface Props {
  status: SetupStatus | null;
  onNext: () => void;
}

function generateToken(bytes = 32): string {
  const arr = new Uint8Array(bytes);
  crypto.getRandomValues(arr);
  return Array.from(arr, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function Step4Secrets({ status, onNext }: Props) {
  const { t } = useTranslation();
  const [rotating, setRotating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rotated, setRotated] = useState(false);

  const dispatchOk = status ? !status.dispatch_token_is_default : false;
  const authOk = status ? status.auth_secret_persisted : false;

  async function handleRotate() {
    setRotating(true);
    setError(null);
    setRotated(false);
    try {
      const res = await fetchTouka(getActiveProxyUrl(), "/v1/admin/setup/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          dispatch_token: generateToken(32),
          auth_secret: generateToken(32),
        }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        const detail = typeof d.detail === "string" ? d.detail : d.detail?.message;
        setError(detail ?? t("setup.step4.errors.save_failed"));
        return;
      }
      setRotated(true);
    } catch (err) {
      logSSOTError(err, "Step4Secrets");
      setError(t("setup.step4.errors.generic"));
    } finally {
      setRotating(false);
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">{t("setup.step4.auto_managed")}</p>

      <ul className="space-y-2 text-sm">
        <li>
          {dispatchOk ? "✅" : "⚠️"} <strong>{t("setup.step4.dispatch_token")}</strong>:{" "}
          {t(dispatchOk ? "setup.step4.status_ok" : "setup.step4.status_pending")}
        </li>
        <li>
          {authOk ? "✅" : "⚠️"} <strong>{t("setup.step4.auth_secret")}</strong>:{" "}
          {t(authOk ? "setup.step4.status_ok" : "setup.step4.status_pending")}
        </li>
      </ul>

      <details className="text-sm">
        <summary className="cursor-pointer text-muted-foreground">
          {t("setup.step4.rotate_toggle")}
        </summary>
        <div className="mt-2 space-y-2">
          <p className="text-xs text-muted-foreground">{t("setup.step4.rotate_desc")}</p>
          <button
            onClick={handleRotate}
            disabled={rotating}
            className="px-3 py-1.5 text-xs border rounded hover:bg-accent disabled:opacity-50"
          >
            {rotating ? (
              <span className="flex items-center gap-2">
                <Loader2 className="w-3 h-3 animate-spin" />
                {t("setup.common.saving")}
              </span>
            ) : (
              t("setup.step4.rotate_button")
            )}
          </button>
          {rotated && (
            <p className="text-xs text-status-success">{t("setup.step4.rotate_success")}</p>
          )}
          {error && <p className="text-xs text-destructive">{error}</p>}
        </div>
      </details>

      <button
        onClick={onNext}
        className="px-4 py-2 bg-primary-button text-primary-foreground rounded hover:opacity-90"
      >
        {t("setup.step4.next")}
      </button>
    </div>
  );
}
