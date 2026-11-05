import { useEffect, useRef } from 'react';
import { captureRequestScope, fetchTouka, isRequestScopeCurrent, subscribeCredentialScope } from '@/lib/connections';
import { ACTIVITY_POLL_MS, parseSessionActivity } from '@/lib/session_activity';
import type { SessionActivity } from '@/lib/types';

interface ActivityPollHandlers {
  /** `sequence`：這次查詢是第幾個發出的。 */
  onResult: (activity: SessionActivity, sequence: number) => void;
  onFailure: () => void;
}

/**
 * 每 8 秒查一次工作狀態：只在分頁顯示中查，回到分頁馬上查一次，同時只跑一個請求。
 * 回傳的 ref 是最新發出的序號；要判斷「這個結果是不是某件事之後才查的」用它比。
 * 憑證換了之後，舊回應丟掉，游標也重來。
 */
export function useActivityPoll(enabled: boolean, handlers: ActivityPollHandlers) {
  const sequence = useRef(0);
  const latestHandlers = useRef(handlers);
  useEffect(() => { latestHandlers.current = handlers; });

  useEffect(() => {
    if (!enabled) return;
    let cursor: number | null = null;
    let controller: AbortController | null = null;

    const poll = async () => {
      if (controller || document.visibilityState !== 'visible') return;
      const scope = captureRequestScope();
      if (!scope) return;
      const current = new AbortController();
      controller = current;
      const issued = ++sequence.current;
      const query = cursor === null ? '' : `?since=${cursor}`;
      try {
        const response = await fetchTouka(scope.proxyOrigin, `/v1/ui/activity${query}`, {
          cache: 'no-store', signal: current.signal, nonStreaming: true, requestScope: scope,
        });
        const activity = response.ok ? parseSessionActivity(await response.json()) : null;
        if (current.signal.aborted || !isRequestScopeCurrent(scope)) return;
        if (!activity) {
          latestHandlers.current.onFailure();
          return;
        }
        cursor = activity.cursor;
        latestHandlers.current.onResult(activity, issued);
      } catch {
        if (!current.signal.aborted && isRequestScopeCurrent(scope)) latestHandlers.current.onFailure();
      } finally {
        if (controller === current) controller = null;
      }
    };

    const onVisibilityChange = () => { void poll(); };
    const first = setTimeout(() => { void poll(); }, 0);
    const interval = setInterval(() => { void poll(); }, ACTIVITY_POLL_MS);
    document.addEventListener('visibilitychange', onVisibilityChange);
    const unsubscribe = subscribeCredentialScope(() => {
      controller?.abort();
      controller = null;
      cursor = null;
      void poll();
    });
    return () => {
      clearTimeout(first);
      clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      unsubscribe();
      controller?.abort();
    };
  }, [enabled]);

  return sequence;
}
