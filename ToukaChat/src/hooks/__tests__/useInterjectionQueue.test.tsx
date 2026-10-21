/**
 * ADR-0249 第 4a 段：插話資料層（useInterjectionQueue）與輸入框行為（useInterjectionComposer）。
 *
 * - 走 fetchTouka 打 U4a-1 的三個端點，一律帶 stream_id。
 * - 還沒拿到 stream_id 或送不出去：留在前端，回合結束後當成下一則送出。
 * - 回合結束：正常完成就自動送出未送達的；按停止或出錯則放回輸入框。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { fetchTouka } from '@/lib/connections';
import { useStreamRegistry } from '@/lib/providers/StreamRegistryProvider';
import { toast } from 'sonner';
import { useInterjectionStore } from '@/lib/chat/interjectionStore';
import { useInterjectionQueue } from '../useInterjectionQueue';
import { useInterjectionComposer } from '../useInterjectionComposer';

vi.mock('@/lib/connections', () => ({
  fetchTouka: vi.fn(),
  isRequestScopeCurrent: vi.fn(() => true),
}));
vi.mock('@/lib/providers/StreamRegistryProvider', () => ({ useStreamRegistry: vi.fn() }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() } }));

const mockFetch = vi.mocked(fetchTouka);
const toastError = vi.mocked(toast.error);
const scope = { proxyOrigin: 'http://proxy.test', authenticatedPrincipalId: 'owner', authEpoch: 1 };
const target = { sessionId: 'sess/1', streamId: 'stream-1', requestScope: scope };
const serverItem = (id: string, text: string, urgent = false) =>
  ({ id, text, urgent, status: 'queued', step: null, created_at: 1 });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

beforeEach(() => {
  vi.clearAllMocks();
  useInterjectionStore.setState({ bySession: {} });
});

describe('useInterjectionQueue', () => {
  it('POST 帶 stream_id 與 {text, urgent}，成功後列入佇列', async () => {
    mockFetch.mockResolvedValue(json(serverItem('i1', '補充', true), 201));
    const { result } = renderHook(() => useInterjectionQueue(target));
    await act(async () => { await result.current.enqueue('補充', true); });
    const [origin, path, init] = mockFetch.mock.calls[0];
    expect([origin, path]).toEqual(['http://proxy.test', '/v1/ui/sessions/sess%2F1/interjections?stream_id=stream-1']);
    expect(init?.method).toBe('POST');
    expect(JSON.parse(init?.body as string)).toEqual({ text: '補充', urgent: true });
    expect(init?.requestScope).toBe(scope);
    expect(result.current.items.map(i => [i.id, i.status])).toEqual([['i1', 'queued']]);
  });

  it('還沒拿到 stream_id：不打 API，留在前端標成未送達', async () => {
    const { result } = renderHook(() => useInterjectionQueue({ ...target, streamId: undefined }));
    await act(async () => { await result.current.enqueue('先排著', false); });
    expect(mockFetch).not.toHaveBeenCalled();
    expect(result.current.items.map(i => [i.text, i.status, i.local])).toEqual([['先排著', 'undelivered', true]]);
  });

  it('伺服器拒絕（例如超量）：顯示錯誤，文字留在前端不遺失', async () => {
    mockFetch.mockResolvedValue(json({ detail: { code: 'INTERJECTION_QUEUE_FULL', id: 1216 } }, 429));
    const { result } = renderHook(() => useInterjectionQueue(target));
    await act(async () => { await result.current.enqueue('第 11 則', false); });
    expect(toastError).toHaveBeenCalledWith('這一則回覆的補充太多了');
    expect(result.current.items.map(i => [i.text, i.local])).toEqual([['第 11 則', true]]);
  });

  it('收回走 DELETE 並帶 stream_id；前端暫存的直接移除', async () => {
    mockFetch.mockResolvedValueOnce(json(serverItem('i/1', 'x'), 201));
    const { result } = renderHook(() => useInterjectionQueue(target));
    await act(async () => { await result.current.enqueue('x', false); });
    mockFetch.mockResolvedValueOnce(json({ ...serverItem('i/1', 'x'), status: 'retracted' }));
    await act(async () => { expect(await result.current.retract(result.current.items[0])).toBe(true); });
    expect(mockFetch.mock.calls[1].slice(0, 2)).toEqual(
      ['http://proxy.test', '/v1/ui/sessions/sess%2F1/interjections/i%2F1?stream_id=stream-1']);
    expect(mockFetch.mock.calls[1][2]?.method).toBe('DELETE');
    expect(result.current.items).toEqual([]);

    const local = renderHook(() => useInterjectionQueue({ ...target, streamId: undefined })).result;
    await act(async () => { await local.current.enqueue('local', false); });
    await act(async () => { expect(await local.current.retract(local.current.items[0])).toBe(true); });
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('已交給 AI 的收回被拒：提示並保留', async () => {
    mockFetch.mockResolvedValueOnce(json(serverItem('i1', 'x'), 201));
    const { result } = renderHook(() => useInterjectionQueue(target));
    await act(async () => { await result.current.enqueue('x', false); });
    mockFetch.mockResolvedValueOnce(json({ detail: { code: 'INTERJECTION_NOT_RETRACTABLE', id: 1219 } }, 409));
    await act(async () => { expect(await result.current.retract(result.current.items[0])).toBe(false); });
    expect(toastError).toHaveBeenCalledWith('這則補充已交給 AI，無法收回');
    expect(result.current.items.map(i => i.id)).toEqual(['i1']);
  });
});

describe('useInterjectionComposer', () => {
  type Props = { sessionId: string; isSending: boolean; inputValue: string };
  const send = vi.fn(async () => {});
  const stop = vi.fn();
  const setInputValue = vi.fn();
  let streamStatus = 'streaming';

  beforeEach(() => {
    streamStatus = 'streaming';
    vi.mocked(useStreamRegistry).mockImplementation(() => ({
      getStream: () => ({ status: streamStatus, started: { stream_id: 'stream-1' }, requestScope: scope,
        assistantMsgId: 'assistant-1' }),
    }) as unknown as ReturnType<typeof useStreamRegistry>);
  });

  const setup = (initial: Props) => renderHook((p: Props) => useInterjectionComposer({
    ...p, disabled: false, setInputValue, send, stop,
  }), { initialProps: initial });
  const keyEvent = (key: string, mods: Partial<KeyboardEvent> = {}) =>
    ({ key, shiftKey: false, ctrlKey: false, metaKey: false, preventDefault: vi.fn(), ...mods }) as never;

  it('生成中按 Enter／Ctrl+Enter 排隊並清空輸入框；Esc 停止', async () => {
    mockFetch.mockResolvedValue(json(serverItem('i1', '改用 B'), 201));
    const { result } = setup({ sessionId: 's1', isSending: true, inputValue: ' 改用 B ' });
    await act(async () => { result.current.onKeyDown(keyEvent('Enter', { ctrlKey: true })); });
    expect(JSON.parse(mockFetch.mock.calls[0][2]?.body as string)).toEqual({ text: '改用 B', urgent: true });
    expect(setInputValue).toHaveBeenCalledWith('');
    expect(send).not.toHaveBeenCalled();
    act(() => { result.current.onKeyDown(keyEvent('Escape')); });
    expect(stop).toHaveBeenCalledTimes(1);
  });

  it('沒在生成時 Enter 照舊送出', () => {
    const { result } = setup({ sessionId: 's1', isSending: false, inputValue: 'hi' });
    act(() => { result.current.onKeyDown(keyEvent('Enter')); });
    expect(send).toHaveBeenCalledWith();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('回合正常結束：未送達的合成下一則自動送出，佇列清空', () => {
    const { add } = useInterjectionStore.getState();
    add('s1', { ...serverItem('a', '第一則'), status: 'undelivered' });
    add('s1', serverItem('b', '第二則'));
    const hook = setup({ sessionId: 's1', isSending: true, inputValue: '' });
    hook.rerender({ sessionId: 's1', isSending: false, inputValue: '' });
    expect(send).toHaveBeenCalledWith('第一則\n\n第二則', 'assistant-1');
    expect(useInterjectionStore.getState().bySession.s1).toEqual([]);
  });

  it('按停止後結束：不自動送出，放回輸入框（保留原本的草稿）', () => {
    useInterjectionStore.getState().add('s1', serverItem('a', '補充'));
    const hook = setup({ sessionId: 's1', isSending: true, inputValue: '草稿' });
    act(() => { hook.result.current.stop(); });
    hook.rerender({ sessionId: 's1', isSending: false, inputValue: '草稿' });
    expect(send).not.toHaveBeenCalled();
    expect(setInputValue).toHaveBeenCalledWith('草稿\n補充');
  });

  it('出錯結束：同樣放回輸入框', () => {
    useInterjectionStore.getState().add('s1', serverItem('a', '補充'));
    const hook = setup({ sessionId: 's1', isSending: true, inputValue: '' });
    streamStatus = 'error';
    hook.rerender({ sessionId: 's1', isSending: false, inputValue: '' });
    expect(send).not.toHaveBeenCalled();
    expect(setInputValue).toHaveBeenCalledWith('補充');
  });

  it('切到別的對話不算回合結束（兩邊留著的項目都不動）', () => {
    useInterjectionStore.getState().add('s1', serverItem('a', '補充'));
    useInterjectionStore.getState().add('s2', { ...serverItem('b', '背景對話留下的'), status: 'undelivered' });
    const hook = setup({ sessionId: 's1', isSending: true, inputValue: '' });
    hook.rerender({ sessionId: 's2', isSending: false, inputValue: '' });
    expect(send).not.toHaveBeenCalled();
    expect(setInputValue).not.toHaveBeenCalled();
    expect(useInterjectionStore.getState().bySession.s1.map(i => i.id)).toEqual(['a']);
    expect(useInterjectionStore.getState().bySession.s2.map(i => i.id)).toEqual(['b']);
  });

  it('在 A 按停止後切到 B：停止旗標歸零，B 正常結束時照樣自動送出', () => {
    const hook = setup({ sessionId: 's1', isSending: true, inputValue: '' });
    act(() => { hook.result.current.stop(); });
    hook.rerender({ sessionId: 's2', isSending: true, inputValue: '' });
    useInterjectionStore.getState().add('s2', serverItem('b', 'B 的補充'));
    hook.rerender({ sessionId: 's2', isSending: false, inputValue: '' });
    expect(send).toHaveBeenCalledWith('B 的補充', 'assistant-1');
    expect(setInputValue).not.toHaveBeenCalled();
  });

  it('串流剛結束時插話被拒（404）：文字放回輸入框，不留在佇列', async () => {
    let resolvePost!: (response: Response) => void;
    mockFetch.mockReturnValue(new Promise<Response>(resolve => { resolvePost = resolve; }));
    const hook = setup({ sessionId: 's1', isSending: true, inputValue: '晚了一步' });
    act(() => { hook.result.current.onKeyDown(keyEvent('Enter')); });
    expect(setInputValue).toHaveBeenLastCalledWith('');
    hook.rerender({ sessionId: 's1', isSending: false, inputValue: '' });
    await act(async () => { resolvePost(json({ detail: { code: 'STREAM_NOT_FOUND', id: 1212 } }, 404)); });
    expect(setInputValue).toHaveBeenLastCalledWith('晚了一步');
    expect(useInterjectionStore.getState().bySession.s1).toEqual([]);
    expect(send).not.toHaveBeenCalled();
  });

  it('伺服器 201 晚於回合結束才到：從佇列移出、放回輸入框，不以 queued 留在清單', async () => {
    let resolvePost!: (response: Response) => void;
    mockFetch.mockReturnValue(new Promise<Response>(resolve => { resolvePost = resolve; }));
    const hook = setup({ sessionId: 's1', isSending: true, inputValue: '晚到的補充' });
    act(() => { hook.result.current.onKeyDown(keyEvent('Enter')); });
    hook.rerender({ sessionId: 's1', isSending: false, inputValue: '' });
    await act(async () => { resolvePost(json(serverItem('late-1', '晚到的補充'), 201)); });
    expect(setInputValue).toHaveBeenLastCalledWith('晚到的補充');
    expect(useInterjectionStore.getState().bySession.s1).toEqual([]);
    expect(send).not.toHaveBeenCalled();
  });

  it('回應到時已切到別的對話：留在原對話的佇列，不放進別的對話的輸入框', async () => {
    let resolvePost!: (response: Response) => void;
    mockFetch.mockReturnValue(new Promise<Response>(resolve => { resolvePost = resolve; }));
    const hook = setup({ sessionId: 's1', isSending: true, inputValue: '給 A 的補充' });
    act(() => { hook.result.current.onKeyDown(keyEvent('Enter')); });
    hook.rerender({ sessionId: 's2', isSending: false, inputValue: 'B 的草稿' });
    await act(async () => { resolvePost(json({ detail: { code: 'STREAM_NOT_FOUND', id: 1212 } }, 404)); });
    expect(setInputValue).toHaveBeenLastCalledWith('');
    expect(useInterjectionStore.getState().bySession.s1.map(i => i.text)).toEqual(['給 A 的補充']);
    expect(useInterjectionStore.getState().bySession.s2 ?? []).toEqual([]);
  });

  it('修改＝收回後把文字放回輸入框', async () => {
    useInterjectionStore.getState().add('s1', serverItem('a', '要改的'));
    mockFetch.mockResolvedValue(json({ ...serverItem('a', '要改的'), status: 'retracted' }));
    const { result } = setup({ sessionId: 's1', isSending: true, inputValue: '' });
    await act(async () => { await result.current.edit(result.current.items[0]); });
    expect(mockFetch.mock.calls[0][2]?.method).toBe('DELETE');
    expect(setInputValue).toHaveBeenCalledWith('要改的');
    expect(result.current.items).toEqual([]);
  });
});
