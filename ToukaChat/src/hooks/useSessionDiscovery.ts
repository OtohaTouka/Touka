import { useEffect, useMemo, useRef } from 'react';
import type { DiscoverResult } from '@/lib/providers/SessionListProvider';
import { MAX_DISCOVERIES_PER_POLL } from '@/lib/session_activity';

interface UseSessionDiscoveryOptions {
  discover: (id: string) => Promise<DiscoverResult>;
  /** 再也不會出現在清單上（不存在且沒在工作、永久拒絕）。 */
  onDropped: (id: string) => void;
}

/**
 * 查詢帶回清單上沒有的對話時排進佇列，分批下載單筆。佇列獨立於查詢的游標：
 * 暫時失敗留著下次再試，游標照常前進，紀錄不會因此掉。
 */
export function useSessionDiscovery(options: UseSessionDiscoveryOptions) {
  const queue = useRef(new Set<string>());
  const denied = useRef(new Set<string>());
  const running = useRef(false);
  // 清單一變，discover 就是新的函式；佇列不該因此重跑
  const latest = useRef(options);
  useEffect(() => { latest.current = options; });

  return useMemo(() => ({
    enqueue: (id: string) => {
      if (!denied.current.has(id)) queue.current.add(id);
    },
    isQueued: (id: string) => queue.current.has(id),
    /** `working`：現在還在工作的對話。它們的存檔可能還沒寫出來，查不到不算不存在。 */
    flush: async (working: ReadonlySet<string>) => {
      if (running.current) return;
      running.current = true;
      try {
        for (const id of [...queue.current].slice(0, MAX_DISCOVERIES_PER_POLL)) {
          const result = await latest.current.discover(id);
          if (result === 'retry' || (result === 'absent' && working.has(id))) continue;
          queue.current.delete(id);
          if (result === 'denied') denied.current.add(id);
          if (result === 'denied' || result === 'absent') latest.current.onDropped(id);
        }
      } finally {
        running.current = false;
      }
    },
  }), []);
}
