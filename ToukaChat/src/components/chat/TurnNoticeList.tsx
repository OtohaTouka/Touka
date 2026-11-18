// src/components/chat/TurnNoticeList.tsx
"use client";

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight, Info } from 'lucide-react';
import type { TurnNotice } from '@/lib/chat/turnNotices';

interface TurnNoticeListProps {
  notices: TurnNotice[];
}

/**
 * ADR-0322 PR 2b：回覆下方的小提示（工具計時、攔截、中斷、警告），灰字、可收合。
 * 全是工具計時這類例行提示時預設收合；有警告或中斷就預設打開。參數照純文字顯示。
 */
export function TurnNoticeList({ notices }: TurnNoticeListProps) {
  const { t } = useTranslation();
  // 串流中清單會變長：沒按過就照目前內容決定，有警告或中斷就打開；按過就照使用者的
  const [chosen, setChosen] = useState<boolean | null>(null);
  const open = chosen ?? notices.some(n => n.kind !== 'tool_finished');
  if (notices.length === 0) return null;
  return (
    <div data-testid="turn-notice-list" className="my-2 text-xs text-muted-foreground">
      <button
        type="button"
        onClick={() => setChosen(!open)}
        aria-expanded={open}
        className="flex items-center gap-1 font-bold hover:text-foreground transition-colors"
      >
        <Info className="w-3.5 h-3.5 shrink-0" />
        <span>{t('chat.turn_notice.summary', { count: notices.length })}</span>
        {open ? <ChevronDown className="w-3.5 h-3.5 shrink-0" /> : <ChevronRight className="w-3.5 h-3.5 shrink-0" />}
      </button>
      {open && (
        <ul className="flex flex-col gap-0.5 mt-1 border-l-2 border-border/50 pl-3">
          {notices.map((notice, index) => (
            <li key={index} className="whitespace-pre-wrap break-words">
              {t(`chat.turn_notice.${notice.kind}`, notice.params)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
