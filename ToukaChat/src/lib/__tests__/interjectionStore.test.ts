/**
 * ADR-0249 第 4a 段：插話佇列（前端）、SSE 事件套用與寫進助理節點 interjections[]。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { applyInterjectionEvent, useInterjectionStore } from '../chat/interjectionStore';
import { recordInterjection } from '../stream_tree';
import { withInterjectionNotes } from '../chat/interjections';
import { fetchChatStream } from '../connections';
import type { TreeHistory } from '../tree_history';
import type { Interjection } from '../types';

const queued = (id: string, text: string, urgent = false): Interjection => ({
  id, text, urgent, status: 'queued', step: null, created_at: 10,
});

beforeEach(() => useInterjectionStore.setState({ bySession: {} }));

describe('applyInterjectionEvent', () => {
  it('delivered：從佇列移除並回傳要寫進節點的紀錄（帶原文）', () => {
    const { add } = useInterjectionStore.getState();
    add('s1', queued('a', '只看 2026 年'));
    add('s1', queued('b', '還在排'));
    add('s2', queued('a', '別的對話'));
    const record = applyInterjectionEvent('s1',
      { type: 'interjection_delivered', interjection_id: 'a', step: 3, urgent: true });
    expect(record).toEqual({ id: 'a', text: '只看 2026 年', urgent: true, status: 'delivered', step: 3, created_at: 10 });
    expect(useInterjectionStore.getState().bySession.s1.map(i => i.id)).toEqual(['b']);
    expect(useInterjectionStore.getState().bySession.s2.map(i => i.text)).toEqual(['別的對話']);
  });

  it('delivered 帶原文：優先用事件的 text（本地佇列的舊文字不蓋過）', () => {
    useInterjectionStore.getState().add('s1', queued('a', '本地文字'));
    const record = applyInterjectionEvent('s1',
      { type: 'interjection_delivered', interjection_id: 'a', step: 2, text: '伺服器原文' });
    expect(record?.text).toBe('伺服器原文');
  });

  it('重整後重播（本地佇列是空的）：事件的 text 寫進節點，下一輪標註看得到', () => {
    const record = applyInterjectionEvent('s1',
      { type: 'interjection_delivered', interjection_id: 'r1', step: 4, urgent: false, text: '只看 2026 年' });
    const tree: TreeHistory = { currentId: 'a1', messages: {
      a1: { id: 'a1', parentId: null, childrenIds: [], role: 'assistant', content: '結果' } } };
    const node = recordInterjection(tree, 'a1', record!).messages.a1;
    expect(withInterjectionNotes(node.content, node.interjections))
      .toBe('〔本回合中使用者補充（第 4 步）：只看 2026 年〕\n\n結果');
  });

  it('delivered 沒帶 text 且前端不知道原文：仍記步數、文字留空', () => {
    vi.spyOn(Date.prototype, 'getTime').mockReturnValue(5000);
    const record = applyInterjectionEvent('s1',
      { type: 'interjection_delivered', interjection_id: 'zz', step: 1 });
    expect(record).toEqual({ id: 'zz', text: '', urgent: false, status: 'delivered', step: 1, created_at: 5 });
    vi.restoreAllMocks();
  });

  it('undelivered：只改這個對話裡對應 id 的狀態，不回傳節點紀錄', () => {
    const { add } = useInterjectionStore.getState();
    add('s1', queued('a', 'x'));
    add('s1', queued('b', 'y'));
    add('s2', queued('a', 'z'));
    expect(applyInterjectionEvent('s1', { type: 'interjection_undelivered', interjection_ids: ['a', 'nope'] })).toBeNull();
    expect(useInterjectionStore.getState().bySession.s1.map(i => [i.id, i.status])).toEqual([['a', 'undelivered'], ['b', 'queued']]);
    expect(useInterjectionStore.getState().bySession.s2.map(i => i.status)).toEqual(['queued']);
  });

  it('其他或格式錯誤的事件不動佇列', () => {
    useInterjectionStore.getState().add('s1', queued('a', 'x'));
    expect(applyInterjectionEvent('s1', { type: 'interjection_delivered', interjection_id: 'a' })).toBeNull();
    expect(applyInterjectionEvent('s1', { type: 'tool_start', id: 'a' })).toBeNull();
    expect(useInterjectionStore.getState().bySession.s1.map(i => i.status)).toEqual(['queued']);
  });

  it('take 取出並清空該對話；remove 只移除指定 id', () => {
    const { add, remove, take } = useInterjectionStore.getState();
    add('s1', queued('a', 'x'));
    add('s1', queued('b', 'y'));
    add('s2', queued('c', 'z'));
    remove('s1', 'a');
    expect(take('s1').map(i => i.id)).toEqual(['b']);
    expect(take('s1')).toEqual([]);
    expect(useInterjectionStore.getState().bySession.s2.map(i => i.id)).toEqual(['c']);
  });
});

describe('recordInterjection（stream_tree）', () => {
  const tree: TreeHistory = {
    currentId: 'a1',
    messages: {
      u1: { id: 'u1', parentId: null, childrenIds: ['a1'], role: 'user', content: 'q' },
      a1: { id: 'a1', parentId: 'u1', childrenIds: [], role: 'assistant', content: '', steps: [] },
    },
  };
  const record: Interjection = { id: 'i1', text: '補充', urgent: false, status: 'delivered', step: 2, created_at: 1 };

  it('寫進助理節點 interjections[]，同 id 不重複', () => {
    const once = recordInterjection(tree, 'a1', record);
    expect(once.messages.a1.interjections).toEqual([record]);
    expect(tree.messages.a1.interjections).toBeUndefined();
    const twice = recordInterjection(once, 'a1', { ...record, step: 5 });
    expect(twice.messages.a1.interjections).toEqual([{ ...record, step: 5 }]);
    const other = recordInterjection(twice, 'a1', { ...record, id: 'i2', step: 3 });
    expect(other.messages.a1.interjections?.map(i => i.id)).toEqual(['i1', 'i2']);
  });

  it('節點不存在：原樹返回', () => {
    expect(recordInterjection(tree, 'gone', record)).toBe(tree);
  });
});

describe('dispatchSseLine 認得插話事件（SSE 契約）', () => {
  const fetchMock = vi.fn();
  beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal('fetch', fetchMock); });
  afterEach(() => vi.unstubAllGlobals());

  it('interjection_delivered／interjection_undelivered 經 onEvent 分發', async () => {
    const lines = [
      { type: 'interjection_delivered', interjection_id: 'a', step: 1, urgent: false },
      { type: 'interjection_undelivered', interjection_ids: ['b'] },
    ].map(e => `data: ${JSON.stringify(e)}\n`).join('') + 'data: [DONE]\n';
    fetchMock.mockResolvedValue(new Response(lines, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
    const events: Record<string, unknown>[] = [];
    await new Promise<void>((resolve) => {
      void fetchChatStream(
        [{ role: 'user', content: 'hi' }],
        { provider: 'proxy', baseUrl: 'http://proxy.test', model: 'm', protocol: 'openai' },
        () => {}, () => resolve(), () => resolve(), (e) => events.push(e),
      );
    });
    expect(events.map(e => e.type)).toEqual(['interjection_delivered', 'interjection_undelivered']);
  });
});
