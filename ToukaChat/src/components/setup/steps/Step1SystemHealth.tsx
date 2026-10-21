'use client';

import { useState, useRef, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, AlertTriangle } from 'lucide-react';
import { SetupStatus } from '@/hooks/useSetupStatus';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';

interface Props {
  status: SetupStatus | null;
  /** 前進到下一步（僅在模型未變更、或下載完成且成功切換後呼叫）。 */
  onNext: () => void;
}

const MODEL_OPTIONS = [
  { value: "all-MiniLM-L6-v2", i18nKey: "setup.step1.model_english" },
  { value: "paraphrase-multilingual-MiniLM-L12-v2", i18nKey: "setup.step1.model_multilingual" },
];

type Phase = 'idle' | 'downloading' | 'switching' | 'confirm';

export function Step1SystemHealth({ status, onNext }: Props) {
  const { t } = useTranslation();
  const currentModel = status?.embedding_model ?? "all-MiniLM-L6-v2";
  const [selectedModel, setSelectedModel] = useState(currentModel);
  const [phase, setPhase] = useState<Phase>('idle');
  const [pct, setPct] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [confirmInfo, setConfirmInfo] = useState<{ current: string; target: string } | null>(null);
  const cancelledRef = useRef(false);

  // 元件卸載後停止輪詢，避免對已卸載元件 setState。
  useEffect(() => () => { cancelledRef.current = true; }, []);

  const busy = phase === 'downloading' || phase === 'switching';

  // ToukaException 的 detail 為物件 {message,…}；FastAPI 預設為字串——兩者皆容忍。
  function extractError(d: unknown, fallbackKey: string): string {
    const detail = (d as { detail?: unknown })?.detail;
    if (typeof detail === "string") return detail;
    const msg = (detail as { message?: string } | undefined)?.message;
    return msg ?? t(fallbackKey);
  }

  // 輪詢下載進度直到 done/error；回傳是否成功完成。
  async function pollDownload(model: string): Promise<boolean> {
    const path = `/v1/admin/setup/embedding/download-status?model=${encodeURIComponent(model)}`;
    for (;;) {
      if (cancelledRef.current) return false;
      const res = await fetchTouka(getActiveProxyUrl(), path);
      if (!res.ok) { setError(t("setup.step1.errors.save_failed")); return false; }
      const st = await res.json();
      if (typeof st.pct === "number") setPct(st.pct);
      if (st.status === "done") return true;
      if (st.status === "error") { setError(st.error ?? t("setup.step1.errors.generic")); return false; }
      await new Promise((r) => setTimeout(r, 700));
    }
  }

  // 呼叫切換端點；confirmation_required 時進入確認階段（不前進）。
  async function doSwitch(model: string, force: boolean): Promise<void> {
    setPhase('switching');
    const res = await fetchTouka(getActiveProxyUrl(), "/v1/admin/setup/embedding/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, force }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      setError(extractError(d, "setup.step1.errors.save_failed"));
      setPhase('idle');
      return;
    }
    const body = await res.json();
    if (body.status === "confirmation_required") {
      setConfirmInfo({ current: body.current_model, target: body.target_model });
      setPhase('confirm');
      return;
    }
    onNext();
  }

  // 未變更＝直接前進；變更＝先下載（進度）→ 再切換（可能需確認）。
  async function handleContinue() {
    setError(null);
    if (selectedModel === currentModel) { onNext(); return; }
    try {
      setPhase('downloading');
      setPct(0);
      const dl = await fetchTouka(getActiveProxyUrl(), "/v1/admin/setup/embedding/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: selectedModel }),
      });
      if (!dl.ok) {
        const d = await dl.json().catch(() => ({}));
        setError(extractError(d, "setup.step1.errors.save_failed"));
        setPhase('idle');
        return;
      }
      const ok = await pollDownload(selectedModel);
      if (!ok) { setPhase('idle'); return; }
      await doSwitch(selectedModel, false);
    } catch (err) {
      logSSOTError(err, "Step1SystemHealth");
      setError(t("setup.step1.errors.generic"));
      setPhase('idle');
    }
  }

  async function handleConfirmSwitch() {
    setError(null);
    try {
      await doSwitch(selectedModel, true);
    } catch (err) {
      logSSOTError(err, "Step1SystemHealth");
      setError(t("setup.step1.errors.generic"));
      setPhase('idle');
    }
  }

  function handleCancelSwitch() {
    setConfirmInfo(null);
    setPhase('idle');
  }

  return (
    <div className="space-y-6">
      <div>
        <h3 className="font-semibold text-lg">{t('setup.step1.system_status')}</h3>
        <ul className="mt-2 space-y-1 text-sm">
          <li>
            {status?.api_keys_configured.length ? "✅" : "❌"} {t('setup.step1.api_keys_label')}:{" "}
            {status?.api_keys_configured.join(", ") || t('setup.step1.none_configured')}
          </li>
          <li>
            {!status?.dispatch_token_is_default ? "✅" : "⚠️"} {t('setup.step1.dispatch_token_label')}:{" "}
            {status?.dispatch_token_is_default ? t('setup.step1.dispatch_default') : t('setup.step1.dispatch_custom')}
          </li>
          <li>
            {status?.auth_secret_persisted ? "✅" : "⚠️"} {t('setup.step1.auth_secret_label')}:{" "}
            {status?.auth_secret_persisted ? t('setup.step1.auth_persisted') : t('setup.step1.auth_not_persisted')}
          </li>
        </ul>
      </div>

      <div>
        <h3 className="font-semibold text-lg">{t('setup.step1.embedding_model')}</h3>
        <p className="text-sm text-muted-foreground mt-1">
          {t('setup.step1.embedding_hint')}
        </p>
        <div className="mt-3 space-y-2">
          {MODEL_OPTIONS.map((opt) => (
            <label key={opt.value} className="flex items-start gap-2 cursor-pointer">
              <input
                type="radio"
                name="embedding_model"
                value={opt.value}
                checked={selectedModel === opt.value}
                onChange={() => setSelectedModel(opt.value)}
                disabled={busy || phase === 'confirm'}
                className="mt-1"
              />
              <span className="text-sm">{t(opt.i18nKey)}</span>
            </label>
          ))}
        </div>
      </div>

      {phase === 'downloading' && (
        <div>
          <p className="text-sm text-muted-foreground mb-1">{t('setup.step1.downloading', { pct })}</p>
          <div className="w-full bg-muted rounded-full h-1.5" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
            <div className="bg-primary h-1.5 rounded-full transition-all" style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}

      {phase === 'confirm' && confirmInfo && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 space-y-3">
          <p className="text-sm flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0 text-destructive" />
            <span>{t('setup.step1.switch_warning', { current: confirmInfo.current, target: confirmInfo.target })}</span>
          </p>
          <div className="flex gap-3">
            <button
              onClick={handleConfirmSwitch}
              className="px-3 py-1.5 text-sm bg-destructive text-destructive-foreground rounded hover:opacity-90"
            >
              {t('setup.step1.confirm_switch')}
            </button>
            <button
              onClick={handleCancelSwitch}
              className="px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
            >
              {t('setup.step1.cancel_switch')}
            </button>
          </div>
        </div>
      )}

      {error && <p id="step1-error" className="text-destructive text-sm">{error}</p>}

      {phase !== 'confirm' && (
        <button
          onClick={handleContinue}
          disabled={busy}
          className="mt-4 px-4 py-2 bg-primary-button text-primary-foreground rounded hover:opacity-90 disabled:opacity-50"
        >
          {phase === 'downloading' ? (
            <span className="flex items-center justify-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />{t('setup.step1.downloading', { pct })}</span>
          ) : phase === 'switching' ? (
            <span className="flex items-center justify-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />{t('setup.common.saving')}</span>
          ) : selectedModel === currentModel ? t('setup.common.next') : t('setup.step1.download_continue')}
        </button>
      )}
    </div>
  );
}
