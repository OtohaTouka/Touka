/**
 * 批⑤-F E42（ADR-0080）— ChatProvider artifact 協調 wiring 的呼叫時機。
 *
 * 病史：唯一 ChatProvider 測試把 useArtifactExtractor 整支 mock、
 * processStreamChunk/finalizeStream 只出現在 mock 行 never in expect()＝零斷言；
 * 砍 `if (activeStream)` 閘或刪 finalize 分支全綠。本檔以可控 StreamRegistry
 * 驅動 :143-154 的效應，鎖住：
 * - active 串流有 aiText → processStreamChunk 即時餵入
 * - active entry 消失（串流完成）→ finalizeStream 以最後文字回填
 * - 從未有 active 串流 → 兩者皆不觸發
 *
 * 骨架前例：ChatProvider.authLevel.test.tsx（同一 mock 佈局）。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

const processStreamChunk = vi.fn();
const finalizeStream = vi.fn();
let sessions: Record<string, unknown>[] = [];
const setSessions = vi.fn();
let currentStream:
  | { tree: Record<string, unknown>; status: string; aiText: string }
  | undefined;

vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: Object.assign(() => ({}), { getState: () => ({ setManuallyClosed: vi.fn() }) }),
}));
vi.mock('@/hooks/useArtifactExtractor', () => ({
  useArtifactExtractor: () => ({ processStreamChunk, finalizeStream }),
}));
vi.mock('@/hooks/useChatStream', () => ({
  useChatStream: () => ({ handleSendStream: vi.fn(), handleStopGeneration: vi.fn() }),
}));
vi.mock('../ModelProvider', () => ({
  useModels: () => ({ availableModels: [], selectedModel: 'm1', setSelectedModel: vi.fn() }),
}));
vi.mock('../SessionListProvider', () => ({
  useSessionList: () => ({ sessions, setSessions, syncSession: vi.fn(), refreshSessions: vi.fn() }),
}));
vi.mock('../StreamRegistryProvider', () => ({
  useStreamRegistry: () => ({ getStream: () => currentStream, streamingSessionIds: [], checkSessionStream: vi.fn() }),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('../../connections', () => ({ getActiveProxyUrl: () => 'http://x', fetchTouka: vi.fn() }));
vi.mock('sonner', () => ({ toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() } }));

import { ChatProvider, useChat } from '../ChatProvider';

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <ChatProvider>{children}</ChatProvider>
);

describe('ChatProvider artifact 協調 wiring（E42/ADR-0080）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    currentStream = undefined;
    sessions = [{ id: 's1', title: 's1', messages: {}, updatedAt: 1 }];
  });

  it('active 串流 aiText → processStreamChunk；entry 消失 → finalizeStream 回填最後文字', () => {
    const { result, rerender } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });

    // 串流中：chunk 即時餵入（變異 :144 `if (activeStream)` → `if (false)` 即紅）
    currentStream = { tree: {}, status: 'streaming', aiText: '<artifact>part1' };
    act(() => { rerender(); });
    expect(processStreamChunk).toHaveBeenCalledWith('<artifact>part1');
    expect(finalizeStream).not.toHaveBeenCalled();

    // 串流完成：active entry 消失 → finalize（開面板＋回填 staticHistory）
    // （變異 :147 finalize 分支 → `else if (false)` 即紅）
    currentStream = undefined;
    act(() => { rerender(); });
    expect(finalizeStream).toHaveBeenCalledTimes(1);
    expect(finalizeStream).toHaveBeenCalledWith('<artifact>part1');
  });

  it('從未有 active 串流 → 兩者皆不觸發（背景串流不打擾面板）', () => {
    const { result, rerender } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.setActiveSessionId('s1'); });
    act(() => { rerender(); });

    expect(processStreamChunk).not.toHaveBeenCalled();
    expect(finalizeStream).not.toHaveBeenCalled();
  });
});
