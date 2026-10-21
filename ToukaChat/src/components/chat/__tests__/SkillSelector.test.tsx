import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { SkillSelector } from '../SkillSelector';
import * as connections from '@/lib/connections';
import * as ChatProviderModule from '@/lib/providers/ChatProvider';
import * as SessionListModule from '@/lib/providers/SessionListProvider';
import type { Session } from '@/lib/types';
import type { SessionChange } from '@/lib/session_persistence';

// Radix UI DropdownMenu uses a portal that doesn't render in jsdom.
// Mock it to render children inline so we can assert dropdown content directly.
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuTrigger: ({ children }: { children: React.ReactNode; asChild?: boolean }) => (
    <div data-testid="skill-dropdown-trigger">{children}</div>
  ),
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="skill-dropdown-content">{children}</div>
  ),
  DropdownMenuItem: ({
    children,
    onClick,
    className,
  }: {
    children: React.ReactNode;
    onClick?: () => void;
    className?: string;
  }) => (
    <button onClick={onClick} className={className}>
      {children}
    </button>
  ),
}));

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn(),
}));

vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));
vi.mock('@/lib/providers/ChatProvider');
vi.mock('@/lib/providers/SessionListProvider');
vi.mock('@/lib/time_utils', () => ({ getSystemEpoch: () => 1234567890 }));

const MOCK_SKILLS = [
  { id: 'skill-python', name: 'Python 專家', description: '精通 Python', required_tools: [], mcp_servers: [] },
  { id: 'skill-writer', name: '創意寫手', description: '創意寫作', required_tools: [], mcp_servers: [] },
];

const BASE_SESSION: Session = {
  id: 'session-123',
  title: '測試 Session',
  messages: {},
  updatedAt: 0,
  active_skills: [],
};

function createChatContext(activeSessionId: string): ReturnType<typeof ChatProviderModule.useChat> {
  return {
    activeSessionId,
    setActiveSessionId: vi.fn(),
    history: { messages: {}, currentId: null },
    setHistory: vi.fn(),
    isSending: false,
    isWaiting: false,
    inputValue: '',
    setInputValue: vi.fn(),
    attachment: null,
    setAttachment: vi.fn(),
    handleSend: vi.fn().mockResolvedValue(undefined),
    handleStopGeneration: vi.fn(),
    handleNewSession: vi.fn(),
    handleSwitchSession: vi.fn(),
    handleHomeSession: vi.fn(),
    handleSetHome: vi.fn().mockResolvedValue(undefined),
    handleUnsetHome: vi.fn().mockResolvedValue(undefined),
    handleDeleteMessage: vi.fn(),
    handleSwitchMessage: vi.fn(),
    handleRegenerate: vi.fn(),
    handleEditMessage: vi.fn(),
    handleUpdateSessionMessages: vi.fn().mockResolvedValue(undefined),
    handleSetProject: vi.fn().mockResolvedValue(undefined),
    authLevel: 'Manual',
    handleSetAuthLevel: vi.fn(),
    pendingInstall: null,
    setPendingInstall: vi.fn(),
    pendingAsk: null,
    setPendingAsk: vi.fn(),
    streamingSessionIds: [],
  };
}

describe('SkillSelector', () => {
  const mockSetSessions = vi.fn();
  const mockSyncSession = vi.fn();
  const mockUpdateSession = vi.fn((id: string, change: SessionChange) => {
    const previous = { ...BASE_SESSION, id };
    const next = change(previous);
    expect(change(previous)).toEqual(next);
    mockSetSessions([next]);
    return mockSyncSession(next);
  });
  const writerMethods = () => ({
    updateSession: mockUpdateSession, getSession: vi.fn(), getPersistence: vi.fn(),
    beginProducer: vi.fn(), completeProducer: vi.fn(), scopeKey: 'test',
    recheckSession: vi.fn(), retrySaveSession: vi.fn(), canRetrySave: vi.fn(), prepareResume: vi.fn(),
  });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(connections.getActiveProxyUrl).mockReturnValue('http://localhost:8000');
    vi.mocked(connections.fetchTouka).mockResolvedValue({
      ok: true,
      json: async () => ({ skills: MOCK_SKILLS }),
    } as Response);
    vi.mocked(ChatProviderModule.useChat).mockReturnValue(createChatContext('session-123'));
    vi.mocked(SessionListModule.useSessionList).mockReturnValue({
      ...writerMethods(),
      sessions: [{ ...BASE_SESSION }],
      setSessions: mockSetSessions,
      syncSession: mockSyncSession,
      refreshSessions: vi.fn().mockResolvedValue([]),
      updateSessionTitle: vi.fn().mockResolvedValue(undefined),
      deleteSession: vi.fn().mockResolvedValue(undefined),
    });
  });

  it('renders nothing when there is no active session', async () => {
    vi.mocked(ChatProviderModule.useChat).mockReturnValue(createChatContext(''));

    const { container } = render(<SkillSelector />);
    // No session → component should return null
    await waitFor(() => {
      expect(container.firstChild).toBeNull();
    });
  });

  it('renders nothing when the skills list is empty', async () => {
    vi.mocked(connections.fetchTouka).mockResolvedValue({
      ok: true,
      json: async () => ({ skills: [] }),
    } as Response);

    const { container } = render(<SkillSelector />);
    await waitFor(() => {
      expect(container.firstChild).toBeNull();
    });
  });

  it('renders the skill trigger button when skills are available', async () => {
    render(<SkillSelector />);
    await waitFor(() => {
      expect(screen.getByTitle('技能選擇')).toBeTruthy();
    });
  });

  it('shows skill names in dropdown content', async () => {
    render(<SkillSelector />);
    // With mocked DropdownMenuContent, items are always visible without clicking
    await waitFor(() => {
      expect(screen.getByText('Python 專家')).toBeTruthy();
      expect(screen.getByText('創意寫手')).toBeTruthy();
    });
  });

  it('displays the active skills count in the button label', async () => {
    vi.mocked(SessionListModule.useSessionList).mockReturnValue({
      ...writerMethods(),
      sessions: [{ ...BASE_SESSION, active_skills: ['skill-python'] }],
      setSessions: mockSetSessions,
      syncSession: mockSyncSession,
      refreshSessions: vi.fn().mockResolvedValue([]),
      updateSessionTitle: vi.fn().mockResolvedValue(undefined),
      deleteSession: vi.fn().mockResolvedValue(undefined),
    });

    render(<SkillSelector />);
    await waitFor(() => {
      expect(screen.getByText('1 技能')).toBeTruthy();
    });
  });

  it('calls setSessions and syncSession when a skill is toggled', async () => {
    render(<SkillSelector />);
    // Mocked DropdownMenuContent renders items inline — no open/close needed
    await waitFor(() => screen.getByText('Python 專家'));
    fireEvent.click(screen.getByText('Python 專家'));
    expect(mockSetSessions).toHaveBeenCalledTimes(1);
  });

  it('replaying the state updater does not repeat the same skill save', async () => {
    mockSetSessions.mockImplementation(action => {
      if (typeof action === 'function') { action([BASE_SESSION]); action([BASE_SESSION]); }
    });
    render(<SkillSelector />);
    await waitFor(() => screen.getByText('Python 專家'));
    fireEvent.click(screen.getByText('Python 專家'));
    expect(mockSyncSession).toHaveBeenCalledTimes(1);
  });

  it('uses fetchTouka (FETCH_SSOT) and not raw fetch', async () => {
    render(<SkillSelector />);
    await waitFor(() => screen.getByTitle('技能選擇'));
    // fetchTouka must have been called for skills/list（P4/ADR-0161：排除 is_system 系統技能）
    expect(connections.fetchTouka).toHaveBeenCalledWith(
      'http://localhost:8000',
      '/v1/skills/list?include_system=false',
    );
  });
});
