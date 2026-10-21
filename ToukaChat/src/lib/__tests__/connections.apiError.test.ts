/**
 * model-p0 U7：fetchChatStream 非 200 時保留後端錯誤碼（ToukaException 的 detail.code），
 * 丟出 ToukaApiError，讓聊天畫面能對特定錯誤（AGENT_BRAIN_NOT_SET）給專屬提示。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fetchChatStream } from '../connections';
import { ToukaApiError, getErrorCode } from '../error_handler';

const fetchMock = vi.fn();

function failWith(status: number, body: string): Promise<unknown> {
  fetchMock.mockResolvedValue(new Response(body, { status }));
  return new Promise((resolve) => {
    void fetchChatStream(
      [{ role: 'user', content: 'hi' }],
      { provider: 'proxy', baseUrl: 'http://synthetic.invalid', model: 'm' },
      () => {},
      () => resolve(undefined),
      (err) => resolve(err),
    );
  });
}

describe('fetchChatStream 非 200 錯誤碼傳遞（model-p0 U7）', () => {
  beforeEach(() => {
    window.localStorage.clear();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it('ToukaException detail → ToukaApiError（Error 子類別）帶碼與狀態', async () => {
    const detail = { code: 'AGENT_BRAIN_NOT_SET', id: 1215, message: 'x', i18n: 'error.model.agent_brain_not_set' };
    const err = await failWith(409, JSON.stringify({ detail }));
    expect(err).toBeInstanceOf(ToukaApiError);
    expect(err).toBeInstanceOf(Error);
    expect((err as ToukaApiError).status).toBe(409);
    expect(getErrorCode(err)).toBe('AGENT_BRAIN_NOT_SET');
  });

  it('沒有 detail.code → 訊息照舊、沒有錯誤碼', async () => {
    const err = await failWith(502, JSON.stringify({ error: { message: 'upstream boom' } }));
    expect((err as Error).message).toBe('upstream boom');
    expect(getErrorCode(err)).toBeUndefined();
  });

  it.each([
    ['陣列 detail（驗證錯誤）', 422, JSON.stringify({ detail: [{ loc: ['body'], msg: 'bad' }] })],
    ['字串 detail', 400, JSON.stringify({ detail: 'plain' })],
    ['非 JSON', 500, 'oops'],
    ['不在 SSOT 的碼', 409, JSON.stringify({ detail: { code: 'toString' } })],
  ])('%s → 沒有錯誤碼', async (_name, status, body) => {
    const err = await failWith(status, body);
    expect(err).toBeInstanceOf(Error);
    expect(getErrorCode(err)).toBeUndefined();
  });

  it('getErrorCode 對非 ToukaApiError 一律 undefined', () => {
    expect(getErrorCode(new Error('AGENT_BRAIN_NOT_SET'))).toBeUndefined();
    expect(getErrorCode({ code: 'AGENT_BRAIN_NOT_SET' })).toBeUndefined();
    expect(getErrorCode(undefined)).toBeUndefined();
  });
});
