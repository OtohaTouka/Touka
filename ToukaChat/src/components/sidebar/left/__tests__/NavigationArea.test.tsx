/**
 * NavigationArea：左側邊欄底部的選單（2026-11-05 起其他選項都收在這裡）。
 *
 * 登出的 Bug 修復：handleLogout 必須呼叫 useAuth().logout()，
 * 而非直接呼叫 connections.logoutTouka()，以確保
 * AuthContext 的 React state 正確清空。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, fireEvent, screen, createEvent } from '@testing-library/react';
import React from 'react';

// vi.mock() calls are hoisted — use vi.hoisted() to ensure variables
// are available when the mock factory runs.
const mockLogout = vi.hoisted(() => vi.fn());
const mockLogoutTouka = vi.hoisted(() => vi.fn());
const mockUser = vi.hoisted(() => ({
    current: null as { email: string; display_name?: string | null } | null,
}));
const menu = vi.hoisted(() => ({
    modal: undefined as boolean | undefined,
    onOpenChange: undefined as ((open: boolean) => void) | undefined,
}));

vi.mock('@/contexts/AuthContext', () => ({
    useAuth: () => ({ logout: mockLogout, user: mockUser.current }),
}));

vi.mock('@/lib/connections', () => ({
    logoutTouka: mockLogoutTouka,
}));

// 把 Radix 下拉攤平（與 SessionItemDeletion 測試相同手法）；開關與真實選單的行為由實際驗證走。
vi.mock('@/components/ui/dropdown-menu', () => ({
    DropdownMenu: ({ children, modal, onOpenChange }: {
        children: React.ReactNode;
        modal?: boolean;
        onOpenChange?: (open: boolean) => void;
    }) => {
        menu.modal = modal;
        menu.onOpenChange = onOpenChange;
        return <div>{children}</div>;
    },
    DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    DropdownMenuContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    DropdownMenuItem: ({ children, onSelect, 'aria-current': ariaCurrent }: {
        children: React.ReactNode;
        onSelect?: (event: React.MouseEvent) => void;
        'aria-current'?: 'page';
    }) => (
        <div role="menuitem" aria-current={ariaCurrent} onClick={(event) => onSelect?.(event)}>
            {children}
        </div>
    ),
}));

import { NavigationArea } from '../NavigationArea';

const defaultProps = {
    activeMode: 'chat',
    isHybrid: false,
    onProjectsClick: vi.fn(),
    onSoulClick: vi.fn(),
    onStudioClick: vi.fn(),
    onPulseClick: vi.fn(),
    onVaultClick: vi.fn(),
    onSettingsClick: vi.fn(),
};

const item = (name: string) => screen.getByRole('menuitem', { name });

beforeEach(() => {
    vi.clearAllMocks();
    mockUser.current = { email: 'otoha@example.test', display_name: 'Otoha' };
});

describe('NavigationArea — 登出按鈕', () => {
    afterEach(() => {
        vi.useRealTimers();
    });

    it('第一次選登出顯示確認提示，選單不關、不立即登出', () => {
        render(<NavigationArea {...defaultProps} />);
        const logout = item('登出');
        const select = createEvent.click(logout);
        fireEvent(logout, select);
        expect(select.defaultPrevented).toBe(true);
        expect(item('確認登出？')).toBeTruthy();
        expect(mockLogout).not.toHaveBeenCalled();
    });

    it('第二次選呼叫 useAuth().logout()，選單仍不自動關（登出後整頁會換掉）', () => {
        render(<NavigationArea {...defaultProps} />);
        fireEvent.click(item('登出'));
        const confirm = item('確認登出？');
        const select = createEvent.click(confirm);
        fireEvent(confirm, select);
        expect(select.defaultPrevented).toBe(true);
        expect(mockLogout).toHaveBeenCalledTimes(1);
    });

    it('不直接呼叫 connections.logoutTouka()', () => {
        render(<NavigationArea {...defaultProps} />);
        fireEvent.click(item('登出'));
        fireEvent.click(item('確認登出？'));
        expect(mockLogoutTouka).not.toHaveBeenCalled();
    });

    it('關掉選單就清掉確認狀態，也清掉計時器', () => {
        vi.useFakeTimers();
        render(<NavigationArea {...defaultProps} />);
        fireEvent.click(item('登出'));
        expect(vi.getTimerCount()).toBe(1);
        act(() => menu.onOpenChange?.(false));
        expect(item('登出')).toBeTruthy();
        expect(vi.getTimerCount()).toBe(0);
    });

    it('打開選單不清確認狀態', () => {
        render(<NavigationArea {...defaultProps} />);
        fireEvent.click(item('登出'));
        act(() => menu.onOpenChange?.(true));
        expect(item('確認登出？')).toBeTruthy();
    });

    it('確認狀態 3 秒後自己消失', () => {
        vi.useFakeTimers();
        render(<NavigationArea {...defaultProps} />);
        fireEvent.click(item('登出'));
        act(() => { vi.advanceTimersByTime(3000); });
        expect(item('登出')).toBeTruthy();
    });

    it('元件卸載時清掉計時器', () => {
        vi.useFakeTimers();
        const { unmount } = render(<NavigationArea {...defaultProps} />);
        fireEvent.click(item('登出'));
        unmount();
        expect(vi.getTimerCount()).toBe(0);
    });
});

describe('NavigationArea — 選單按鈕', () => {
    const trigger = () => screen.getByRole('button', { name: /選單/ });

    it('顯示使用者名稱；沒有名稱顯示 email；都沒有只寫選單', () => {
        const { unmount } = render(<NavigationArea {...defaultProps} />);
        expect(trigger()).toHaveTextContent('Otoha');
        unmount();

        mockUser.current = { email: 'otoha@example.test', display_name: null };
        const second = render(<NavigationArea {...defaultProps} />);
        expect(trigger()).toHaveTextContent('otoha@example.test');
        second.unmount();

        mockUser.current = null;
        render(<NavigationArea {...defaultProps} />);
        expect(trigger()).toHaveTextContent('選單');
    });

    it('在其他畫面時標出目前畫面，名稱也念得到；回聊天就消失', () => {
        const { rerender } = render(<NavigationArea {...defaultProps} activeMode="settings" />);
        expect(trigger()).toHaveTextContent('設定');
        expect(screen.getByRole('button', { name: /選單.*設定/ })).toBeTruthy();
        rerender(<NavigationArea {...defaultProps} activeMode="chat" />);
        expect(trigger()).not.toHaveTextContent('設定');
    });

    it('不用強制對話模式（防手機抽屜關掉後畫面卡住，實際驗證沒重現，靠這條釘住）', () => {
        render(<NavigationArea {...defaultProps} />);
        expect(menu.modal).toBe(false);
    });
});

// 上導覽列拿掉後（2026-10-28），這裡是唯一的分頁入口：接手上導覽列的「目前分頁」標示與只有整合模式才有的分頁。
describe('NavigationArea — 分頁入口', () => {
    it('目前分頁標 aria-current="page"，其他分頁不標', () => {
        render(<NavigationArea {...defaultProps} activeMode="settings" />);
        expect(item('設定')).toHaveAttribute('aria-current', 'page');
        expect(item('專案管理')).not.toHaveAttribute('aria-current');
        expect(item('登出')).not.toHaveAttribute('aria-current');
    });

    it('整合模式才有靈魂核心、系統脈搏、記憶寶庫', () => {
        const hybridOnly = ['靈魂核心', '系統脈搏', '記憶寶庫'];
        const always = ['專案管理', '模型工坊', '設定'];
        const { unmount } = render(<NavigationArea {...defaultProps} isHybrid />);
        for (const name of [...always, ...hybridOnly]) expect(item(name)).toBeTruthy();
        unmount();
        render(<NavigationArea {...defaultProps} isHybrid={false} />);
        for (const name of always) expect(item(name)).toBeTruthy();
        for (const name of hybridOnly) expect(screen.queryByRole('menuitem', { name })).toBeNull();
    });

    it('選單裡沒有聊天大廳', () => {
        render(<NavigationArea {...defaultProps} isHybrid />);
        expect(screen.queryByRole('menuitem', { name: '聊天大廳' })).toBeNull();
    });

    it('每個項目各走自己的 handler', () => {
        render(<NavigationArea {...defaultProps} isHybrid />);
        const cases: [string, keyof typeof defaultProps][] = [
            ['專案管理', 'onProjectsClick'],
            ['靈魂核心', 'onSoulClick'],
            ['模型工坊', 'onStudioClick'],
            ['系統脈搏', 'onPulseClick'],
            ['記憶寶庫', 'onVaultClick'],
            ['設定', 'onSettingsClick'],
        ];
        for (const [name, handler] of cases) {
            fireEvent.click(item(name));
            expect(defaultProps[handler]).toHaveBeenCalledTimes(1);
        }
    });
});
