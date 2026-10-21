/**
 * ADR-0081 — resumeChatStream 單元測試（先 Red 後 Green）。
 *
 * resume 走 fetchTouka（GET，自動 Authorization、FETCH_SSOT 合規），
 * 讀取後端緩衝重播的 SSE 並以與 fetchChatStream 同一套解析分發。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { resumeChatStream } from '@/lib/connections';

vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

function sseBody(lines: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const l of lines) controller.enqueue(encoder.encode(l));
      controller.close();
    },
  });
}

describe('resumeChatStream（ADR-0081）', () => {
  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchSpy = vi.fn();
    global.fetch = fetchSpy as unknown as typeof fetch;
  });

  it('以 starting_after 呼叫 resume 端點並分發 chunk / event / done', async () => {
    fetchSpy.mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers(),
      body: sseBody([
        'data: {"choices":[{"delta":{"content":"哈"}}]}\n\n',
        'data: {"type":"tool_start","id":"t1","tool":"bash","args":{}}\n\n',
        'data: {"choices":[{"delta":{"content":"囉"}}]}\n\n',
        'data: [DONE]\n\n',
      ]),
    });

    const chunks: string[] = [];
    const events: Array<Record<string, unknown>> = [];
    let done = false;

    await resumeChatStream(
      'http://localhost:8000', 'sess-1', 5,
      (c) => chunks.push(c),
      () => { done = true; },
      () => { throw new Error('不應觸發 onError'); },
      (e) => events.push(e),
    );

    const calledUrl = fetchSpy.mock.calls[0][0] as string;
    expect(calledUrl).toContain('/v1/ui/sessions/sess-1/stream');
    expect(calledUrl).toContain('starting_after=5');

    expect(chunks).toEqual(['哈', '囉']);
    expect(events).toEqual([{ type: 'tool_start', id: 't1', tool: 'bash', args: {} }]);
    expect(done).toBe(true);
  });

  it('非 2xx 回應 → onError、不觸發 onDone', async () => {
    fetchSpy.mockResolvedValue({
      ok: false, status: 404, headers: new Headers(),
      json: async () => ({}),
    });

    let done = false;
    let error: unknown = null;
    await resumeChatStream(
      'http://localhost:8000', 'sess-x', -1,
      () => {}, () => { done = true; }, (e) => { error = e; },
    );
    expect(done).toBe(false);
    expect(error).toBeTruthy();
  });

  it('跨 chunk 斷裂的 SSE 行仍正確重組（buffer 殘段保留）', async () => {
    const full = 'data: {"choices":[{"delta":{"content":"完整內容"}}]}\n\n';
    fetchSpy.mockResolvedValue({
      ok: true, status: 200, headers: new Headers(),
      body: sseBody([full.slice(0, 20), full.slice(20)]),
    });

    const chunks: string[] = [];
    await resumeChatStream(
      'http://localhost:8000', 'sess-1', -1,
      (c) => chunks.push(c), () => {}, () => { throw new Error('no error'); },
    );
    expect(chunks).toEqual(['完整內容']);
  });
});
