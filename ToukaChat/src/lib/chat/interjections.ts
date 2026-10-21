/**
 * ADR-0249 第 4a 段：插話（AI 工作中使用者的補充）的契約常數與純函式。
 *
 * - SSE 事件名稱與欄位：和後端 services/interjection_queue.py 同名（雙端常數，U4a-2 照這份送）。
 * - 歷史重組標註：組下一輪 API 訊息時，加在帶 interjections[] 的助理節點 content 前面，
 *   角色仍是 assistant，回合數不變（裁定 7）。
 * - 輸入框按鍵對應（DB-20）。
 */
import type { Interjection } from '@/lib/types';

export const INTERJECTION_DELIVERED_EVENT = 'interjection_delivered';
export const INTERJECTION_UNDELIVERED_EVENT = 'interjection_undelivered';
export const INTERJECTION_EVENT_TYPES = [INTERJECTION_DELIVERED_EVENT, INTERJECTION_UNDELIVERED_EVENT];
/** ADR-0249 第 4b 段：Ctrl+Enter 打斷正在寫的回答（後端送；寫進助理節點 interruptions[]）。 */
export const ROUND_INTERRUPTED_EVENT = 'round_interrupted_by_user';
export const INTERJECTION_EVENT_FIELDS = {
  id: 'interjection_id',
  step: 'step',
  urgent: 'urgent',
  ids: 'interjection_ids',
  text: 'text',
} as const;

/** 給模型看的固定格式（不是畫面文字，不走 i18n）。 */
export const INTERJECTION_NOTE_TEMPLATE = '〔本回合中使用者補充（第 {step} 步）：{text}〕';

export interface DeliveredInterjection {
  interjectionId: string;
  step: number;
  urgent: boolean;
  /** 事件帶的原文；沒帶時由呼叫端退回本地佇列的原文。 */
  text?: string;
}

export function parseDeliveredEvent(event: Record<string, unknown>): DeliveredInterjection | null {
  const id = event[INTERJECTION_EVENT_FIELDS.id];
  const step = event[INTERJECTION_EVENT_FIELDS.step];
  if (event.type !== INTERJECTION_DELIVERED_EVENT || typeof id !== 'string' || !id ||
      typeof step !== 'number' || !Number.isInteger(step) || step < 0) return null;
  const text = event[INTERJECTION_EVENT_FIELDS.text];
  return {
    interjectionId: id, step, urgent: event[INTERJECTION_EVENT_FIELDS.urgent] === true,
    ...(typeof text === 'string' ? { text } : {}),
  };
}

export interface RoundInterruption {
  step: number;
}

export function parseRoundInterruptedEvent(event: Record<string, unknown>): RoundInterruption | null {
  const step = event[INTERJECTION_EVENT_FIELDS.step];
  if (event.type !== ROUND_INTERRUPTED_EVENT || typeof step !== 'number' || !Number.isInteger(step) || step < 0) return null;
  return { step };
}

export function parseUndeliveredEvent(event: Record<string, unknown>): string[] | null {
  const ids = event[INTERJECTION_EVENT_FIELDS.ids];
  if (event.type !== INTERJECTION_UNDELIVERED_EVENT || !Array.isArray(ids)) return null;
  return ids.filter((id): id is string => typeof id === 'string' && id.length > 0);
}

export function formatInterjectionNote(step: number, text: string): string {
  // 用函式取代，避免文字裡的 $& 之類被當成取代樣式。
  return INTERJECTION_NOTE_TEMPLATE.replace('{step}', () => String(step)).replace('{text}', () => text);
}

/** 已送達、有文字的插話照步數排在 content 前面；沒有就原樣返回。 */
export function withInterjectionNotes(content: string, interjections?: Interjection[]): string {
  const notes = (Array.isArray(interjections) ? interjections : [])
    .filter(i => i?.status === 'delivered' && typeof i.step === 'number' && typeof i.text === 'string' && i.text)
    .sort((a, b) => (a.step as number) - (b.step as number))
    .map(i => formatInterjectionNote(i.step as number, i.text));
  return notes.length ? `${notes.join('\n')}\n\n${content}` : content;
}

/** 回合結束時，未送達的合成下一則訊息。 */
export function joinUndeliveredText(items: { text: string }[]): string {
  return items.map(i => i.text.trim()).filter(Boolean).join('\n\n');
}

export type ComposerKeyAction = 'send' | 'queue' | 'queue_urgent' | 'stop' | null;

/** Enter＝送出（生成中＝排隊）、Ctrl／Cmd+Enter＝優先排隊、Esc＝停止、Shift+Enter＝換行。 */
export function composerKeyAction(
  e: { key: string; shiftKey: boolean; ctrlKey: boolean; metaKey: boolean },
  isSending: boolean,
): ComposerKeyAction {
  if (e.key === 'Escape') return isSending ? 'stop' : null;
  if (e.key !== 'Enter' || e.shiftKey) return null;
  if (!isSending) return 'send';
  return e.ctrlKey || e.metaKey ? 'queue_urgent' : 'queue';
}
