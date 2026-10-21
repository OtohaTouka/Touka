"use client";

import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useSessionList } from '@/lib/providers/SessionListProvider';
import { Button } from '@/components/ui/button';

export function SessionPersistenceNotice({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation();
  const { getPersistence, getSession, recheckSession, retrySaveSession, canRetrySave } = useSessionList();
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const state = getPersistence(sessionId);
  if (!state || getSession(sessionId)?.incognito || state.status === 'saved' || state.status === 'skipped') return null;
  const recover = async (retry: boolean) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try { await (retry ? retrySaveSession(sessionId) : recheckSession(sessionId)); }
    finally { busyRef.current = false; setBusy(false); }
  };
  return <div role="status" className="m-2 flex flex-wrap items-center gap-2 rounded border p-3 text-sm">
    <span>{t(`chat.persistence.${state.status}`)}</span>
    {state.status !== 'pending' && <Button variant="outline" size="sm" disabled={busy}
      onClick={() => { void recover(false); }}>{t('chat.persistence.recheck')}</Button>}
    {state.status === 'failed' && canRetrySave(sessionId) && <Button variant="outline" size="sm" disabled={busy}
      onClick={() => { void recover(true); }}>{t('chat.persistence.retry')}</Button>}
  </div>;
}
