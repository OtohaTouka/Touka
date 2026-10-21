/**
 * ADR-0080：useChatStream 瘦身為「發送前準備 + 委派 StreamRegistry」後的單元測試。
 *
 * 串流生命週期已下沉至 registry（見 StreamRegistry.test.tsx）；此處只驗證委派：
 * - handleSendStream 對既有 session → 呼叫 startStream 帶正確 sessionId/model/resolvedInput
 * - 無 active session → 先建新 session，再對新 session startStream
 * - handleStopGeneration → 呼叫 stopStream(activeSessionId)
 * - startStream 回 false（並發上限）→ toast 提示
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useChatStream } from '@/hooks/useChatStream';
import * as ModelProvider from '@/lib/providers/ModelProvider';
import * as SessionProvider from '@/lib/providers/SessionListProvider';
import * as DebugProvider from '@/lib/providers/DebugProvider';
import * as StreamRegistry from '@/lib/providers/StreamRegistryProvider';
import * as connections from '@/lib/connections';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';

vi.mock('react-i18next', async (importOriginal) => ({
  ...await importOriginal<typeof import('react-i18next')>(),
  useTranslation: vi.fn(),
}));

vi.mock('@/lib/connections');
vi.mock('@/lib/mentions', () => ({ resolveMentions: vi.fn((v: string) => Promise.resolve(`resolved:${v}`)) }));
vi.mock('@/lib/providers/ModelProvider');
vi.mock('@/lib/providers/SessionListProvider');
vi.mock('@/lib/providers/DebugProvider');
vi.mock('@/lib/providers/StreamRegistryProvider');

const emptyTree = { messages: {}, currentId: null };

describe('useChatStream（委派 StreamRegistry）', () => {
  let startStream: ReturnType<typeof vi.fn>;
  let stopStream: ReturnType<typeof vi.fn>;
  let setSessions: ReturnType<typeof vi.fn>;
  let syncSession: ReturnType<typeof vi.fn>;
  let setActiveSessionId: ReturnType<typeof vi.fn<(id: string) => void>>;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useTranslation).mockReturnValue({ t: (key: string) => key } as never);
    startStream = vi.fn().mockResolvedValue(true);
    syncSession = vi.fn().mockResolvedValue({ status: 'saved', representation: 'full', revision: 'created' });
    stopStream = vi.fn();
    setSessions = vi.fn();
    setActiveSessionId = vi.fn();

    vi.spyOn(ModelProvider, 'useModels').mockReturnValue({ selectedModel: 'gpt-4o' } as never);
    vi.spyOn(SessionProvider, 'useSessionList').mockReturnValue({
      sessions: [{ id: 'sess-1', messages: {}, title: 't', auth_level: 'Auto', active_skills: ['s1'] }],
      setSessions, syncSession,
      getSession: (id: string) => ({ id, agent_id: 'gpt-4o', auth_level: 'Auto', active_skills: ['s1'] }),
    } as never);
    vi.spyOn(DebugProvider, 'useDebug').mockReturnValue({
      setLastRequestPayload: vi.fn(), setIsSimulationMode: vi.fn(),
    } as never);
    vi.spyOn(StreamRegistry, 'useStreamRegistry').mockReturnValue({
      startStream, stopStream, getStream: vi.fn(), streamingSessionIds: [], activeCount: 0,
    } as never);
    vi.spyOn(connections, 'getActiveConnection').mockReturnValue({ protocol: 'openai' } as never);
    vi.spyOn(connections, 'getActiveProxyUrl').mockReturnValue('http://localhost:8000');
    vi.mocked(connections.captureRequestScope).mockReturnValue({ proxyOrigin: 'http://localhost:8000', authenticatedPrincipalId: 'a', authEpoch: 1 });
    vi.mocked(connections.isRequestScopeCurrent).mockReturnValue(true);
  });

  const opts = (activeSessionId = 'sess-1') => ({
    staticHistory: emptyTree, activeSessionId, setActiveSessionId, setPendingInstall: vi.fn(),
    setPendingAsk: vi.fn(),
  });

  it('既有 session：委派 startStream 帶正確 sessionId/model/resolvedInput/skills', async () => {
    const { result } = renderHook(() => useChatStream(opts()));
    await act(async () => { await result.current.handleSendStream('hello', null); });

    expect(startStream).toHaveBeenCalledTimes(1);
    const arg = startStream.mock.calls[0][0];
    expect(arg.sessionId).toBe('sess-1');
    expect(arg.model).toBe('gpt-4o');
    expect(arg.resolvedInput).toBe('resolved:hello');
    expect(arg.userText).toBe('hello');
    expect(arg.authLevel).toBe('Auto');
    expect(arg.activeSkills).toEqual(['s1']);
  });

  it('無 active session：先建新 session 再對其 startStream', async () => {
    const { result } = renderHook(() => useChatStream(opts('')));
    await act(async () => { await result.current.handleSendStream('hi', null); });

    expect(syncSession).toHaveBeenCalledTimes(1);
    expect(setSessions).not.toHaveBeenCalled();
    expect(setActiveSessionId).toHaveBeenCalled();
    const newId = setActiveSessionId.mock.calls[0][0];
    expect(startStream.mock.calls[0][0].sessionId).toBe(newId);
  });

  it('handleStopGeneration：委派 stopStream(activeSessionId)', () => {
    const { result } = renderHook(() => useChatStream(opts('sess-1')));
    act(() => { result.current.handleStopGeneration(); });
    expect(stopStream).toHaveBeenCalledWith('sess-1');
  });

  it('startStream 回 false：保留輸入，hook 不誤報所有失敗為額滿', async () => {
    startStream.mockResolvedValue(false);
    const warnSpy = vi.spyOn(toast, 'warning').mockImplementation((() => '') as never);
    const { result } = renderHook(() => useChatStream(opts()));
    const reset = vi.fn();
    await act(async () => { await result.current.handleSendStream('hello', null, undefined, reset); });
    expect(startStream).toHaveBeenCalledTimes(1);
    expect(reset).not.toHaveBeenCalled();
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('切換語言後仍將 admission 結果交給 registry，不重複提示或清輸入', async () => {
    const tA = (key: string) => `A:${key}`;
    const tB = (key: string) => `B:${key}`;
    vi.mocked(useTranslation).mockReturnValue({ t: tA } as never);
    startStream.mockResolvedValue(false);
    const warnSpy = vi.spyOn(toast, 'warning').mockReturnValue('toast-id');
    const stableOptions = opts();
    const { result, rerender } = renderHook(() => useChatStream(stableOptions));

    await act(async () => { await result.current.handleSendStream('hello', null); });
    expect(warnSpy).not.toHaveBeenCalled();

    vi.mocked(useTranslation).mockReturnValue({ t: tB } as never);
    rerender();
    await act(async () => { await result.current.handleSendStream('hello', null); });

    expect(warnSpy).not.toHaveBeenCalled();
    expect(startStream).toHaveBeenCalledTimes(2);
  });
});
