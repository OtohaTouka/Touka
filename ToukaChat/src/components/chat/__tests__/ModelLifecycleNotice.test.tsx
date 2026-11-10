/**
 * 模型快下架提示（ADR-0310）：GET /v1/models/lifecycle-alerts。只提醒，不換模型。
 * 看不懂的回應當成沒有；換帳號先清掉上一個人的提示再重問；後端還在查清單時等一下再問一次。
 */
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import en from '@/locales/en/translation.json';
import zhTW from '@/locales/zh-TW/translation.json';

const state = vi.hoisted(() => ({ signedIn: true, subscribers: [] as Array<() => void> }));
vi.mock('@/lib/connections', () => ({
  fetchTouka: vi.fn(),
  getActiveProxyUrl: () => 'http://proxy.test',
  captureRequestScope: () => (state.signedIn ? { proxyOrigin: 'http://proxy.test', authEpoch: 0 } : null),
  subscribeCredentialScope: (listener: () => void) => {
    state.subscribers.push(listener);
    return () => { state.subscribers = state.subscribers.filter(item => item !== listener); };
  },
}));
import { fetchTouka } from '@/lib/connections';
import {
  MODEL_LIFECYCLE_POLL_MS,
  MODEL_LIFECYCLE_RECHECK_MS,
} from '@/hooks/useModelLifecycleAlerts';
import { ModelLifecycleNotice } from '../ModelLifecycleNotice';

const RETIRES = 1_796_058_000; // 2026-11-30T17:00:00Z，台北是 12/1
const alert = (overrides: Record<string, unknown> = {}) => ({
  model: 'claude-old', status: 'retiring', retires_at: RETIRES, souls: ['Alice'],
  reviewer: false, summarizer: false, ...overrides,
});
const reply = (body: unknown, ok = true) => ({ ok, status: ok ? 200 : 500, json: async () => body }) as Response;
const answer = (body: unknown, ok = true) => vi.mocked(fetchTouka).mockResolvedValue(reply(body, ok));
const notice = () => screen.queryByTestId('model-lifecycle-notice');
const flush = async () => { await act(async () => { await vi.advanceTimersByTimeAsync(1); }); };
const mountWith = async (body: unknown, ok = true) => {
  answer(body, ok);
  render(<ModelLifecycleNotice />);
  await flush();
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  state.signedIn = true;
  state.subscribers = [];
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('ModelLifecycleNotice', () => {
  it('沒有提醒時不顯示', async () => {
    await mountWith({ alerts: [], checking: false });
    expect(fetchTouka).toHaveBeenCalledOnce();
    expect(notice()).toBeNull();
  });

  it('打對的端點，帶目前的登入範圍', async () => {
    await mountWith({ alerts: [], checking: false });
    const [url, path, options] = vi.mocked(fetchTouka).mock.calls[0];
    expect(url).toBe('http://proxy.test');
    expect(path).toBe('/v1/models/lifecycle-alerts');
    expect(options).toMatchObject({ requestScope: { proxyOrigin: 'http://proxy.test' } });
  });

  it('快下架：講模型、台北日期、誰在用，也講不會自動換', async () => {
    await mountWith({ alerts: [alert()], checking: false });
    expect(notice()).toHaveTextContent('模型 claude-old 預計在 2026/12/1 下架。');
    expect(notice()).toHaveTextContent('使用中：Alice。');
    expect(notice()).toHaveTextContent('Touka 不會自動換模型');
  });

  it.each([
    ['overdue', '已過官方排定的下架日（2026/12/1）'],
    ['date_unknown', '下架日期未定'],
    ['retired', '已被官方標示下架'],
    ['not_found', '廠商回覆找不到這個模型'],
  ])('%s 的說法', async (status, text) => {
    await mountWith({ alerts: [alert({ status })], checking: false });
    expect(notice()).toHaveTextContent(text);
  });

  it('審查員和摘要員用翻譯過的名字，和靈魂列在一起', async () => {
    await mountWith({
      alerts: [alert({ souls: ['Alice', 'Bob'], reviewer: true, summarizer: true })], checking: false,
    });
    expect(notice()).toHaveTextContent('使用中：Alice、Bob、Auto 審查員、摘要員。');
  });

  it('只有審查員在用時不會多一個空名字', async () => {
    await mountWith({ alerts: [alert({ souls: [], reviewer: true })], checking: false });
    expect(notice()).toHaveTextContent('使用中：Auto 審查員。');
  });

  it('多個模型各一行', async () => {
    await mountWith({ alerts: [alert(), alert({ model: 'gemini-old', status: 'retired' })], checking: false });
    expect(notice()?.querySelectorAll('p')).toHaveLength(2);
  });

  it('沒登入不問', async () => {
    state.signedIn = false;
    await mountWith({ alerts: [alert()], checking: false });
    expect(fetchTouka).not.toHaveBeenCalled();
    expect(notice()).toBeNull();
  });

  it.each([
    ['null', null],
    ['不是物件', 'x'],
    ['沒有 alerts', {}],
    ['alerts 不是陣列', { alerts: 'x' }],
  ])('回應看不懂（%s）當成沒有', async (_name, body) => {
    await mountWith(body);
    expect(notice()).toBeNull();
  });

  it('HTTP 失敗當成沒有', async () => {
    await mountWith({ alerts: [alert()] }, false);
    expect(notice()).toBeNull();
  });

  it('連線失敗當成沒有', async () => {
    vi.mocked(fetchTouka).mockRejectedValue(new Error('down'));
    render(<ModelLifecycleNotice />);
    await flush();
    expect(notice()).toBeNull();
  });

  it('看不懂的那一筆略過，其他照顯示', async () => {
    await mountWith({ alerts: [alert({ status: 'melting' }), alert({ model: 7 }), 'x', alert({ model: 'claude-ok' })] });
    expect(notice()?.querySelectorAll('p')).toHaveLength(1);
    expect(notice()).toHaveTextContent('claude-ok');
  });

  it('souls 形狀不對不會炸', async () => {
    await mountWith({ alerts: [alert({ souls: 'Alice', reviewer: true })] });
    expect(notice()).toHaveTextContent('使用中：Auto 審查員。');
  });

  it('換帳號：先清掉上一個人的提示，再重問', async () => {
    await mountWith({ alerts: [alert()], checking: false });
    expect(notice()).not.toBeNull();
    let resolve: (value: Response) => void = () => undefined;
    vi.mocked(fetchTouka).mockReturnValue(new Promise(done => { resolve = done; }));
    act(() => { state.subscribers.forEach(listener => listener()); });
    expect(notice()).toBeNull();
    expect(fetchTouka).toHaveBeenCalledTimes(2);
    await act(async () => { resolve(reply({ alerts: [alert({ model: 'claude-next' })], checking: false })); });
    await flush();
    expect(notice()).toHaveTextContent('claude-next');
  });

  it('登出後不顯示', async () => {
    await mountWith({ alerts: [alert()], checking: false });
    state.signedIn = false;
    await act(async () => { state.subscribers.forEach(listener => listener()); });
    expect(notice()).toBeNull();
  });

  it('沒有憑證變動的通知時，下一次重問發現登出也會清掉', async () => {
    await mountWith({ alerts: [alert()], checking: false });
    state.signedIn = false;
    await act(async () => { await vi.advanceTimersByTimeAsync(MODEL_LIFECYCLE_POLL_MS); });
    expect(notice()).toBeNull();
  });

  it.each([
    ['連線失敗', () => { vi.mocked(fetchTouka).mockRejectedValue(new Error('down')); }],
    ['HTTP 500', () => { answer({}, false); }],
  ])('重問時%s：保留上一次的提示，正確的提示不會消失一小時', async (_name, fail) => {
    await mountWith({ alerts: [alert()], checking: false });
    fail();
    await act(async () => { await vi.advanceTimersByTimeAsync(MODEL_LIFECYCLE_POLL_MS); });
    expect(fetchTouka).toHaveBeenCalledTimes(2);
    expect(notice()).not.toBeNull();
  });

  it.each([
    ['不是物件', 'x'],
    ['alerts 不是陣列', { alerts: 'x' }],
  ])('重問拿到 200 但看不懂（%s）：當成沒有，舊的提示要清掉', async (_name, body) => {
    await mountWith({ alerts: [alert()], checking: false });
    answer(body);
    await act(async () => { await vi.advanceTimersByTimeAsync(MODEL_LIFECYCLE_POLL_MS); });
    expect(fetchTouka).toHaveBeenCalledTimes(2);
    expect(notice()).toBeNull();
  });

  it('舊請求晚到不覆蓋換帳號後的結果', async () => {
    let resolveOld: (value: Response) => void = () => undefined;
    vi.mocked(fetchTouka).mockReturnValueOnce(new Promise(done => { resolveOld = done; }));
    render(<ModelLifecycleNotice />);
    await flush();
    answer({ alerts: [alert({ model: 'claude-next' })], checking: false });
    await act(async () => { state.subscribers.forEach(listener => listener()); });
    await flush();
    await act(async () => { resolveOld(reply({ alerts: [alert({ model: 'claude-stale' })], checking: false })); });
    await flush();
    expect(notice()).toHaveTextContent('claude-next');
    expect(notice()).not.toHaveTextContent('claude-stale');
  });

  it('後端還在查：等一下再問一次，查完就停', async () => {
    await mountWith({ alerts: [], checking: true });
    expect(fetchTouka).toHaveBeenCalledTimes(1);
    answer({ alerts: [alert()], checking: false });
    await act(async () => { await vi.advanceTimersByTimeAsync(MODEL_LIFECYCLE_RECHECK_MS); });
    expect(fetchTouka).toHaveBeenCalledTimes(2);
    expect(notice()).not.toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(MODEL_LIFECYCLE_RECHECK_MS * 5); });
    expect(fetchTouka).toHaveBeenCalledTimes(2);
  });

  it('沒在查時不會多問', async () => {
    await mountWith({ alerts: [], checking: false });
    await act(async () => { await vi.advanceTimersByTimeAsync(MODEL_LIFECYCLE_RECHECK_MS * 2); });
    expect(fetchTouka).toHaveBeenCalledTimes(1);
  });

  it('分頁開很久也會重問', async () => {
    await mountWith({ alerts: [], checking: false });
    await act(async () => { await vi.advanceTimersByTimeAsync(MODEL_LIFECYCLE_POLL_MS); });
    expect(fetchTouka).toHaveBeenCalledTimes(2);
  });

  it('關掉之後不再問', async () => {
    answer({ alerts: [], checking: true });
    const { unmount } = render(<ModelLifecycleNotice />);
    await flush();
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(MODEL_LIFECYCLE_POLL_MS); });
    expect(fetchTouka).toHaveBeenCalledTimes(1);
    expect(state.subscribers).toHaveLength(0);
  });

  it('兩種語言的文字都有', () => {
    expect(Object.keys(en.chat.model_lifecycle).sort()).toEqual(Object.keys(zhTW.chat.model_lifecycle).sort());
    expect(en.chat.model_lifecycle.retiring).toContain('{{model}}');
    expect(en.chat.model_lifecycle.retiring).toContain('{{date}}');
  });
});
