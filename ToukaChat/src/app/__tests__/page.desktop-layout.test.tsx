/**
 * page.tsx — 桌面版版面（上導覽列拿掉後的接線）
 *
 * 1. 主區沒有上導覽列（沒有 header、h1）。
 * 2. 左側邊欄收到 onCollapse，按了面板收起；收起後主區左上角的展開鈕把面板展開。
 *    面板已經展開、但記住的狀態是收起（兩邊沒同步）時，按展開鈕直接把狀態改回展開。
 * 3. 右上角工具面板鈕：預設收起；按了打開右側邊欄；有新產出物、模擬模式照舊自動打開。
 * 4. IDE：設定鈕進設定頁，再按一次回對話。
 *
 * react-resizable-panels 換成假的 Panel：記下 collapse／expand，並像真的一樣呼叫 onCollapse／onExpand。
 * useLocalStorage 與 useArtifacts 用真的（jsdom localStorage、zustand store），狀態變化才是真的。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import React from 'react';

type PanelEntry = { collapsed: boolean; collapse: ReturnType<typeof vi.fn>; expand: ReturnType<typeof vi.fn> };

const { panels, leftSidebarProps, ideMode, simulationMode } = vi.hoisted(() => ({
    panels: {} as Record<string, PanelEntry>,
    leftSidebarProps: { current: null as null | Record<string, unknown> },
    ideMode: { current: false },
    simulationMode: { current: false },
}));

vi.mock('react-resizable-panels', async () => {
    const R = await import('react');
    type PanelProps = {
        id: string;
        children?: React.ReactNode;
        onCollapse?: () => void;
        onExpand?: () => void;
    };
    const Panel = R.forwardRef<unknown, PanelProps>(function Panel({ id, children, onCollapse, onExpand }, ref) {
        // 和真的套件一樣：狀態真的改變才呼叫 onCollapse／onExpand。
        R.useImperativeHandle(ref, () => ({
            collapse: () => {
                panels[id].collapse();
                if (!panels[id].collapsed) { panels[id].collapsed = true; onCollapse?.(); }
            },
            expand: () => {
                panels[id].expand();
                if (panels[id].collapsed) { panels[id].collapsed = false; onExpand?.(); }
            },
            isCollapsed: () => panels[id].collapsed,
        }));
        return <div id={id}>{children}</div>;
    });
    return {
        Panel,
        PanelGroup: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
        PanelResizeHandle: () => <div />,
    };
});

vi.mock('@/hooks/useMediaQuery', () => ({ useMediaQuery: vi.fn().mockReturnValue(false) }));
vi.mock('@/lib/providers/ChatProvider', () => ({
    useChat: () => ({ activeSessionId: 'test', setAttachment: vi.fn(), handleNewSession: vi.fn() }),
}));
vi.mock('@/lib/providers/ModelProvider', () => ({ useModels: () => ({ selectedModel: null }) }));
vi.mock('@/lib/providers/DebugProvider', () => ({ useDebug: () => ({ isSimulationMode: simulationMode.current }) }));
vi.mock('@/lib/bridge', () => ({ postToBridge: vi.fn(), isIDE: () => ideMode.current }));

vi.mock('@/components/LeftSidebar', () => ({
    default: (props: Record<string, unknown>) => {
        leftSidebarProps.current = props;
        return <div data-testid="left-sidebar" />;
    },
}));
vi.mock('@/components/RightSidebar', () => ({ default: () => <div data-testid="right-sidebar" /> }));
vi.mock('@/components/chat/ChatArea', () => ({ ChatArea: () => <div data-testid="chat-area" /> }));
vi.mock('@/components/layout/SoulStudio', () => ({ SoulStudio: () => <div /> }));
vi.mock('@/components/layout/ModelStudio', () => ({ ModelStudio: () => <div /> }));
vi.mock('@/components/dashboard/PulseDashboard', () => ({ PulseDashboard: () => <div /> }));
vi.mock('@/components/dashboard/VaultDashboard', () => ({ VaultDashboard: () => <div /> }));
vi.mock('@/components/settings/SettingsView', () => ({ SettingsView: () => <div data-testid="settings-view" /> }));
vi.mock('@/components/projects/ProjectsView', () => ({ ProjectsView: () => <div /> }));

import ChatPage from '../page';
import { useArtifacts } from '@/hooks/useArtifacts';

const LEFT = 'left-sidebar-panel';
const RIGHT = 'right-sidebar-panel';

function resetPanels(leftCollapsed: boolean) {
    panels[LEFT] = { collapsed: leftCollapsed, collapse: vi.fn(), expand: vi.fn() };
    panels[RIGHT] = { collapsed: true, collapse: vi.fn(), expand: vi.fn() };
}

function renderPage({ storedCollapsed = false, panelCollapsed = storedCollapsed } = {}) {
    window.localStorage.setItem('touka_sidebar_collapsed', JSON.stringify(storedCollapsed));
    resetPanels(panelCollapsed);
    return render(<ChatPage />);
}

beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    leftSidebarProps.current = null;
    ideMode.current = false;
    simulationMode.current = false;
    useArtifacts.setState({ artifacts: [], isOpen: false, isManuallyClosed: false, activeTab: 'system' });
});

describe('page.tsx 桌面版 — 沒有上導覽列', () => {
    it('主區裡沒有 header 與 h1', () => {
        const { container } = renderPage();
        const main = container.querySelector('main') as HTMLElement;
        expect(main).not.toBeNull();
        expect(main.querySelector('header')).toBeNull();
        expect(main.querySelector('h1')).toBeNull();
        expect(within(main).getByTestId('chat-area')).toBeInTheDocument();
    });
});

describe('page.tsx 桌面版 — 左側邊欄收合與展開', () => {
    it('左側邊欄收到 onCollapse，呼叫後面板收起、主區出現展開鈕', () => {
        renderPage();
        expect(screen.queryByRole('button', { name: '展開側邊欄' })).toBeNull();
        const onCollapse = leftSidebarProps.current?.onCollapse;
        expect(typeof onCollapse).toBe('function');
        act(() => (onCollapse as () => void)());
        expect(panels[LEFT].collapse).toHaveBeenCalledTimes(1);
        expect(screen.getByRole('button', { name: '展開側邊欄' })).toBeInTheDocument();
        expect(leftSidebarProps.current?.isCollapsed).toBe(true);
    });

    it('收起狀態時按展開鈕，面板展開、展開鈕消失', () => {
        renderPage({ storedCollapsed: true });
        fireEvent.click(screen.getByRole('button', { name: '展開側邊欄' }));
        expect(panels[LEFT].expand).toHaveBeenCalledTimes(1);
        expect(screen.queryByRole('button', { name: '展開側邊欄' })).toBeNull();
        expect(leftSidebarProps.current?.isCollapsed).toBe(false);
    });

    it('面板已經展開、但記住的狀態是收起時，按展開鈕直接把狀態改回展開', () => {
        renderPage({ storedCollapsed: true, panelCollapsed: false });
        fireEvent.click(screen.getByRole('button', { name: '展開側邊欄' }));
        expect(panels[LEFT].expand).not.toHaveBeenCalled();
        expect(screen.queryByRole('button', { name: '展開側邊欄' })).toBeNull();
        expect(leftSidebarProps.current?.isCollapsed).toBe(false);
        expect(window.localStorage.getItem('touka_sidebar_collapsed')).toBe('false');
    });
});

describe('page.tsx 桌面版 — 右上角工具面板鈕', () => {
    it('預設收起；按了打開右側邊欄', () => {
        renderPage();
        const button = screen.getByRole('button', { name: '工具面板' });
        expect(button).toHaveAttribute('aria-expanded', 'false');
        fireEvent.click(button);
        expect(useArtifacts.getState().isOpen).toBe(true);
        expect(panels[RIGHT].expand).toHaveBeenCalled();
        expect(screen.getByRole('button', { name: '工具面板' })).toHaveAttribute('aria-expanded', 'true');
    });

    it('有新產出物時照舊自動打開，按鈕跟著顯示打開', () => {
        renderPage();
        act(() => {
            useArtifacts.getState().addArtifact({ title: 'a', type: 'markdown', content: '# a' });
        });
        expect(panels[RIGHT].expand).toHaveBeenCalled();
        expect(screen.getByRole('button', { name: '工具面板' })).toHaveAttribute('aria-expanded', 'true');
    });

    it('模擬模式照舊打開右側邊欄並切到除錯分頁', () => {
        simulationMode.current = true;
        renderPage();
        expect(useArtifacts.getState().isOpen).toBe(true);
        expect(useArtifacts.getState().activeTab).toBe('debug');
        expect(panels[RIGHT].expand).toHaveBeenCalled();
    });
});

describe('page.tsx IDE 模式 — 只有設定鈕', () => {
    it('按設定鈕進設定頁，再按一次回對話', () => {
        ideMode.current = true;
        renderPage();
        expect(screen.queryByRole('button', { name: '工具面板' })).toBeNull();
        expect(screen.queryByTestId('left-sidebar')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: '設定' }));
        expect(screen.getByTestId('settings-view')).toBeInTheDocument();
        expect(screen.queryByTestId('chat-area')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: '設定' }));
        expect(screen.getByTestId('chat-area')).toBeInTheDocument();
    });
});
