/**
 * page.tsx — SheetContent 可及性標題測試
 *
 * Radix Dialog（SheetContent 底層）要求每個 DialogContent 必須含有
 * DialogTitle，否則對螢幕閱讀器不可及，並在 console.error 輸出警告。
 *
 * 本測試驗證：
 * 1. 左側 Sidebar Sheet 開啟時，dialog 含有 accessible name「導覽選單」。
 * 2. 右側 Sidebar Sheet 開啟時，dialog 含有 accessible name「工具面板」。
 * 3. 兩種 Sheet 開啟時均不觸發 Radix DialogTitle 可及性 console.error。
 *
 * 測試強制 useMediaQuery 返回 true（行動裝置模式），因為 SheetContent
 * 只在 isMobile 路徑下渲染。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';

// ── vi.hoisted：在 vi.mock 工廠內部使用可控的 mock 函數 ────────────────────
const { mockUseArtifacts } = vi.hoisted(() => ({
    mockUseArtifacts: vi.fn(),
}));

// ── 全域依賴 mock ──────────────────────────────────────────────────────────

vi.mock('@/lib/SystemStatusContext', () => ({
    useSystemStatus: () => ({
        status: {
            isConnected: false, isMemoryActive: false,
            latency: '0ms', mode: 'standalone', lastChecked: '',
        },
        checkStatus: vi.fn(),
    }),
    SystemStatusProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

// isMobile = true → 渲染 Mobile 分支（含 SheetContent）
vi.mock('@/hooks/useMediaQuery', () => ({
    useMediaQuery: vi.fn().mockReturnValue(true),
}));

vi.mock('@/hooks/useArtifacts', () => ({
    useArtifacts: mockUseArtifacts,
}));

vi.mock('@/hooks/useLocalStorage', () => ({
    getLocalStorageItem: vi.fn().mockReturnValue(null),
    useLocalStorage: vi.fn().mockReturnValue([false, vi.fn()]),
}));

vi.mock('@/lib/providers/ChatProvider', () => ({
    useChat: () => ({
        messages: [], isStreaming: false, sessionId: 'test',
        setIsStreaming: vi.fn(), addMessage: vi.fn(), clearMessages: vi.fn(), setSessionId: vi.fn(),
    }),
}));

vi.mock('@/lib/providers/ModelProvider', () => ({
    useModels: () => ({ currentModel: null, models: [], setCurrentModel: vi.fn() }),
}));

vi.mock('@/lib/providers/DebugProvider', () => ({
    useDebug: () => ({ debugMode: false, setDebugMode: vi.fn() }),
}));

vi.mock('@/lib/bridge', () => ({
    postToBridge: vi.fn(),
    isIDE: vi.fn().mockReturnValue(false),
}));

// Stub 重型子元件
vi.mock('@/components/LeftSidebar', () => ({
    default: () => <div data-testid="left-sidebar">LeftSidebar</div>,
}));

vi.mock('@/components/RightSidebar', () => ({
    default: () => <div data-testid="right-sidebar">RightSidebar</div>,
}));

vi.mock('@/components/chat/ChatArea', () => ({
    ChatArea: () => <div data-testid="chat-area">ChatArea</div>,
}));

vi.mock('@/components/layout/MainHeader', () => ({
    MainHeader: () => <div data-testid="main-header">MainHeader</div>,
}));

vi.mock('@/components/layout/SoulStudio', () => ({
    SoulStudio: () => <div>SoulStudio</div>,
}));

vi.mock('@/components/layout/ModelStudio', () => ({
    ModelStudio: () => <div>ModelStudio</div>,
}));

vi.mock('@/components/dashboard/PulseDashboard', () => ({
    PulseDashboard: () => <div>PulseDashboard</div>,
}));

vi.mock('@/components/dashboard/VaultDashboard', () => ({
    VaultDashboard: () => <div>VaultDashboard</div>,
}));

vi.mock('@/components/settings/SettingsView', () => ({
    SettingsView: () => <div>SettingsView</div>,
}));

vi.mock('@/components/projects/ProjectsView', () => ({
    ProjectsView: () => <div>ProjectsView</div>,
}));

vi.mock('react-resizable-panels', () => ({
    Panel: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    PanelGroup: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    PanelResizeHandle: () => <div />,
    ImperativePanelHandle: class {},
}));

vi.mock('@/lib/utils', () => ({
    cn: (...classes: (string | undefined | false)[]) => classes.filter(Boolean).join(' '),
}));

import ChatPage from '../page';

// ── 共用 mock 回傳值 ──────────────────────────────────────────────────────

const makeArtifactsReturn = (isOpen: boolean) => ({
    artifacts: [],
    isOpen,
    setIsOpen: vi.fn(),
    setActiveTab: vi.fn(),
    addArtifact: vi.fn(),
    clearArtifacts: vi.fn(),
});

// ── 測試 ──────────────────────────────────────────────────────────────────

describe('page.tsx — SheetContent 可及性標題（a11y）', () => {
    let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
        // 攔截 console.error 以偵測 Radix UI 可及性警告
        consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        consoleErrorSpy.mockRestore();
        vi.clearAllMocks();
    });

    it('左側 Sidebar Sheet（isSidebarCollapsed=false 預設開啟）含有 accessible name「導覽選單」', async () => {
        // 右側 Sidebar 關閉，左側因 isSidebarCollapsed=false 預設開啟
        mockUseArtifacts.mockReturnValue(makeArtifactsReturn(false));

        render(<ChatPage />);

        // isSidebarCollapsed 預設為 false → Sheet open={true}，SheetContent 即時掛載
        // SheetTitle 以 sr-only 渲染（視覺隱藏但 DOM 存在）
        await waitFor(() => {
            expect(
                screen.getByText('導覽選單', { selector: '*' }),
            ).toBeDefined();
        });

        // 不應觸發 DialogTitle 可及性警告
        const a11yErrors = consoleErrorSpy.mock.calls.filter(
            (args: unknown[]) =>
                typeof args[0] === 'string' && (args[0] as string).includes('DialogTitle'),
        );
        expect(a11yErrors).toHaveLength(0);
    });

    it('右側 Sidebar Sheet（isOpen=true）含有 accessible name「工具面板」', async () => {
        // 右側 Sidebar 預設開啟
        mockUseArtifacts.mockReturnValue(makeArtifactsReturn(true));

        render(<ChatPage />);

        await waitFor(() => {
            expect(
                screen.getByText('工具面板', { selector: '*' }),
            ).toBeDefined();
        });

        const a11yErrors = consoleErrorSpy.mock.calls.filter(
            (args: unknown[]) =>
                typeof args[0] === 'string' && (args[0] as string).includes('DialogTitle'),
        );
        expect(a11yErrors).toHaveLength(0);
    });
});
