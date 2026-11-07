"use client";

import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { useChat } from '@/lib/providers/ChatProvider';
import { useSessionActivity } from '@/lib/providers/SessionActivityProvider';
import { useSessionList } from '@/lib/providers/SessionListProvider';

/**
 * 開著的對話被心跳或別處寫入、畫面內容是舊的（ADR-0300）。不點就送出還是存不進去。
 * 存檔已經失敗時不顯示：那時重新讀取會保留本地內容，點了沒用，留給存檔失敗的畫面處理。
 */
export function SessionUpdatedNotice({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation();
  const { staleSessionId, clearStale } = useSessionActivity();
  const { getPersistence } = useSessionList();
  const { handleSwitchSession } = useChat();
  const persistence = getPersistence(sessionId)?.status;
  if (staleSessionId !== sessionId || persistence === 'failed' || persistence === 'unknown') return null;
  return (
    <div role="status" className="m-2 flex flex-wrap items-center gap-2 rounded border p-3 text-sm">
      <span>{t('chat.session_updated.message')}</span>
      <Button variant="outline" size="sm" onClick={() => { clearStale(); handleSwitchSession(sessionId); }}>
        {t('chat.session_updated.refresh')}
      </Button>
    </div>
  );
}
