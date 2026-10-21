"use client";

import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError, errorMsg } from '@/lib/error_handler';

export type FeedbackType = 'up' | 'down' | 'none';

export interface FeedbackEntry {
  type: string;
  reason?: string | null;
}

/**
 * HAX G15（細粒度回饋）— 管理某 session 內每則訊息的讚/倒讚狀態。
 *
 * 切換 session 立即清空舊狀態並拉回後端權威對映（還原按鈕）；submit 採樂觀更新，
 * 失敗回滾＋toast。載入的權威資料只填補空缺、不覆蓋使用者剛做的樂觀變更（避免競態）。
 */
export function useFeedback(sessionId: string | null) {
  const [feedbackState, setFeedbackState] = useState<Record<string, FeedbackEntry>>({});

  useEffect(() => {
    setFeedbackState({}); // 切換 session 即清空舊狀態，杜絕跨 session 洩漏
    if (!sessionId) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetchTouka(
          getActiveProxyUrl(),
          `/v1/feedback/session/${encodeURIComponent(sessionId)}`,
        );
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (cancelled) return;
        // 權威資料填補空缺，但保留使用者剛做的樂觀變更（cur 優先）
        setFeedbackState((cur) => ({ ...(data.feedbacks || {}), ...cur }));
      } catch (e) {
        logSSOTError(e, 'useFeedback:load');
      }
    })();
    return () => { cancelled = true; };
  }, [sessionId]);

  const submit = useCallback(
    async (messageId: string, type: FeedbackType, reason?: string) => {
      if (!sessionId) return;
      let prev: FeedbackEntry | undefined;
      setFeedbackState((s) => {
        prev = s[messageId];
        const next = { ...s };
        if (type === 'none') delete next[messageId];
        else next[messageId] = { type, reason: reason ?? null };
        return next;
      });
      try {
        const res = await fetchTouka(getActiveProxyUrl(), '/v1/feedback/submit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: sessionId,
            message_id: messageId,
            feedback_type: type,
            reason: reason ?? null,
          }),
        });
        if (!res.ok) throw new Error(`feedback submit failed: ${res.status}`);
      } catch (e) {
        logSSOTError(e, 'useFeedback:submit');
        setFeedbackState((s) => {
          const next = { ...s };
          if (prev) next[messageId] = prev;
          else delete next[messageId];
          return next;
        });
        toast.error(errorMsg("SYSTEM_ERROR"));
      }
    },
    [sessionId],
  );

  return { feedbackState, submit };
}
