"use client";

import { useEffect, useState } from 'react';
import { useTranslation } from "react-i18next";
import { ThumbsUp, ThumbsDown } from 'lucide-react';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';
import { formatToTaipeiTime } from '@/lib/time_utils';

interface FeedbackRow {
  id: string;
  session_id: string;
  message_id: string;
  agent_id: string;
  feedback_type: string;
  reason?: string | null;
  created_at: number;
  updated_at: number;
}

/**
 * HAX G15 — 擁有者檢視頁：唯讀列出最近的回饋（讚/倒讚＋原因＋時間）。
 * 僅 Owner/admin 可讀（後端 owner-gate）；非授權者優雅降為空狀態。
 */
export function FeedbackLog() {
  const { t } = useTranslation();
  const [items, setItems] = useState<FeedbackRow[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetchTouka(getActiveProxyUrl(), '/v1/feedback/recent');
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (!cancelled) setItems(Array.isArray(data) ? data : []);
      } catch (e) {
        logSSOTError(e, 'FeedbackLog:load');
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (loaded && items.length === 0) {
    return <p className="px-1 py-2 text-xs italic text-muted-foreground">{t('sidebar.feedback_log.empty_state')}</p>;
  }

  return (
    <ul className="space-y-2">
      {items.map((it) => (
        <li key={it.id} className="rounded-lg border border-border bg-muted/20 p-2.5 text-xs">
          <div className="flex items-center gap-2">
            {it.feedback_type === 'up' ? (
              <ThumbsUp size={12} className="shrink-0 text-primary" aria-label={t('sidebar.feedback_log.thumbs_up')} />
            ) : (
              <ThumbsDown size={12} className="shrink-0 text-status-error" aria-label={t('sidebar.feedback_log.thumbs_down')} />
            )}
            <span suppressHydrationWarning className="truncate font-mono text-muted-foreground">
              {formatToTaipeiTime(it.updated_at)}
            </span>
          </div>
          {it.reason && (
            <p className="mt-1 leading-relaxed text-foreground break-words">{it.reason}</p>
          )}
        </li>
      ))}
    </ul>
  );
}
