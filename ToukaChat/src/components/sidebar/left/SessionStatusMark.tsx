"use client";

import { CircleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useReducedMotionPreference } from '@/hooks/useReducedMotionPreference';
import type { SessionWorkStatus } from '@/lib/session_activity';
import { cn } from '@/lib/utils';

/** 工作中：桌面版是對話名稱呼吸（手機沒有滑鼠、名稱又常被截斷，改由名稱前的小點呼吸）。 */
const isWorkingStatus = (status: SessionWorkStatus | null | undefined) =>
  status?.kind === 'replying' || status?.kind === 'heartbeat';

/** 狀態的文字：寫進列的無障礙名稱；減少動態時也顯示在列上。 */
export function useStatusLabel(status: SessionWorkStatus | null | undefined): string | null {
  const { t } = useTranslation();
  switch (status?.kind) {
    case 'awaiting': return t('sidebar.session_activity.awaiting');
    case 'replying': return t('sidebar.session_activity.replying');
    case 'heartbeat': return t('sidebar.session_activity.heartbeat', { current: status.current, total: status.total });
    case 'unseen': return t('sidebar.session_activity.unseen');
    default: return null;
  }
}

/** 名稱這一段要不要呼吸（減少動態時不動）。 */
export function useBreathingClass(status: SessionWorkStatus | null | undefined): string | undefined {
  const reduced = useReducedMotionPreference();
  return isWorkingStatus(status) && !reduced ? 'md:animate-breathe' : undefined;
}

/** 名稱前面的記號：手機的呼吸小點、不動的「等您處理」。 */
export function SessionStatusLead({ status }: { status: SessionWorkStatus | null | undefined }) {
  const reduced = useReducedMotionPreference();
  if (status?.kind === 'awaiting') {
    return <CircleAlert data-testid="session-status-awaiting" aria-hidden="true" className="w-3.5 h-3.5 shrink-0 text-status-warning" />;
  }
  if (!isWorkingStatus(status) || reduced) return null;
  return <span data-testid="session-status-working-dot" aria-hidden="true" className="md:hidden h-2 w-2 shrink-0 rounded-full bg-muted-foreground animate-breathe" />;
}

/** 名稱後面的記號：心跳進度、減少動態時的文字、您沒在看時完成的小點。 */
export function SessionStatusTail({ status }: { status: SessionWorkStatus | null | undefined }) {
  const reduced = useReducedMotionPreference();
  const label = useStatusLabel(status);
  if (status?.kind === 'unseen') {
    return <span data-testid="session-status-unseen" aria-hidden="true" className="ml-auto h-2 w-2 shrink-0 rounded-full bg-muted-foreground" />;
  }
  const showText = status?.kind === 'heartbeat' || (reduced && isWorkingStatus(status));
  if (!showText) return null;
  return <span data-testid="session-status-text" aria-hidden="true" className="shrink-0 text-xs text-muted-foreground whitespace-nowrap">{label}</span>;
}

/** 側欄收起來時，開關按鈕上的提示點。和首次設定的紅點分開放（那個在右上角）。按鈕要 `relative`。 */
export function SessionAttentionDot({ status }: { status: SessionWorkStatus | null }) {
  const reduced = useReducedMotionPreference();
  if (!status) return null;
  return (
    <span
      data-testid="session-attention-dot"
      data-kind={status.kind}
      aria-hidden="true"
      className={cn(
        "absolute top-1 left-1 h-2 w-2 rounded-full",
        status.kind === 'awaiting' ? "bg-status-warning" : "bg-muted-foreground",
        isWorkingStatus(status) && !reduced && "animate-breathe",
      )}
    />
  );
}
