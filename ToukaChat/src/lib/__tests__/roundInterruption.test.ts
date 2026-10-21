/**
 * ADR-0249 第 4b 段（U4b-2）：Ctrl+Enter 中斷正在寫的回答——`round_interrupted_by_user` 事件。
 *
 * - 事件名稱固定（後端 U4b-1 送），欄位至少 step；形狀不對回 null。
 * - 收到時寫進同一個助理節點的 interruptions[]，重整後重播或從存檔讀回仍看得到。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ROUND_INTERRUPTED_EVENT, parseRoundInterruptedEvent } from '../chat/interjections';
import { applyEventToTree } from '../stream_tree';
import { fetchChatStream } from '../connections';
import type { TreeHistory } from '../tree_history';

const tree: TreeHistory = {
  currentId: 'a1',
  messages: {
    u1: { id: 'u1', parentId: null, childrenIds: ['a1'], role: 'user', content: 'q' },
    a1: { id: 'a1', parentId: 'u1', childrenIds: [], role: 'assistant', content: '', steps: [] },
  },
};

describe('round_interrupted_by_user 事件', () => {
  it('名稱是固定字串', () => {
    expect(ROUND_INTERRUPTED_EVENT).toBe('round_interrupted_by_user');
  });

  it('只收合法的 step', () => {
    expect(parseRoundInterruptedEvent({ type: 'round_interrupted_by_user', step: 2 })).toEqual({ step: 2 });
    expect(parseRoundInterruptedEvent({ type: 'round_interrupted_by_user', step: 0, extra: 'x' })).toEqual({ step: 0 });
    for (const bad of [
      { type: 'round_interrupted_by_user' },
      { type: 'round_interrupted_by_user', step: '2' },
      { type: 'round_interrupted_by_user', step: 1.5 },
      { type: 'round_interrupted_by_user', step: -1 },
      { type: 'interjection_delivered', step: 2 },
    ]) expect(parseRoundInterruptedEvent(bad)).toBeNull();
  });

  it('寫進助理節點 interruptions[]（同一步被打斷兩次就記兩筆）', () => {
    const event = { type: 'round_interrupted_by_user', step: 2 };
    const once = applyEventToTree(tree, 'a1', event);
    expect(once.messages.a1.interruptions).toEqual([{ step: 2 }]);
    expect(tree.messages.a1.interruptions).toBeUndefined();
    const twice = applyEventToTree(once, 'a1', event);
    expect(twice.messages.a1.interruptions).toEqual([{ step: 2 }, { step: 2 }]);
    expect(twice.messages.a1.steps).toEqual([]);
  });

  it('形狀不對或節點不存在：原樹返回', () => {
    expect(applyEventToTree(tree, 'a1', { type: 'round_interrupted_by_user', step: 'x' })).toBe(tree);
    expect(applyEventToTree(tree, 'gone', { type: 'round_interrupted_by_user', step: 1 })).toBe(tree);
  });
});

describe('dispatchSseLine 認得 round_interrupted_by_user', () => {
  const fetchMock = vi.fn();
  beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal('fetch', fetchMock); });
  afterEach(() => vi.unstubAllGlobals());

  it('經 onEvent 分發', async () => {
    const body = `data: ${JSON.stringify({ type: 'round_interrupted_by_user', step: 1 })}\ndata: [DONE]\n`;
    fetchMock.mockResolvedValue(new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
    const events: Record<string, unknown>[] = [];
    await new Promise<void>((resolve) => {
      void fetchChatStream(
        [{ role: 'user', content: 'hi' }],
        { provider: 'proxy', baseUrl: 'http://proxy.test', model: 'm', protocol: 'openai' },
        () => {}, () => resolve(), () => resolve(), (e) => events.push(e),
      );
    });
    expect(events).toEqual([{ type: 'round_interrupted_by_user', step: 1 }]);
  });
});
