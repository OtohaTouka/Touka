/**
 * ChatProvider — 從靈魂開新對話時換回一般模型，不寫死模型名（ADR-0250 A2）。
 *
 * 找得到可用的一般模型（pickDefaultModel：啟用、沒隱藏、非代理、不離線）就換並提示；
 * 找不到就不切換、不跳通知（不再換成不存在的舊模型名）。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import type { ModelInfo } from '../ModelProvider';

let models: { availableModels: ModelInfo[]; selectedModel: string } = { availableModels: [], selectedModel: '' };
const setSelectedModel = vi.fn();
const syncSession = vi.fn().mockResolvedValue({ status: 'saved', representation: 'full', revision: 'r1' });
const setSessions = vi.fn();

vi.mock('@/hooks/useArtifacts', () => ({
  useArtifacts: Object.assign(() => ({}), { getState: () => ({ setManuallyClosed: vi.fn() }) }),
}));
vi.mock('@/hooks/useArtifactExtractor', () => ({
  useArtifactExtractor: () => ({ processStreamChunk: vi.fn(), finalizeStream: vi.fn() }),
}));
vi.mock('@/hooks/useChatStream', () => ({
  useChatStream: () => ({ handleSendStream: vi.fn(), handleStopGeneration: vi.fn() }),
}));
vi.mock('../ModelProvider', () => ({
  useModels: () => ({ ...models, setSelectedModel }),
}));
vi.mock('../SessionListProvider', () => ({
  useSessionList: () => ({ sessions: [], setSessions, syncSession, updateSession: vi.fn(), refreshSessions: vi.fn(async () => []) }),
}));
vi.mock('../StreamRegistryProvider', () => ({
  useStreamRegistry: () => ({ getStream: () => undefined, streamingSessionIds: [], checkSessionStream: vi.fn() }),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('../../connections', () => ({
  getActiveProxyUrl: () => 'http://x', fetchTouka: vi.fn(),
  captureRequestScope: vi.fn(), isRequestScopeCurrent: () => true,
}));
vi.mock('sonner', () => ({ toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() } }));

import { toast } from 'sonner';
import { ChatProvider, useChat } from '../ChatProvider';

const wrapper = ({ children }: { children: React.ReactNode }) => <ChatProvider>{children}</ChatProvider>;

const soul = (id: string): ModelInfo => ({ id, name: id, owned_by: 'touka', enabled: true, is_agent: true });

describe('ChatProvider 開新對話時的預設模型（ADR-0250 A2）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('選的是靈魂、只剩代理模型可用：不切換、不跳通知', () => {
    models = { availableModels: [soul('Alice'), soul('Bob')], selectedModel: 'Alice' };
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.handleNewSession(); });
    expect(setSelectedModel).not.toHaveBeenCalled();
    expect(toast.info).not.toHaveBeenCalled();
  });

  it('選的是靈魂、有一般模型：換成第一個可用的一般模型並提示', () => {
    models = {
      availableModels: [
        soul('Alice'),
        { id: 'down', name: 'Down', owned_by: 'p', enabled: true, status: 'offline' },
        { id: 'gpt-x', name: 'GPT X', owned_by: 'p', enabled: true, status: 'online' },
      ],
      selectedModel: 'Alice',
    };
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.handleNewSession(); });
    expect(setSelectedModel).toHaveBeenCalledWith('gpt-x');
    expect(toast.info).toHaveBeenCalledWith('chat.provider.model_switched_to_default');
  });

  it('選的是一般模型：開新對話不動選擇', () => {
    models = {
      availableModels: [soul('Alice'), { id: 'gpt-x', name: 'GPT X', owned_by: 'p', enabled: true }],
      selectedModel: 'gpt-x',
    };
    const { result } = renderHook(() => useChat(), { wrapper });
    act(() => { result.current.handleNewSession(); });
    expect(setSelectedModel).not.toHaveBeenCalled();
    expect(toast.info).not.toHaveBeenCalled();
  });
});
