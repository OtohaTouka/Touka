"use client";

import { useTranslation } from 'react-i18next';
import { useHooksActive } from '@/hooks/useHooksActive';
import { useSessionList } from '@/lib/providers/SessionListProvider';

/** 無痕對話的內容也會交給管理員設定的 hook：有這種 hook 時要讓使用者知道（ADR-0292）。 */
export function IncognitoHookNotice({ sessionId, recheckKey }: { sessionId: string; recheckKey: unknown }) {
  const { t } = useTranslation();
  const { getSession } = useSessionList();
  const incognito = Boolean(getSession(sessionId)?.incognito);
  const active = useHooksActive(incognito, recheckKey);
  if (!active) return null;
  return <div role="status" className="m-2 rounded border p-3 text-sm">{t('chat.incognito_hook_notice')}</div>;
}
