/**
 * ChatArea 互動測試（四功能區調查 P4／ADR-0161）。
 *
 * 病史：輸入列六控制項的宿主 ChatArea.tsx 零測試＝「這區常在修 bug/重構後壞掉」的
 * 結構性原因。本檔補六控制項的關鍵互動＋回歸鎖：
 * - 研究鈕 deepResearch toggle **純記憶體**（P4-1 移死碼後不得再依賴 syncSession——
 *   mock 的 useSessionList 刻意不提供 syncSession，若 ChatArea 還呼叫即 crash）。
 * - MCP 動態掛載 toggle（ADR-0161 新增）翻轉 mcpDispatcher。
 * - 截圖鈕呼叫 handleScreenshot prop；模型選單/上傳鈕渲染。
 * - 對話左側的訊息導覽列：顯示條件、點擊跳轉、捲動範圍標亮。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';

const translation = vi.hoisted(() => ({ current: (key: string) => key }));
vi.mock('react-i18next', async (importOriginal) => ({
  ...await importOriginal<typeof import('react-i18next')>(),
  useTranslation: () => ({ t: translation.current }),
  Trans: ({ children }: { children?: React.ReactNode }) => children ?? null,
}));
// Virtuoso 替身：不畫訊息，只記下 props 並提供 scrollToIndex
const virtuoso = vi.hoisted(() => ({
  props: {} as { rangeChanged?: (range: { startIndex: number; endIndex: number }) => void },
  scrollToIndex: vi.fn(),
}));
vi.mock('react-virtuoso', async () => {
  const react = await import('react');
  const Virtuoso = react.forwardRef((props: typeof virtuoso.props, ref) => {
    react.useImperativeHandle(ref, () => ({ scrollToIndex: virtuoso.scrollToIndex }));
    virtuoso.props = props;
    return null;
  });
  Virtuoso.displayName = 'Virtuoso';
  return { Virtuoso };
});
const chat = vi.hoisted(() => ({ history: [] as unknown }));
const media = vi.hoisted(() => ({ mobile: false }));
vi.mock('@/hooks/useMediaQuery', () => ({ useMediaQuery: () => media.mobile }));

const setSessions = vi.fn();
const setSelectedModel = vi.fn();
const availableModels = [{ id: 'm1', name: 'M1', status: 'online' }];

vi.mock('@/lib/ThemeContext', () => ({ useTheme: () => ({ density: 'comfortable' }) }));
vi.mock('@/lib/providers/ChatProvider', () => ({
  useChat: () => ({
    history: chat.history, inputValue: '', setInputValue: vi.fn(),
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

function renderChatArea(overrides: Partial<Record<string, unknown>> = {}, { isIDE = false } = {}) {
  vi.mocked(useSessionList).mockReturnValue({
    sessions: [{ id: 's1', deepResearch: false, mcpDispatcher: false, ...overrides }],
    setSessions,
  } as unknown as ReturnType<typeof useSessionList>);
  const handleScreenshot = vi.fn();
  const fileInputRef = React.createRef<HTMLInputElement>();
  const view = () => (
    <ChatArea
      isIDE={isIDE}
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

/** 線性路徑 u1 → a1 → u2 → a2 …（userCount 則使用者訊息，各接一則回覆）。 */
function linearHistory(userCount: number) {
  const ids = Array.from({ length: userCount * 2 }, (_, i) => (i % 2 === 0 ? `u${i / 2 + 1}` : `a${(i + 1) / 2}`));
  const messages = Object.fromEntries(ids.map((id, i) => [id, {
    id, role: i % 2 === 0 ? 'user' : 'assistant', content: id,
    parentId: i === 0 ? null : ids[i - 1], childrenIds: i + 1 < ids.length ? [ids[i + 1]] : [],
  }]));
  return { messages, currentId: ids[ids.length - 1] };
}

describe('ChatArea 輸入列六控制項（P4/ADR-0161）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    translation.current = (key: string) => key;
    chat.history = [];
    media.mobile = false;
    virtuoso.props = {};
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

describe('ChatArea 對話左側的訊息導覽列', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    translation.current = (key: string) => key;
    media.mobile = false;
    virtuoso.props = {};
  });

  const navButtons = () => screen.getAllByRole('button', { name: 'chat.message_nav.jump_to' });

  it('兩條以上才顯示，一條時不顯示但照收捲動範圍', () => {
    chat.history = linearHistory(2);
    const { rerender } = renderChatArea();
    expect(screen.getByRole('navigation', { name: 'chat.message_nav.label' })).toBeDefined();
    expect(navButtons()).toHaveLength(2);

    chat.history = linearHistory(1);
    rerender();
    expect(screen.queryByRole('navigation', { name: 'chat.message_nav.label' })).toBeNull();
    expect(virtuoso.props.rangeChanged).toBeTypeOf('function');
  });

  it.each([
    ['IDE', { isIDE: true }, false],
    ['手機版', { isIDE: false }, true],
  ])('%s 不顯示', (_name, options, mobile) => {
    media.mobile = mobile;
    chat.history = linearHistory(3);
    renderChatArea({}, options);
    expect(screen.queryByRole('navigation', { name: 'chat.message_nav.label' })).toBeNull();
  });

  it('點一條就捲到那則訊息的開頭', () => {
    chat.history = linearHistory(3);
    renderChatArea();
    fireEvent.click(navButtons()[1]);
    expect(virtuoso.scrollToIndex).toHaveBeenCalledWith(expect.objectContaining({ index: 2, align: 'start' }));
  });

  it('捲動範圍改變時標亮目前那則', () => {
    chat.history = linearHistory(3);
    renderChatArea();
    act(() => virtuoso.props.rangeChanged?.({ startIndex: 3, endIndex: 4 }));
    expect(navButtons().map(b => b.getAttribute('aria-current'))).toEqual([null, null, 'true']);
  });

  it('依訊息捲動區的高度決定線數', () => {
    // jsdom 沒有 ResizeObserver：記下觀察對象，由測試觸發高度
    const observed: { target: Element; resize: (height: number) => void }[] = [];
    vi.stubGlobal('ResizeObserver', class {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe(target: Element) {
        observed.push({ target, resize: height => this.callback(
          [{ contentRect: { height } } as ResizeObserverEntry], this as unknown as ResizeObserver) });
      }
      unobserve() {}
      disconnect() {}
    });
    try {
      chat.history = linearHistory(5);
      renderChatArea();
      const nav = screen.getByRole('navigation', { name: 'chat.message_nav.label' });
      expect(observed.map(o => o.target)).toEqual([nav.parentElement]);
      act(() => observed[0].resize(16)); // 只排得下最少的 2 條
      expect(screen.getAllByRole('button', { name: /^chat\.message_nav\.jump_to/ })).toHaveLength(2);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
