'use client';

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { SetupStatus } from '@/hooks/useSetupStatus';
import { PROVIDER_KEYS } from '@/lib/providerKeys';
import { ProviderKeyInputs } from '@/components/common/ProviderKeyInputs';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';

interface Props {
  status: SetupStatus | null;
  /** 儲存成功或無輸入（略過）後前進；apply 失敗時不呼叫（停在本步顯示錯誤）。 */
  onNext: () => void;
}

export function Step3ApiKeys({ status, onNext }: Props) {
  const { t } = useTranslation();
  const configured = new Set(status?.api_keys_configured ?? []);
  // SetupStatus.api_keys_configured 以 provider id 表示；轉成 env→已設定 供共用元件使用。
  const statuses: Record<string, boolean> = {};
  for (const p of PROVIDER_KEYS) statuses[p.env] = configured.has(p.id);

  // 新值輸入（write-only；既有金鑰永不回填）。
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hasInput = Object.values(inputs).some(v => v && v.trim());

  const handleChange = (env: string, val: string) =>
    setInputs(prev => ({ ...prev, [env]: val }));

  // 自帶 apply（對齊 Step2/Step5 SSOT 模式）：無輸入＝略過直接前進；有輸入則只送非空鍵，
  // res.ok=false 或網路錯誤時停在本步並顯示錯誤，嚴禁默默 onNext 吞掉失敗（root-cause-C）。
  async function handleContinue() {
    const settings: Record<string, string> = {};
    for (const [env, val] of Object.entries(inputs)) {
      if (val && val.trim()) settings[env] = val.trim();
    }
    if (Object.keys(settings).length === 0) { onNext(); return; }
    setSaving(true);
    setError(null);
    try {
      const res = await fetchTouka(getActiveProxyUrl(), "/v1/config/env", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        setError(d.detail ?? t("setup.step3.errors.save_failed"));
        return;
      }
      onNext();
    } catch (err) {
      logSSOTError(err, "Step3ApiKeys");
      setError(t("setup.step3.errors.generic"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        {t('setup.step3.description')}
      </p>

      <ProviderKeyInputs statuses={statuses} values={inputs} onChange={handleChange} />

      {error && <p id="step3-error" className="text-destructive text-sm">{error}</p>}

      <button
        onClick={handleContinue}
        disabled={saving}
        className="px-4 py-2 bg-primary-button text-primary-foreground rounded hover:opacity-90 disabled:opacity-50"
      >
        {saving ? (
          <span className="flex items-center justify-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />{t('setup.common.saving')}</span>
        ) : hasInput
          ? t('setup.step3.save_continue')
          : configured.size > 0
            ? t('setup.step3.continue')
            : t('setup.step3.skip')}
      </button>
    </div>
  );
}
