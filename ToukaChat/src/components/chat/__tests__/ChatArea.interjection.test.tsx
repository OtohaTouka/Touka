/**
 * ADR-0249（R-4 ② LOW 3）：ChatArea 的輸入框與停止鈕都經 useInterjectionComposer。
 * 生成中 Enter＝排隊（不送新訊息）、Esc＝停止；停止鈕會設定停止旗標，所以回合結束時
 * 未送達的放回輸入框，而不是自動送出。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';

vi.mock('react-i18next', async (importOriginal) => ({
  ...await importOriginal<typeof import('react-i18next')>(),
  useTranslation: () => ({ t: (key: string) => key }),
  Trans: ({ children }: { children?: React.ReactNode }) => children ?? null,
}));
vi.mock('react-virtuoso', async () => {
  const react = await import('react');
  const Virtuoso = react.forwardRef(() => null);
  Virtuoso.displayName = 'Virtuoso';
  return { Virtuoso };
});
// jsdom 沒有 matchMedia（ChatArea 用它判斷手機版）
vi.mock('@/hooks/useMediaQuery', () => ({ useMediaQuery: () => false }));

const chat = vi.hoisted(() => ({
  state: {} as Record<string, unknown>,
  handleSend: vi.fn(),
  handleStopGeneration: vi.fn(),
  setInputValue: vi.fn(),
}));
const fetchTouka = vi.hoisted(() => vi.fn());

vi.mock('@/lib/ThemeContext', () => ({ useTheme: () => ({ density: 'comfortable' }) }));
vi.mock('@/lib/providers/ChatProvider', () => ({ useChat: () => chat.state }));
vi.mock('@/lib/providers/ModelProvider', () => ({
  useModels: () => ({ availableModels: [{ id: 'm1', name: 'M1', status: 'online' }], selectedModel: 'm1',
    setSelectedModel: vi.fn() }),
}));
vi.mock('@/lib/providers/SessionListProvider', () => ({ useSessionList: () => ({ sessions: [{ id: 's1' }], setSessions: vi.fn() }) }));
vi.mock('@/lib/providers/ProjectProvider', () => ({ useProjects: () => ({ projects: [] }) }));
vi.mock('@/hooks/useFeedback', () => ({ useFeedback: () => ({ feedbackState: {}, submit: vi.fn() }) }));
vi.mock('../AuthSwitcher', () => ({ AuthSwitcher: () => null }));
vi.mock('../SkillSelector', () => ({ SkillSelector: () => null }));
vi.mock('../ContextMeter', () => ({ ContextMeter: () => null }));
vi.mock('../SessionPersistenceNotice', () => ({ SessionPersistenceNotice: () => null }));
vi.mock('../MessageItem', () => ({ MessageItem: () => null }));
vi.mock('../ChatWelcome', () => ({ ChatWelcome: () => null }));
vi.mock('../FeedbackReasonDialog', () => ({ FeedbackReasonDialog: () => null }));
vi.mock('@/components/PlanTracker', () => ({ PlanTracker: () => null }));
vi.mock('@/components/ui/MentionInput', () => ({
  MentionInput: ({ value, onKeyDown }: { value: string; onKeyDown: React.KeyboardEventHandler<HTMLTextAreaElement> }) => (
    <textarea data-testid="mention-input" value={value} onChange={() => {}} onKeyDown={onKeyDown} />
  ),
}));
vi.mock('@/components/ui/select', () => ({
  Select: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SelectContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SelectItem: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SelectTrigger: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SelectValue: () => null,
}));
vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://x', fetchTouka, isRequestScopeCurrent: () => true,
}));
vi.mock('@/lib/providers/StreamRegistryProvider', () => ({
  useStreamRegistry: () => ({ getStream: () => ({ status: 'streaming', started: { stream_id: 'st1' },
    requestScope: { proxyOrigin: 'http://x', authenticatedPrincipalId: 'u', authEpoch: 1 }, assistantMsgId: 'a1' }) }),
}));
vi.mock('@/lib/bridge', () => ({ bridgeApplyArtifact: vi.fn() }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), errorMsg: vi.fn(), getErrorMessage: vi.fn() }));
vi.mock('sonner', () => ({ toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() } }));

import { ChatArea } from '../ChatArea';
import { useInterjectionStore } from '@/lib/chat/interjectionStore';

function renderChatArea() {
  const view = () => (
    <ChatArea isIDE={false} fileInputRef={React.createRef<HTMLInputElement>()} handleScreenshot={vi.fn()}
      editingMessageId={null} setEditingMessageId={vi.fn()} editValue="" setEditValue={vi.fn()} />
  );
  const { rerender } = render(view());
  return () => rerender(view());
}

describe('ChatArea × 插話輸入框', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useInterjectionStore.setState({ bySession: {} });
    chat.state = {
      history: { messages: {}, currentId: null }, inputValue: '改看 B', setInputValue: chat.setInputValue,
      attachment: null, setAttachment: vi.fn(), isSending: true,
      handleSend: chat.handleSend, handleStopGeneration: chat.handleStopGeneration,
      handleDeleteMessage: vi.fn(), handleSwitchMessage: vi.fn(), handleRegenerate: vi.fn(),
      handleEditMessage: vi.fn(), activeSessionId: 's1',
    };
    fetchTouka.mockResolvedValue(new Response(JSON.stringify(
      { id: 'i1', text: '改看 B', urgent: false, status: 'queued', step: null, created_at: 1 }), { status: 201 }));
  });

  it('生成中 Enter＝排隊：打插話端點、清空輸入框，不送新訊息', async () => {
    renderChatArea();
    await act(async () => { fireEvent.keyDown(screen.getByTestId('mention-input'), { key: 'Enter' }); });
    expect(chat.handleSend).not.toHaveBeenCalled();
    expect(chat.setInputValue).toHaveBeenCalledWith('');
    expect(fetchTouka.mock.calls[0][1]).toBe('/v1/ui/sessions/s1/interjections?stream_id=st1');
  });

  it('生成中 Esc＝停止', () => {
    renderChatArea();
    fireEvent.keyDown(screen.getByTestId('mention-input'), { key: 'Escape' });
    expect(chat.handleStopGeneration).toHaveBeenCalledTimes(1);
  });

  it('停止鈕經 composer：回合結束時未送達的放回輸入框、不自動送出', () => {
    useInterjectionStore.getState().add('s1', { id: 'i0', text: '補充', urgent: false, status: 'queued',
      step: null, created_at: 1 });
    const rerender = renderChatArea();
    fireEvent.click(screen.getByTitle('chat.chat_area.stop'));
    expect(chat.handleStopGeneration).toHaveBeenCalledTimes(1);
    chat.state = { ...chat.state, isSending: false };
    rerender();
    expect(chat.handleSend).not.toHaveBeenCalled();
    expect(chat.setInputValue).toHaveBeenCalledWith('改看 B\n補充');
  });
});
