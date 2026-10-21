/**
 * NavigationArea 登出按鈕行為測試
 *
 * Bug 修復：handleLogout 必須呼叫 useAuth().logout()，
 * 而非直接呼叫 connections.logoutTouka()，以確保
 * AuthContext 的 React state 正確清空。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, screen } from '@testing-library/react';
import React from 'react';

// vi.mock() calls are hoisted — use vi.hoisted() to ensure variables
// are available when the mock factory runs.
const mockLogout = vi.hoisted(() => vi.fn());
const mockLogoutTouka = vi.hoisted(() => vi.fn());

vi.mock('@/contexts/AuthContext', () => ({
    useAuth: () => ({ logout: mockLogout }),
}));

vi.mock('@/lib/connections', () => ({
    logoutTouka: mockLogoutTouka,
}));

import { NavigationArea } from '../NavigationArea';

const defaultProps = {
    activeMode: 'chat',
    isHybrid: false,
    onChatClick: vi.fn(),
    onProjectsClick: vi.fn(),
    onSoulClick: vi.fn(),
    onStudioClick: vi.fn(),
    onPulseClick: vi.fn(),
    onVaultClick: vi.fn(),
    onSettingsClick: vi.fn(),
};

describe('NavigationArea — 登出按鈕', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('第一次點擊顯示確認提示，不立即登出', () => {
        render(React.createElement(NavigationArea, defaultProps));
        fireEvent.click(screen.getByText('登出'));
        expect(screen.getByText('確認登出？')).toBeTruthy();
        expect(mockLogout).not.toHaveBeenCalled();
    });

    it('第二次點擊呼叫 useAuth().logout()', () => {
        render(React.createElement(NavigationArea, defaultProps));
        fireEvent.click(screen.getByText('登出'));
        fireEvent.click(screen.getByText('確認登出？'));
        expect(mockLogout).toHaveBeenCalledTimes(1);
    });

    it('不直接呼叫 connections.logoutTouka()', () => {
        render(React.createElement(NavigationArea, defaultProps));
        fireEvent.click(screen.getByText('登出'));
        fireEvent.click(screen.getByText('確認登出？'));
        expect(mockLogoutTouka).not.toHaveBeenCalled();
    });
});
