/**
 * model-p0 U7：fetchChatStream 的送出內容一律不帶取樣參數（溫度、Top P、Top K 交給廠商預設）。
 * 涵蓋 proxy 兩種協定、瀏覽器直連 openai／openrouter／litellm／ollama，以及 default 分支；
 * 呼叫端即使塞了 temperature 也不得轉送。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fetchChatStream, type BackendProvider, type ChatOptions } from '../connections';

const fetchMock = vi.fn();
const SAMPLING_KEYS = ['temperature', 'top_p', 'top_k'];

async function sentBody(extra: Partial<ChatOptions>): Promise<Record<string, unknown>> {
  fetchMock.mockResolvedValue(new Response('data: [DONE]\n', { status: 200 }));
  await new Promise<void>((resolve) => {
    void fetchChatStream(
      [{ role: 'user', content: 'hi' }],
      { provider: 'proxy', baseUrl: 'http://synthetic.invalid', model: 'm', temperature: 0.9, ...extra } as ChatOptions,
      () => {},
      () => resolve(),
      () => resolve(),
    );
  });
  const init = fetchMock.mock.calls[0][1] as { body: string };
  return JSON.parse(init.body);
}

describe('fetchChatStream 不送取樣參數（model-p0 U7）', () => {
  beforeEach(() => {
    window.localStorage.clear();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it.each<[string, Partial<ChatOptions>]>([
    ['proxy anthropic', { provider: 'proxy', protocol: 'anthropic' }],
    ['proxy openai', { provider: 'proxy', protocol: 'openai' }],
    ['openai', { provider: 'openai' }],
    ['openrouter', { provider: 'openrouter' }],
    ['litellm', { provider: 'litellm' }],
    ['ollama', { provider: 'ollama' }],
    ['default', { provider: 'unlisted' as BackendProvider }],
  ])('%s：body 沒有 temperature／top_p／top_k', async (_name, extra) => {
    const body = await sentBody(extra);
    expect(body.model).toBe('m');
    for (const key of SAMPLING_KEYS) expect(body).not.toHaveProperty(key);
  });

  it('ollama：不送 options（原本只裝 temperature）', async () => {
    const body = await sentBody({ provider: 'ollama' });
    expect(body).not.toHaveProperty('options');
  });
});
