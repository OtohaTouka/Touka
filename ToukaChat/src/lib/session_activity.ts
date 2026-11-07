import type { SessionActivity } from './types';

/** 側欄每幾毫秒查一次工作狀態（只在分頁顯示中）。 */
export const ACTIVITY_POLL_MS = 8_000;
/** 連續幾次查不到就先不亮心跳狀態。 */
export const MAX_ACTIVITY_FAILURES = 3;
/** 每次查詢最多下載幾段新出現的對話，其餘留到下一次。 */
export const MAX_DISCOVERIES_PER_POLL = 5;
/** 後端讀不出某段對話時（可能只是資料庫暫時忙），讀不出來滿幾次才放棄；中間讀到就不算。 */
export const MAX_UNREADABLE_ATTEMPTS = 3;

export type SessionWorkStatus =
  | { kind: 'awaiting' }
  | { kind: 'replying' }
  | { kind: 'heartbeat'; current: number; total: number }
  | { kind: 'unseen' };

const URGENCY: Record<SessionWorkStatus['kind'], number> = { awaiting: 3, replying: 2, heartbeat: 1, unseen: 0 };

/** 列上只顯示一種：等您處理 ＞ 工作中 ＞ 小點。 */
export function mostUrgentStatus(statuses: readonly (SessionWorkStatus | null | undefined)[]): SessionWorkStatus | null {
  let urgent: SessionWorkStatus | null = null;
  for (const status of statuses) {
    if (status && (!urgent || URGENCY[status.kind] > URGENCY[urgent.kind])) urgent = status;
  }
  return urgent;
}

/** 小點只是提醒，工作已經結束了。 */
export function isBusyStatus(status: SessionWorkStatus | null | undefined): boolean {
  return !!status && status.kind !== 'unseen';
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const isInt = (value: unknown): value is number => Number.isSafeInteger(value);

/** 看不懂的回應當成查詢失敗，不能拿來判斷工作結束了。 */
export function parseSessionActivity(value: unknown): SessionActivity | null {
  if (!isRecord(value) || !isInt(value.cursor)) return null;
  const { streams, background, finished } = value;
  if (!Array.isArray(streams) || !streams.every(id => typeof id === 'string')) return null;
  if (!Array.isArray(background) || !background.every(row => isRecord(row) && typeof row.session_id === 'string' &&
      typeof row.kind === 'string' && isInt(row.current) && isInt(row.total))) return null;
  if (!Array.isArray(finished) || !finished.every(row => isRecord(row) && typeof row.session_id === 'string' &&
      isInt(row.finished_at))) return null;
  return value as unknown as SessionActivity;
}
