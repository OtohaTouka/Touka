import { useCallback, useMemo, useState } from 'react';

interface QueuedRequest<T> {
  sessionId: string;
  request: T;
}

/**
 * 串流途中要人回答的請求（提問、技能安裝）排成一列：對外是目前那一筆，答完或關掉跳下一筆。
 * 兩段對話同時問時，第二個不會蓋掉第一個的彈窗。
 */
export function usePendingRequestQueue<T extends { id: string }>() {
  const [queue, setQueue] = useState<QueuedRequest<T>[]>([]);

  const enqueue = useCallback((sessionId: string, request: T) => {
    setQueue(current => current.some(item => item.request.id === request.id)
      ? current : [...current, { sessionId, request }]);
  }, []);

  const dismissCurrent = useCallback(() => setQueue(current => current.slice(1)), []);

  /** 那段對話的串流結束了：等著的問題已經沒有人要答案，不能之後才跳出來。 */
  const dropSession = useCallback((sessionId: string) => {
    setQueue(current => {
      const kept = current.filter(item => item.sessionId !== sessionId);
      return kept.length === current.length ? current : kept;
    });
  }, []);

  const sessionIds = useMemo(() => queue.map(item => item.sessionId), [queue]);

  return { current: queue[0]?.request ?? null, sessionIds, enqueue, dismissCurrent, dropSession };
}
