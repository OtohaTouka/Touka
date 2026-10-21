/**
 * ADR-0152 T2 — fetchChatStream 的思考分頻解析（dispatchSseLine SSOT）。
 *
 * 後端把思考走 `delta.reasoning_content`（DeepSeek 串流慣例）：
 * - reasoning chunk → onThinking（不得混入 onChunk 正文）
 * - content chunk → onChunk（不受影響）
 * - 未接 onThinking 的呼叫端 → 安全忽略（不炸、不漏進正文）
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fetchChatStream } from '../connections';

const fetchMock = vi.fn();

function sseResponse(lines: string[]) {
  const body = lines.map(l => `data: ${l}\n`).join('') + 'data: [DONE]\n';
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

const reasoningChunk = JSON.stringify({
  choices: [{ delta: { reasoning_content: '思考步驟一' }, index: 0 }],
});
const contentChunk = JSON.stringify({
  choices: [{ delta: { content: '正文回答' }, index: 0 }],
});

function run(onThinking?: (t: string) => void) {
  return new Promise<{ chunks: string[]; err?: unknown }>((resolve) => {
    const chunks: string[] = [];
    void fetchChatStream(
      [{ role: 'user', content: 'hi' }],
      { provider: 'proxy', baseUrl: 'http://proxy.test', model: 'm', protocol: 'openai' },
      (t) => chunks.push(t),
      () => resolve({ chunks }),
      (err) => resolve({ chunks, err }),
      undefined,
      undefined,
      undefined,
      onThinking,
    );
  });
}

describe('fetchChatStream 思考分頻（ADR-0152）', () => {
  beforeEach(() => {
    window.localStorage.clear();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it('reasoning_content → onThinking；content → onChunk；互不混流', async () => {
    fetchMock.mockResolvedValue(sseResponse([reasoningChunk, contentChunk]));
    const thinking: string[] = [];
    const { chunks, err } = await run((t) => thinking.push(t));
    expect(err).toBeUndefined();
    expect(thinking.join('')).toBe('思考步驟一');
    expect(chunks.join('')).toBe('正文回答');
  });

  it('未接 onThinking → reasoning 安全忽略、不漏進正文', async () => {
    fetchMock.mockResolvedValue(sseResponse([reasoningChunk, contentChunk]));
    const { chunks, err } = await run(undefined);
    expect(err).toBeUndefined();
    expect(chunks.join('')).toBe('正文回答');
    expect(chunks.join('')).not.toContain('思考步驟一');
  });
});
