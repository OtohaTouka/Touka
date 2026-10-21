/**
 * ADR-0249 第 4a 段：前端的插話佇列（每個對話一份，只在記憶體）。
 *
 * 由 useInterjectionQueue（打 API）與 StreamRegistryProvider（收 SSE 事件）共用。
 * `local` 表示沒送進伺服器佇列（還沒拿到 stream_id 或被拒），回合結束後當成下一則送出。
 */
import { create } from 'zustand';
import type { Interjection } from '@/lib/types';
import { getSystemEpoch } from '@/lib/time_utils';
import { parseDeliveredEvent, parseUndeliveredEvent } from './interjections';

export type QueuedInterjection = Interjection & { local?: boolean };

interface InterjectionStoreState {
  bySession: Record<string, QueuedInterjection[]>;
  add: (sessionId: string, item: QueuedInterjection) => void;
  remove: (sessionId: string, id: string) => void;
  /** 取出並清空該對話的佇列（回合結束時用）。 */
  take: (sessionId: string) => QueuedInterjection[];
}

export const useInterjectionStore = create<InterjectionStoreState>((set, get) => ({
  bySession: {},
  add: (sessionId, item) => set(s => ({
    bySession: { ...s.bySession, [sessionId]: [...(s.bySession[sessionId] ?? []), item] },
  })),
  remove: (sessionId, id) => set(s => ({
    bySession: { ...s.bySession, [sessionId]: (s.bySession[sessionId] ?? []).filter(i => i.id !== id) },
  })),
  take: (sessionId) => {
    const items = get().bySession[sessionId] ?? [];
    set(s => ({ bySession: { ...s.bySession, [sessionId]: [] } }));
    return items;
  },
}));

/**
 * 套用一筆插話 SSE 事件。delivered：移出佇列，回傳要寫進助理節點的紀錄；
 * undelivered：標成未送達。其他事件不動、回傳 null。
 */
export function applyInterjectionEvent(sessionId: string, event: Record<string, unknown>): Interjection | null {
  const store = useInterjectionStore.getState();
  const items = store.bySession[sessionId] ?? [];
  const delivered = parseDeliveredEvent(event);
  if (delivered) {
    const known = items.find(i => i.id === delivered.interjectionId);
    store.remove(sessionId, delivered.interjectionId);
    return {
      id: delivered.interjectionId,
      text: delivered.text ?? known?.text ?? '',
      created_at: known?.created_at ?? getSystemEpoch() / 1000,
      urgent: delivered.urgent,
      status: 'delivered',
      step: delivered.step,
    };
  }
  const undelivered = parseUndeliveredEvent(event);
  if (undelivered) {
    useInterjectionStore.setState(s => ({
      bySession: {
        ...s.bySession,
        [sessionId]: items.map(i => (undelivered.includes(i.id) ? { ...i, status: 'undelivered' } : i)),
      },
    }));
  }
  return null;
}
