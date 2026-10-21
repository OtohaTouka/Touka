import { describe, it, expect } from 'vitest';
import { fetchChatStream, type ChatOptions } from '@/lib/connections';

const noop = () => {};

// 利用 onBeforeSend(body) 攔截點：回傳 false 在實際 fetch 前中止，純測 body 組裝。
async function captureBody(extra: Partial<ChatOptions>): Promise<Record<string, unknown>> {
  let body: Record<string, unknown> = {};
  const opts: ChatOptions = {
    provider: 'proxy',
    baseUrl: 'http://localhost:8000',
    model: 'gemini-x',
    ...extra,
  };
  await fetchChatStream(
    [{ role: 'user', content: 'hi' }],
    opts,
    noop,
    noop,
    noop,
    noop,
    undefined,
    async (b) => {
      body = b as Record<string, unknown>;
      return false;
    },
  );
  return body;
}

describe('fetchChatStream — ADR-0087 deep_research 旗標 threading', () => {
  it('openai 協定：deepResearch=true → body.deep_research=true', async () => {
    const body = await captureBody({ protocol: 'openai', deepResearch: true });
    expect(body.deep_research).toBe(true);
  });

  it('anthropic 協定：deepResearch=true → body.deep_research=true', async () => {
    const body = await captureBody({ protocol: 'anthropic', deepResearch: true });
    expect(body.deep_research).toBe(true);
  });

  it('未設 deepResearch → body.deep_research 為 undefined', async () => {
    const body = await captureBody({ protocol: 'openai' });
    expect(body.deep_research).toBeUndefined();
  });
});
