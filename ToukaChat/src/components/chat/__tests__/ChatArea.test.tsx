/**
 * ChatArea 互動測試（四功能區調查 P4／ADR-0161）。
 *
 * 病史：輸入列六控制項的宿主 ChatArea.tsx 零測試＝「這區常在修 bug/重構後壞掉」的
 * 結構性原因。本檔補六控制項的關鍵互動＋回歸鎖：
 * - 研究鈕 deepResearch toggle **純記憶體**（P4-1 移死碼後不得再依賴 syncSession——
 *   mock 的 useSessionList 刻意不提供 syncSession，若 ChatArea 還呼叫即 crash）。
 * - MCP 動態掛載 toggle（ADR-0161 新增）翻轉 mcpDispatcher。
 * - 截圖鈕呼叫 handleScreenshot prop；模型選單/上傳鈕渲染。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

const translation = vi.hoisted(() => ({ current: (key: string) => key }));
vi.mock('react-i18next', async (importOriginal) => ({
  ...await importOriginal<typeof import('react-i18next')>(),
  useTranslation: () => ({ t: translation.current }),
  Trans: ({ children }: { children?: React.ReactNode }) => children ?? null,
}));
vi.mock('react-virtuoso', async () => {
  const react = await import('react');
  const Virtuoso = react.forwardRef(() => null);
  Virtuoso.displayName = 'Virtuoso';
  return { Virtuoso };
});

const setSessions = vi.fn();
const setSelectedModel = vi.fn();
const availableModels = [{ id: 'm1', name: 'M1', status: 'online' }];

vi.mock('@/lib/ThemeContext', () => ({ useTheme: () => ({ density: 'comfortable' }) }));
vi.mock('@/lib/providers/ChatProvider', () => ({
  useChat: () => ({
    history: [], inputValue: '', setInputValue: vi.fn(),
    attachment: null, setAttachment: vi.fn(), isSending: false,
    handleSend: vi.fn(), handleStopGeneration: vi.fn(),
    handleDeleteMessage: vi.fn(), handleSwitchMessage: vi.fn(),
    handleRegenerate: vi.fn(), handleEditMessage: vi.fn(),
    activeSessionId: 's1',
  }),
}));
vi.mock('@/lib/providers/ModelProvider', () => ({
  useModels: () => ({
    availableModels,
    selectedModel: 'm1', setSelectedModel,
  }),
}));
// 刻意不提供 syncSession——回歸鎖：deepResearch/mcpDispatcher toggle 不得依賴它。
vi.mock('@/lib/providers/SessionListProvider', () => ({ useSessionList: vi.fn() }));
vi.mock('@/lib/providers/ProjectProvider', () => ({ useProjects: () => ({ projects: [] }) }));
vi.mock('@/hooks/useFeedback', () => ({
  useFeedback: () => ({ feedbackState: {}, submit: vi.fn() }),
}));
// 子元件 stub（各有自己的 provider 依賴，隔離之）。
vi.mock('../AuthSwitcher', () => ({ AuthSwitcher: () => <div data-testid="auth-switcher" /> }));
vi.mock('../SkillSelector', () => ({ SkillSelector: () => <div data-testid="skill-selector" /> }));
vi.mock('../ContextMeter', () => ({ ContextMeter: () => null }));
vi.mock('../SessionPersistenceNotice', () => ({ SessionPersistenceNotice: () => null }));
vi.mock('../MessageItem', () => ({ MessageItem: () => null }));
vi.mock('../ChatWelcome', () => ({ ChatWelcome: () => null }));
vi.mock('../FeedbackReasonDialog', () => ({ FeedbackReasonDialog: () => null }));
vi.mock('@/components/PlanTracker', () => ({ PlanTracker: () => null }));
vi.mock('@/components/ui/MentionInput', () => ({
  MentionInput: () => <textarea data-testid="mention-input" />,
}));
vi.mock('@/components/ui/select', () => ({
  Select: ({ onValueChange, children }: { onValueChange: (id: string) => void; children: React.ReactNode }) => (
    <div><button data-testid="model-select" onClick={() => onValueChange('m1')}>M1</button>{children}</div>
  ),
  SelectContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SelectItem: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SelectTrigger: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SelectValue: () => null,
}));
vi.mock('@/lib/connections', () => ({ getActiveProxyUrl: () => 'http://x', fetchTouka: vi.fn() }));
// ADR-0249：輸入框的插話行為會讀目前串流（未在生成時為 undefined）
vi.mock('@/lib/providers/StreamRegistryProvider', () => ({ useStreamRegistry: () => ({ getStream: () => undefined }) }));
vi.mock('@/lib/bridge', () => ({ bridgeApplyArtifact: vi.fn() }));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));
vi.mock('sonner', () => ({ toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() } }));

import { ChatArea } from '../ChatArea';
import { useSessionList } from '@/lib/providers/SessionListProvider';
import { toast } from 'sonner';

function renderChatArea(overrides: Partial<Record<string, unknown>> = {}) {
  vi.mocked(useSessionList).mockReturnValue({
    sessions: [{ id: 's1', deepResearch: false, mcpDispatcher: false, ...overrides }],
    setSessions,
  } as unknown as ReturnType<typeof useSessionList>);
  const handleScreenshot = vi.fn();
  const fileInputRef = React.createRef<HTMLInputElement>();
  const view = () => (
    <ChatArea
      isIDE={false}
      fileInputRef={fileInputRef}
      handleScreenshot={handleScreenshot}
      editingMessageId={null}
      setEditingMessageId={vi.fn()}
      editValue=""
      setEditValue={vi.fn()}
    />
  );
  const { rerender } = render(view());
  return { handleScreenshot, rerender: () => rerender(view()) };
}

describe('ChatArea 輸入列六控制項（P4/ADR-0161）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    translation.current = (key: string) => key;
  });

  it('P11 切換語言後，模型切換提示使用新翻譯', () => {
    translation.current = (key: string) => `before:${key}`;
    const { rerender } = renderChatArea();
    fireEvent.click(screen.getByTestId('model-select'));
    expect(toast.info).toHaveBeenLastCalledWith('before:chat.chat_area.model_switched_style_may_change');

    // 模型清單與 setter 保持同一參照，只替換翻譯函式。
    translation.current = (key: string) => `after:${key}`;
    rerender();
    fireEvent.click(screen.getByTestId('model-select'));
    expect(toast.info).toHaveBeenLastCalledWith('after:chat.chat_area.model_switched_style_may_change');
    expect(setSelectedModel).toHaveBeenLastCalledWith('m1');
  });

  it('六控制項皆渲染（上傳/截圖/HITL/技能/研究/MCP/模型）', () => {
    renderChatArea();
    expect(screen.getByTitle('chat.chat_area.upload_file')).toBeDefined();
    expect(screen.getByTitle('chat.chat_area.screenshot')).toBeDefined();
    expect(screen.getByTestId('auth-switcher')).toBeDefined();
    expect(screen.getByTestId('skill-selector')).toBeDefined();
    expect(screen.getByTitle('chat.chat_area.deep_research_tooltip')).toBeDefined();
    expect(screen.getByTitle('chat.chat_area.mcp_dispatcher_tooltip')).toBeDefined();
  });

  it('回歸鎖：研究鈕 toggle 純記憶體翻轉 deepResearch（不依賴 syncSession）', () => {
    renderChatArea();
    fireEvent.click(screen.getByTitle('chat.chat_area.deep_research_tooltip'));
    expect(setSessions).toHaveBeenCalledTimes(1);
    // 驗 updater：把當前 session 的 deepResearch 由 false 翻成 true
    const updater = setSessions.mock.calls[0][0] as (p: Record<string, unknown>[]) => Record<string, unknown>[];
    const next = updater([{ id: 's1', deepResearch: false }]);
    expect(next[0].deepResearch).toBe(true);
  });

  it('MCP 動態掛載 toggle 翻轉 mcpDispatcher', () => {
    renderChatArea();
    fireEvent.click(screen.getByTitle('chat.chat_area.mcp_dispatcher_tooltip'));
    const updater = setSessions.mock.calls[0][0] as (p: Record<string, unknown>[]) => Record<string, unknown>[];
    const next = updater([{ id: 's1', mcpDispatcher: false }]);
    expect(next[0].mcpDispatcher).toBe(true);
  });

  it('研究鈕 aria-pressed 反映當前 deepResearch 狀態', () => {
    renderChatArea({ deepResearch: true });
    expect(screen.getByTitle('chat.chat_area.deep_research_tooltip').getAttribute('aria-pressed')).toBe('true');
  });

  it('截圖鈕呼叫 handleScreenshot prop', () => {
    const { handleScreenshot } = renderChatArea();
    fireEvent.click(screen.getByTitle('chat.chat_area.screenshot'));
    expect(handleScreenshot).toHaveBeenCalledTimes(1);
  });
});
