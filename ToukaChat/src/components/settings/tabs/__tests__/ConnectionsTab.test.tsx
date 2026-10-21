import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { ConnectionsTab } from '../ConnectionsTab';

// Mock hook
const mockUseConnectionSettings = {
    connections: [
        { id: "conn-1", name: "Alpha", url: "http://alpha", enabled: true, protocol: "openai" },
        { id: "conn-2", name: "Beta", url: "http://beta", enabled: false, protocol: "anthropic" }
    ],
    connectionStatus: { "conn-1": "ok" },
    fetchConnections: vi.fn(),
    fetchConnectionStatus: vi.fn(),
    testConnection: vi.fn(),
    saveConnections: vi.fn(),
    addConnection: vi.fn(),
    removeConnection: vi.fn(),
    updateConnection: vi.fn()
};

vi.mock('@/hooks/settings/useConnectionSettings', () => ({
    useConnectionSettings: () => mockUseConnectionSettings
}));

// 刪除連線改走確認框：此處自動確認，沿用既有「點刪即刪」斷言。
vi.mock('@/hooks/useConfirmToast', () => ({
    useConfirmToast: () => ({
        confirmToast: (_m: string, onConfirm: () => void) => { onConfirm(); },
    }),
}));

describe('ConnectionsTab (Baseline)', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('renders existing connections', () => {
        render(<ConnectionsTab isSystemRefreshing={false} onRefresh={() => {}} />);
        expect(screen.getAllByText(/Alpha/i)).not.toHaveLength(0);
        expect(screen.getAllByText(/Beta/i)).not.toHaveLength(0);
        expect(screen.getAllByText(/http:\/\/alpha/i)).not.toHaveLength(0);
    });

    it('triggers saveConnections when toggling enabled state', () => {
        render(<ConnectionsTab isSystemRefreshing={false} onRefresh={() => {}} />);
        const checkboxes = screen.getAllByRole('checkbox');
        fireEvent.click(checkboxes[0]);
        expect(mockUseConnectionSettings.saveConnections).toHaveBeenCalled();
    });

    it('triggers addConnection when submitting the form', () => {
        render(<ConnectionsTab isSystemRefreshing={false} onRefresh={() => {}} />);
        
        const inputs = screen.getAllByRole('textbox');
        // inputs[0]: name, inputs[1]: url
        fireEvent.change(inputs[0], { target: { value: 'Gamma' } });
        fireEvent.change(inputs[1], { target: { value: 'http://gamma' } });
        
        const addBtn = screen.getByRole('button', { name: /新增連線/i });
        fireEvent.click(addBtn);
        
        expect(mockUseConnectionSettings.addConnection).toHaveBeenCalledWith(expect.objectContaining({
            name: 'Gamma',
            url: 'http://gamma'
        }));
    });

    it('triggers removeConnection when clicking delete', () => {
        render(<ConnectionsTab isSystemRefreshing={false} onRefresh={() => {}} />);
        // 以 aria-label 精準鎖定刪除鈕（避免與新增的編輯鈕混淆）
        const removeBtn = screen.getAllByRole('button', { name: /移除連線/i })[0];
        fireEvent.click(removeBtn);
        expect(mockUseConnectionSettings.removeConnection).toHaveBeenCalledWith('conn-1');
    });

    it('enters edit mode (form shows editing badge) when clicking edit', () => {
        render(<ConnectionsTab isSystemRefreshing={false} onRefresh={() => {}} />);
        const editBtn = screen.getAllByRole('button', { name: /編輯連線/i })[0];
        fireEvent.click(editBtn);
        // 表單切換為編輯模式，標題顯示「編輯中：…」
        expect(screen.getByText(/編輯中/)).toBeDefined();
    });

    it('displays refreshing state on system info button', () => {
        render(<ConnectionsTab isSystemRefreshing={true} onRefresh={() => {}} />);
        expect(screen.getByText(/更新中.../i)).toBeDefined();
    });
});
