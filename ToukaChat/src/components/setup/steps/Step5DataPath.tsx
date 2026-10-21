'use client';

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { SetupStatus } from '@/hooks/useSetupStatus';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';

interface Props {
  status: SetupStatus | null;
  onNext: () => void;
}

export function Step5DataPath({ status, onNext }: Props) {
  const { t } = useTranslation();
  const [dataPath, setDataPath] = useState("");
  const [workspaceRoot, setWorkspaceRoot] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hasInput = !!(dataPath.trim() || workspaceRoot.trim());

  // 兩者皆為「寫入 .env、供 docker compose 掛載」的宿主路徑：只送有填的欄位；
  // 皆空＝略過直接前進；apply 失敗（res.ok=false）停在本步顯示錯誤（root-cause-C 同精神）。
  async function handleSave() {
    const body: Record<string, string> = {};
    if (dataPath.trim()) body.data_path = dataPath.trim();
    if (workspaceRoot.trim()) body.workspace_root = workspaceRoot.trim();
    if (Object.keys(body).length === 0) { onNext(); return; }
    setSaving(true);
    setError(null);
    try {
      const res = await fetchTouka(getActiveProxyUrl(), "/v1/admin/setup/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        // ToukaException 的 detail 為物件 {message,…}；FastAPI 預設為字串——兩者皆容忍，
        // 避免 setError(物件) 導致 React 崩潰。
        const detail = d?.detail;
        const msg = (typeof detail === "string" ? detail : detail?.message) ?? t("setup.step5.errors.save_failed");
        setError(msg);
        return;
      }
      onNext();
    } catch (err) {
      logSSOTError(err, "Step5DataPath");
      setError(t("setup.step5.errors.generic"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        {t("setup.step5.description")}
        {status?.data_path_is_default && (
          <span className="block mt-1 text-accent-foreground">
            {t("setup.step5.default_warning")}
          </span>
        )}
      </p>

      <div>
        <label htmlFor="data-path" className="block text-sm font-medium mb-1">{t("setup.step5.path_label")}</label>
        <input
          id="data-path"
          type="text"
          value={dataPath}
          onChange={(e) => setDataPath(e.target.value)}
          placeholder={t("setup.step5.path_placeholder")}
          aria-describedby={error ? "data-path-error" : undefined}
          className="w-full border rounded px-3 py-2 text-sm bg-muted"
        />
      </div>

      <div>
        <label htmlFor="workspace-root" className="block text-sm font-medium mb-1">{t("setup.step5.workspace_label")}</label>
        <input
          id="workspace-root"
          type="text"
          value={workspaceRoot}
          onChange={(e) => setWorkspaceRoot(e.target.value)}
          placeholder={t("setup.step5.workspace_placeholder")}
          aria-describedby={error ? "data-path-error" : undefined}
          className="w-full border rounded px-3 py-2 text-sm bg-muted"
        />
        <p className="text-xs text-muted-foreground mt-1">{t("setup.step5.workspace_hint")}</p>
      </div>

      <p className="text-xs text-muted-foreground italic border-t border-border/50 pt-2">
        {t("setup.step5.restart_note")}
      </p>

      {error && <p id="data-path-error" className="text-destructive text-sm">{error}</p>}

      <div className="flex gap-3">
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-4 py-2 bg-primary-button text-primary-foreground rounded hover:opacity-90 disabled:opacity-50"
        >
          {saving ? (
            <span className="flex items-center justify-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />{t("setup.common.saving")}</span>
          ) : hasInput ? t("setup.step5.save_continue") : t("setup.step5.skip")}
        </button>
      </div>
    </div>
  );
}
