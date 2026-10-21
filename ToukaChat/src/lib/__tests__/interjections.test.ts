/**
 * ADR-0249 第 4a 段：插話的 SSE 契約常數、歷史重組標註、按鍵對應與回合計數。
 *
 * - SSE 事件名稱與欄位和後端 services/interjection_queue.py 同名（雙端常數）。
 * - 組下一輪歷史時，帶 interjections[] 的助理節點 content 前面加固定標註，角色仍是 assistant。
 * - 標註不改回合數：export-utils（splitSessionMessages）與 context_usage 的計數不變。
 */
import { describe, expect, it } from 'vitest';
import {
  INTERJECTION_DELIVERED_EVENT,
  INTERJECTION_EVENT_FIELDS,
  INTERJECTION_NOTE_TEMPLATE,
  INTERJECTION_UNDELIVERED_EVENT,
  composerKeyAction,
  formatInterjectionNote,
  joinUndeliveredText,
  parseDeliveredEvent,
  parseUndeliveredEvent,
  withInterjectionNotes,
} from '../chat/interjections';
import { buildApiHistory } from '../chat/buildApiMessage';
import { countRounds } from '../context_usage';
import { splitSessionMessages } from '../export-utils';
import type { MessageNode } from '../tree_history';
import type { Interjection, Session } from '../types';

const delivered = (id: string, step: number, text: string): Interjection => ({
  id, text, urgent: false, status: 'delivered', step, created_at: 1,
});

describe('SSE 契約常數（與後端同名）', () => {
  it('事件名稱與欄位是固定字串', () => {
    expect(INTERJECTION_DELIVERED_EVENT).toBe('interjection_delivered');
    expect(INTERJECTION_UNDELIVERED_EVENT).toBe('interjection_undelivered');
    expect(INTERJECTION_EVENT_FIELDS).toEqual({
      id: 'interjection_id', step: 'step', urgent: 'urgent', ids: 'interjection_ids', text: 'text',
    });
  });

  it('interjection_delivered 只收合法欄位', () => {
    expect(parseDeliveredEvent({ type: 'interjection_delivered', interjection_id: 'a', step: 2, urgent: true, text: '補充' }))
      .toEqual({ interjectionId: 'a', step: 2, urgent: true, text: '補充' });
    expect(parseDeliveredEvent({ type: 'interjection_delivered', interjection_id: 'a', step: 2, text: 7 }))
      .toEqual({ interjectionId: 'a', step: 2, urgent: false });
    expect(parseDeliveredEvent({ type: 'interjection_delivered', interjection_id: 'a', step: 0 }))
      .toEqual({ interjectionId: 'a', step: 0, urgent: false });
    for (const bad of [
      { type: 'interjection_delivered', interjection_id: '', step: 1 },
      { type: 'interjection_delivered', interjection_id: 'a', step: 1.5 },
      { type: 'interjection_delivered', interjection_id: 'a', step: -1 },
      { type: 'interjection_delivered', interjection_id: 7, step: 1 },
      { type: 'interjection_undelivered', interjection_id: 'a', step: 1 },
    ]) expect(parseDeliveredEvent(bad)).toBeNull();
  });

  it('interjection_undelivered 只留非空字串 id', () => {
    expect(parseUndeliveredEvent({ type: 'interjection_undelivered', interjection_ids: ['a', '', 3, 'b'] }))
      .toEqual(['a', 'b']);
    expect(parseUndeliveredEvent({ type: 'interjection_undelivered', interjection_ids: 'a' })).toBeNull();
    expect(parseUndeliveredEvent({ type: 'interjection_delivered', interjection_ids: ['a'] })).toBeNull();
  });
});

describe('歷史重組標註（ADR-0249 裁定 7）', () => {
  it('標註格式是固定字串', () => {
    expect(INTERJECTION_NOTE_TEMPLATE).toBe('〔本回合中使用者補充（第 {step} 步）：{text}〕');
    expect(formatInterjectionNote(3, '改用 B 方案')).toBe('〔本回合中使用者補充（第 3 步）：改用 B 方案〕');
    // 文字裡的 $& 之類不能被當成取代樣式
    expect(formatInterjectionNote(1, '價格 $& 與 $1')).toBe('〔本回合中使用者補充（第 1 步）：價格 $& 與 $1〕');
  });

  it('已送達的照步數排在 content 前面；其他狀態或空白文字略過', () => {
    const items: Interjection[] = [
      delivered('b', 4, '第二則'),
      { ...delivered('x', 2, '還在排隊'), status: 'queued' },
      delivered('a', 1, '第一則'),
      delivered('e', 3, ''),
    ];
    expect(withInterjectionNotes('最後的回答', items)).toBe(
      '〔本回合中使用者補充（第 1 步）：第一則〕\n〔本回合中使用者補充（第 4 步）：第二則〕\n\n最後的回答',
    );
    expect(withInterjectionNotes('原文', undefined)).toBe('原文');
    expect(withInterjectionNotes('原文', [])).toBe('原文');
    expect(withInterjectionNotes('原文', [{ ...delivered('u', 1, 'x'), status: 'undelivered' }])).toBe('原文');
  });

  it('buildApiHistory：助理節點加標註、角色不變；本輪 user 用 resolvedInput', () => {
    const linear: MessageNode[] = [
      { id: 'u1', parentId: null, childrenIds: ['a1'], role: 'user', content: '先查資料' },
      { id: 'a1', parentId: 'u1', childrenIds: ['u2'], role: 'assistant', content: '查好了',
        interjections: [delivered('i1', 2, '只看 2026 年')] },
      { id: 'u2', parentId: 'a1', childrenIds: [], role: 'user', content: '@file 整理' },
    ];
    expect(buildApiHistory(linear, 'u2', '整理（已展開）')).toEqual([
      { role: 'user', content: '先查資料' },
      { role: 'assistant', content: '〔本回合中使用者補充（第 2 步）：只看 2026 年〕\n\n查好了' },
      { role: 'user', content: '整理（已展開）' },
    ]);
  });
});

describe('回合數不因插話改變', () => {
  const node = (id: string, role: 'user' | 'assistant', parentId: string | null, child?: string,
    extra: Partial<MessageNode> = {}): MessageNode =>
    ({ id, role, parentId, childrenIds: child ? [child] : [], content: id, ...extra });
  const plain = [node('u1', 'user', null, 'a1'), node('a1', 'assistant', 'u1', 'u2'),
    node('u2', 'user', 'a1', 'a2'), node('a2', 'assistant', 'u2')];
  const withNotes = plain.map(m => m.role === 'assistant'
    ? { ...m, interjections: [delivered(`${m.id}-i`, 1, '補充'), delivered(`${m.id}-j`, 2, '再補充')] } : m);

  it('context_usage.countRounds 相同', () => {
    expect(countRounds(plain)).toBe(2);
    expect(countRounds(withNotes)).toBe(2);
  });

  it('export-utils.splitSessionMessages 的切點相同', () => {
    const session = (messages: MessageNode[]): Session => ({
      id: 's', title: 't', updatedAt: 1, agent_id: 'Alice',
      messages: { messages: Object.fromEntries(messages.map(m => [m.id, m])), currentId: 'a2' },
    });
    for (const messages of [plain, withNotes]) {
      const split = splitSessionMessages(session(messages), 1);
      expect(split?.legacyMessages.map(m => m.id)).toEqual(['u1', 'a1']);
      expect(split?.keepMessages.map(m => m.id)).toEqual(['u2', 'a2']);
      expect(split?.legacyUserCount).toBe(1);
      expect(splitSessionMessages(session(messages), 2)).toBeNull();
    }
  });
});

describe('輸入框按鍵（DB-20）', () => {
  const key = (k: string, mods: Partial<{ shiftKey: boolean; ctrlKey: boolean; metaKey: boolean }> = {}) =>
    ({ key: k, shiftKey: false, ctrlKey: false, metaKey: false, ...mods });

  it('沒在生成：Enter 照舊送出、Esc 不動作', () => {
    expect(composerKeyAction(key('Enter'), false)).toBe('send');
    expect(composerKeyAction(key('Enter', { ctrlKey: true }), false)).toBe('send');
    expect(composerKeyAction(key('Enter', { shiftKey: true }), false)).toBeNull();
    expect(composerKeyAction(key('Escape'), false)).toBeNull();
  });

  it('生成中：Enter 排隊、Ctrl／Cmd+Enter 優先排隊、Esc 停止、Shift+Enter 換行', () => {
    expect(composerKeyAction(key('Enter'), true)).toBe('queue');
    expect(composerKeyAction(key('Enter', { ctrlKey: true }), true)).toBe('queue_urgent');
    expect(composerKeyAction(key('Enter', { metaKey: true }), true)).toBe('queue_urgent');
    expect(composerKeyAction(key('Enter', { shiftKey: true }), true)).toBeNull();
    expect(composerKeyAction(key('Escape'), true)).toBe('stop');
    expect(composerKeyAction(key('a'), true)).toBeNull();
  });

  it('未送達的文字合成下一則訊息', () => {
    expect(joinUndeliveredText([{ text: ' 一 ' }, { text: '' }, { text: '二' }])).toBe('一\n\n二');
    expect(joinUndeliveredText([])).toBe('');
  });
});
