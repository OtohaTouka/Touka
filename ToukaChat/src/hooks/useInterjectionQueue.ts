/**
 * ADR-0249 第 4a 段：插話資料層——經 fetchTouka 打 U4a-1 的端點（一律帶 stream_id）。
 *
 * 還沒拿到 stream_id、API 被拒或連不上時，文字留在前端（local），回合結束後當成下一則送出，
 * 不會遺失；被拒時另外顯示錯誤。
 */
import { useCallback } from 'react';
import { toast } from 'sonner';
import { fetchTouka, isRequestScopeCurrent } from '@/lib/connections';
import type { RequestScope } from '@/lib/connections';
import { errorMsg, getErrorMessage, logSSOTError } from '@/lib/error_handler';
import { getSystemEpoch } from '@/lib/time_utils';
import { useInterjectionStore, type QueuedInterjection } from '@/lib/chat/interjectionStore';
import type { Interjection } from '@/lib/types';

export interface InterjectionTarget {
  sessionId: string;
  streamId?: string;
  requestScope: RequestScope | null;
}

const EMPTY: QueuedInterjection[] = [];

const localItem = (text: string, urgent: boolean): QueuedInterjection => ({
  id: crypto.randomUUID(), text, urgent, status: 'undelivered', step: null,
  created_at: getSystemEpoch() / 1000, local: true,
});

export function useInterjectionQueue({ sessionId, streamId, requestScope }: InterjectionTarget) {
  const items = useInterjectionStore(s => s.bySession[sessionId]) ?? EMPTY;
  const base = streamId && requestScope
    ? `/v1/ui/sessions/${encodeURIComponent(sessionId)}/interjections` : null;
  const query = streamId ? `?stream_id=${encodeURIComponent(streamId)}` : '';
  const usable = !!base && isRequestScopeCurrent(requestScope);

  const enqueue = useCallback(async (text: string, urgent: boolean): Promise<QueuedInterjection> => {
    const { add } = useInterjectionStore.getState();
    if (usable && base && requestScope) {
      try {
        const res = await fetchTouka(requestScope.proxyOrigin, base + query, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, urgent }), requestScope,
        });
        if (res.ok) {
          const accepted = (await res.json()) as Interjection;
          add(sessionId, accepted);
          return accepted;
        }
        toast.error(await getErrorMessage(res));
      } catch (e) {
        logSSOTError(e, 'useInterjectionQueue:enqueue');
      }
    }
    const item = localItem(text, urgent);
    add(sessionId, item);
    return item;
  }, [usable, base, query, requestScope, sessionId]);

  const retract = useCallback(async (item: QueuedInterjection): Promise<boolean> => {
    const { remove } = useInterjectionStore.getState();
    if (item.local || item.status === 'undelivered') { remove(sessionId, item.id); return true; }
    if (!usable || !base || !requestScope) return false;
    try {
      const res = await fetchTouka(requestScope.proxyOrigin,
        `${base}/${encodeURIComponent(item.id)}${query}`, { method: 'DELETE', requestScope });
      if (res.ok) { remove(sessionId, item.id); return true; }
      toast.error(await getErrorMessage(res));
    } catch (e) {
      logSSOTError(e, 'useInterjectionQueue:retract');
      toast.error(errorMsg('NETWORK_CONNECTION_ERROR'));
    }
    return false;
  }, [usable, base, query, requestScope, sessionId]);

  return { items, enqueue, retract };
}
