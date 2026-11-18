/**
 * ADR-0322 PR 2b：畫面上的小提示。工具計時、攔截、中斷、警告這類系統說明改由 turn_notice 事件送來，
 * 存在助理節點的 notices（有上限），不進回覆文字、不送給模型；舊回覆裡的日誌行組歷史時拿掉
 * （黃金案例和後端共用 legacyLogLines.golden.json）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchChatStream } from '../../connections';
import { applyEventToTree } from '../../stream_tree';
import type { TreeHistory } from '../../tree_history';
import {
  MAX_NOTICE_CHARS, MAX_NOTICES_PER_NODE, TURN_NOTICE_EVENT, parseTurnNoticeEvent, stripLegacyLogLines,
} from '../turnNotices';
import goldenCases from './legacyLogLines.golden.json';

const notice = (kind: string, params: unknown = {}) => ({ type: TURN_NOTICE_EVENT, kind, params, text: '顯示用' });

describe('stripLegacyLogLines', () => {
  it.each(goldenCases)('$name', ({ input, expected }) => {
    expect(stripLegacyLogLines(input)).toBe(expected);
  });
});

describe('parseTurnNoticeEvent', () => {
  it('keeps the kind and the string or integer params, capped', () => {
    const parsed = parseTurnNoticeEvent(notice('tool_finished', {
      tool: 't'.repeat(400), rounds: 3, ratio: 1.5, flag: true, nested: { a: 1 },
    }));
    expect(parsed).toEqual({ kind: 'tool_finished', params: { tool: 't'.repeat(MAX_NOTICE_CHARS), rounds: 3 } });
  });

  it.each([
    ['an unknown kind', notice('made_up')],
    ['an object built-in name', notice('constructor')],
    ['params that are not an object', notice('tool_finished', 'x')],
    ['another event type', { type: 'log', kind: 'tool_finished', params: {} }],
  ])('rejects %s', (_label, event) => {
    expect(parseTurnNoticeEvent(event)).toBeNull();
  });
});

describe('applyEventToTree with notices', () => {
  const tree: TreeHistory = {
    messages: { reply: { id: 'reply', role: 'assistant', content: '回覆', parentId: null, childrenIds: [] } },
    currentId: 'reply',
  };

  it('appends notices to the reply up to the cap and never touches the text', () => {
    let next = tree;
    for (let i = 0; i < MAX_NOTICES_PER_NODE + 1; i += 1) {
      next = applyEventToTree(next, 'reply', notice('tool_finished', { tool: 'read' }));
    }
    next = applyEventToTree(next, 'reply', notice('made_up'));
    expect(next.messages.reply.content).toBe('回覆');
    expect(next.messages.reply.notices).toHaveLength(MAX_NOTICES_PER_NODE);
    expect(next.messages.reply.notices?.[0]).toEqual({ kind: 'tool_finished', params: { tool: 'read' } });
    expect(tree.messages.reply.notices).toBeUndefined();
  });
});

describe('turn_notice passes the SSE event whitelist', () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    window.localStorage.clear();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it('reaches onEvent instead of being dropped', async () => {
    const body = `data: ${JSON.stringify(notice('interrupted'))}\ndata: [DONE]\n`;
    fetchMock.mockResolvedValue(new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
    const events: Record<string, unknown>[] = [];
    await new Promise<void>((resolve) => {
      void fetchChatStream(
        [{ role: 'user', content: 'hi' }],
        { provider: 'proxy', baseUrl: 'http://proxy.test', model: 'm', protocol: 'openai' },
        () => {}, () => resolve(), () => resolve(), (e) => events.push(e),
      );
    });
    expect(events.map(e => e.type)).toContain(TURN_NOTICE_EVENT);
  });
});
