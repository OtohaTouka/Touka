/**
 * 批⑤-F E30（ADR-0059）— fetchChatStream 的 x-touka-skills header「建構」。
 *
 * 後端消費側（SkillLayer 讀 header）全真執法；前端建構側先前只有字串檢查＝
 * 假綠（改壞 join/刪行全綠）。本檔以真 fetchChatStream＋stub fetch 驅動：
 * - active_skills 非空 → 送出 `x-touka-skills`（逗號串接）
 * - 空/缺席 → 不送 header
 *
 * 骨架前例：connections.reasoning.test.ts（ADR-0152 T2）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fetchChatStream } from '../connections';

const fetchMock = vi.fn();

function sseDone() {
  return new Response('data: [DONE]\n', {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

function run(extra: Record<string, unknown>) {
  return new Promise<void>((resolve) => {
    void fetchChatStream(
      [{ role: 'user', content: 'hi' }],
      {
        provider: 'proxy',
        baseUrl: 'http://proxy.test',
        model: 'm',
        protocol: 'openai',
        ...extra,
      },
      () => {},
      () => resolve(),
      () => resolve(),
    );
  });
}

function sentHeaders(): Record<string, string> {
  const call = fetchMock.mock.calls[0] as [string, { headers: Record<string, string> }];
  return call[1].headers;
}

describe('fetchChatStream x-touka-skills header 建構（E30/ADR-0059）', () => {
  beforeEach(() => {
    window.localStorage.clear();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it('active_skills 非空 → 送出 x-touka-skills（逗號串接、順序保留）', async () => {
    fetchMock.mockResolvedValue(sseDone());
    await run({ active_skills: ['skill-a', 'skill-b'] });
    expect(sentHeaders()['x-touka-skills']).toBe('skill-a,skill-b');
  });

  it('active_skills 空陣列 → 不送 header（後端不收空殼）', async () => {
    fetchMock.mockResolvedValue(sseDone());
    await run({ active_skills: [] });
    expect(sentHeaders()).not.toHaveProperty('x-touka-skills');
  });

  it('active_skills 缺席 → 不送 header', async () => {
    fetchMock.mockResolvedValue(sseDone());
    await run({});
    expect(sentHeaders()).not.toHaveProperty('x-touka-skills');
  });
});
