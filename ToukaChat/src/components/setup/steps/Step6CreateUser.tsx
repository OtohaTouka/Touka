'use client';

import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';

interface Props {
  onNext: () => void;
}

export function Step6CreateUser({ onNext }: Props) {
  const { t } = useTranslation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<string[]>([]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const res = await fetchTouka(getActiveProxyUrl(), "/v1/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, role: "normal" }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        setError(d.detail ?? t("setup.step6.errors.create_failed"));
        return;
      }
      setCreated((prev) => [...prev, email]);
      setEmail("");
      setPassword("");
    } catch (err) {
      logSSOTError(err, "Step6CreateUser");
      setError(t("setup.step6.errors.generic"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        {t("setup.step6.description")}
      </p>

      <form onSubmit={handleCreate} className="space-y-3">
        <div>
          <label htmlFor="user-email" className="block text-sm font-medium mb-1">{t("setup.step6.email")}</label>
          <input
            id="user-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full border rounded px-3 py-2 text-sm bg-muted"
            required
          />
        </div>
        <div>
          <label htmlFor="user-password" className="block text-sm font-medium mb-1">{t("setup.step6.password")}</label>
          <input
            id="user-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full border rounded px-3 py-2 text-sm bg-muted"
            aria-describedby={error ? "user-form-error" : undefined}
            minLength={8}
            required
          />
        </div>
        {error && <p id="user-form-error" className="text-destructive text-sm">{error}</p>}
        <button
          type="submit"
          disabled={saving}
          className="px-4 py-2 bg-primary-button text-primary-foreground rounded hover:opacity-90 disabled:opacity-50"
        >
          {saving ? (
            <span className="flex items-center justify-center gap-2"><Loader2 className="w-4 h-4 animate-spin" />{t("setup.step6.creating")}</span>
          ) : t("setup.step6.create")}
        </button>
      </form>

      {created.length > 0 && (
        <div>
          <p className="text-sm font-medium text-primary">{t("setup.step6.created_users")}</p>
          <ul className="text-sm list-disc ml-4">
            {created.map((e) => <li key={e}>{e}</li>)}
          </ul>
        </div>
      )}

      <button onClick={onNext} className="px-4 py-2 bg-primary-button text-primary-foreground rounded hover:opacity-90">
        {created.length > 0 ? t("setup.step6.done") : t("setup.step6.skip")}
      </button>
    </div>
  );
}
