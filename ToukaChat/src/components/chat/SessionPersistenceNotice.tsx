"use client";

import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useSessionList } from '@/lib/providers/SessionListProvider';
import { useChat } from '@/lib/providers/ChatProvider';
import { useSessionActivity } from '@/lib/providers/SessionActivityProvider';
import { useConfirmToast } from '@/hooks/useConfirmToast';
import { isSaved } from '@/lib/session_persistence';
import { Button } from '@/components/ui/button';

/**
 * 存檔沒確認時的提示。存檔有別人的新內容（心跳、別的裝置、工具）時不給「僅重試保存」，
 * 改給「改用存檔內容」（ADR-0313）：換上伺服器的版本，模式顯示與「有新內容」提示跟著更新。
 */
export function SessionPersistenceNotice({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation();
  const { getPersistence, getSession, recheckSession, retrySaveSession, canRetrySave, canAdoptStored, adoptStoredSession } = useSessionList();
  const { handleSwitchSession } = useChat();
  const { clearStale } = useSessionActivity();
  const { confirmToast } = useConfirmToast();
  const busyRef = useRef(false);
  // 確認提示不會自己消失：換了對話（這個提示卸載）之後才按確認，不能換掉舊對話、再跳回去
  const mountedRef = useRef(true);
  useEffect(() => { mountedRef.current = true; return () => { mountedRef.current = false; }; }, []);
  const [busy, setBusy] = useState(false);
  const state = getPersistence(sessionId);
  if (!state || getSession(sessionId)?.incognito || state.status === 'saved' || state.status === 'skipped') return null;
  const run = async (action: () => Promise<unknown>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try { await action(); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const adopt = () => confirmToast(t('chat.persistence.adopt_confirm'), () => mountedRef.current && run(async () => {
    if (!isSaved(await adoptStoredSession(sessionId)) || !mountedRef.current) return;
    clearStale();
    handleSwitchSession(sessionId);
  }), t('chat.persistence.adopt'));
  const retryable = state.status === 'failed' && canRetrySave(sessionId);
  return <div role="status" className="m-2 flex flex-wrap items-center gap-2 rounded border p-3 text-sm">
    <span>{t(`chat.persistence.${state.status}`)}</span>
    {state.status !== 'pending' && <Button variant="outline" size="sm" disabled={busy}
      onClick={() => { void run(() => recheckSession(sessionId)); }}>{t('chat.persistence.recheck')}</Button>}
    {retryable && <Button variant="outline" size="sm" disabled={busy}
      onClick={() => { void run(() => retrySaveSession(sessionId)); }}>{t('chat.persistence.retry')}</Button>}
    {state.status === 'failed' && !retryable && canAdoptStored(sessionId) && <Button variant="outline" size="sm" disabled={busy}
      onClick={adopt}>{t('chat.persistence.adopt')}</Button>}
  </div>;
}
