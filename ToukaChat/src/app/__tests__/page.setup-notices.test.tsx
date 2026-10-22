/**
 * page.tsx — 左側邊欄首次設定警告的手機版接線
 *
 * 1. 手機版左側欄在 modal Sheet 裡；按側欄警告的按鈕打開精靈時，必須先關掉 Sheet，
 *    否則 Sheet 會擋住精靈的點擊與焦點。LeftSidebar 換成記下 props 的假元件，直接呼叫
 *    page.tsx 傳進來的 onSetupAction，確認它把側欄收起來。
 * 2. 選單沒打開時，選單按鈕上有紅點、按鈕名稱說明側欄裡有設定警告；沒有警告時兩者都沒有。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import React from 'react';
import { SetupNoticesProvider } from '@/components/setup/SetupNoticesProvider';

const { leftSidebarProps, setSidebarCollapsed, mockUseLocalStorage } = vi.hoisted(() => ({
    leftSidebarProps: { current: null as null | Record<string, unknown> },
    setSidebarCollapsed: vi.fn(),
    mockUseLocalStorage: vi.fn(),
}));

vi.mock('@/contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('@/hooks/useSetupStatus', () => ({ useSetupStatus: vi.fn() }));

vi.mock('@/lib/SystemStatusContext', () => ({
    useSystemStatus: () => ({
        status: { isConnected: false, isMemoryActive: false, latency: '0ms', mode: 'standalone', lastChecked: '' },
        checkStatus: vi.fn(),
    }),
}));

// isMobile = true → 手機版分支（左側欄在 Sheet 裡）
vi.mock('@/hooks/useMediaQuery', () => ({ useMediaQuery: vi.fn().mockReturnValue(true) }));

vi.mock('@/hooks/useArtifacts', () => ({
    useArtifacts: () => ({
        artifacts: [], isOpen: false, setIsOpen: vi.fn(), setActiveTab: vi.fn(),
        addArtifact: vi.fn(), clearArtifacts: vi.fn(),
    }),
}));

vi.mock('@/hooks/useLocalStorage', () => ({
    getLocalStorageItem: vi.fn().mockReturnValue(null),
    useLocalStorage: mockUseLocalStorage,
}));

vi.mock('@/lib/providers/ChatProvider', () => ({
    useChat: () => ({ activeSessionId: 'test', setAttachment: vi.fn(), handleNewSession: vi.fn() }),
}));
vi.mock('@/lib/providers/ModelProvider', () => ({ useModels: () => ({ selectedModel: null }) }));
vi.mock('@/lib/providers/DebugProvider', () => ({ useDebug: () => ({ isSimulationMode: false }) }));
vi.mock('@/lib/bridge', () => ({ postToBridge: vi.fn(), isIDE: vi.fn().mockReturnValue(false) }));

vi.mock('@/components/LeftSidebar', () => ({
    default: (props: Record<string, unknown>) => {
        leftSidebarProps.current = props;
        return <div data-testid="left-sidebar">LeftSidebar</div>;
    },
}));
vi.mock('@/components/RightSidebar', () => ({ default: () => <div data-testid="right-sidebar" /> }));
vi.mock('@/components/chat/ChatArea', () => ({ ChatArea: () => <div data-testid="chat-area" /> }));
vi.mock('@/components/layout/MainHeader', () => ({ MainHeader: () => <div data-testid="main-header" /> }));
vi.mock('@/components/layout/SoulStudio', () => ({ SoulStudio: () => <div /> }));
vi.mock('@/components/layout/ModelStudio', () => ({ ModelStudio: () => <div /> }));
vi.mock('@/components/dashboard/PulseDashboard', () => ({ PulseDashboard: () => <div /> }));
vi.mock('@/components/dashboard/VaultDashboard', () => ({ VaultDashboard: () => <div /> }));
vi.mock('@/components/settings/SettingsView', () => ({ SettingsView: () => <div /> }));
vi.mock('@/components/projects/ProjectsView', () => ({ ProjectsView: () => <div /> }));

import ChatPage from '../page';
import { useAuth } from '@/contexts/AuthContext';
import { useSetupStatus, type SetupStatus } from '@/hooks/useSetupStatus';

function setupStatus(isTempPassword: boolean): SetupStatus {
    return {
        dispatch_token_is_default: false,
        auth_secret_persisted: true,
        api_keys_configured: ['anthropic'],
        is_admin_temp_password: isTempPassword,
        data_path_is_default: false,
        workspace_root_is_default: false,
        embedding_model: 'all-MiniLM-L6-v2',
    };
}

function renderPage({ collapsed, warning }: { collapsed: boolean; warning: boolean }) {
    mockUseLocalStorage.mockReturnValue([collapsed, setSidebarCollapsed]);
    vi.mocked(useAuth).mockReturnValue({
        user: { role: 'admin', email: 'admin@test.com', id: '1', status: 'active' },
    } as unknown as ReturnType<typeof useAuth>);
    vi.mocked(useSetupStatus).mockReturnValue({ status: setupStatus(warning), isLoading: false, refetch: vi.fn() });
    return render(<SetupNoticesProvider><ChatPage /></SetupNoticesProvider>);
}

describe('page.tsx — 左側邊欄首次設定警告（手機版）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        leftSidebarProps.current = null;
    });

    it('手機版左側欄按了警告按鈕後會收起選單（讓精靈不被 Sheet 擋住）', () => {
        renderPage({ collapsed: false, warning: true });
        const onSetupAction = leftSidebarProps.current?.onSetupAction;
        expect(typeof onSetupAction).toBe('function');
        act(() => (onSetupAction as () => void)());
        expect(setSidebarCollapsed).toHaveBeenCalledWith(true);
    });

    it('選單沒打開且有警告時，選單按鈕上有紅點，按鈕名稱說明有設定警告', () => {
        renderPage({ collapsed: true, warning: true });
        const menuButton = screen.getByRole('button', { name: /設定警告/ });
        expect(menuButton.querySelector('[data-testid="setup-notice-dot"]')).not.toBeNull();
    });

    it('沒有警告時選單按鈕沒有紅點', () => {
        renderPage({ collapsed: true, warning: false });
        expect(screen.queryByTestId('setup-notice-dot')).toBeNull();
        expect(screen.queryByRole('button', { name: /設定警告/ })).toBeNull();
    });
});
