import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { VaultDashboard } from '../VaultDashboard';

// 1780569921 秒 = 2026-06-04；後端修復後於邊界轉為毫秒回傳
const TS_MS = 1780569921000;

const MEMORIES = [
    { id: 1, content: '記憶 J1', agent_id: 'Alice', timestamp: TS_MS, tags: [] },
    { id: 2, content: '記憶 J2', agent_id: 'Alice', timestamp: TS_MS, tags: [] },
    { id: 3, content: '記憶 Y1', agent_id: 'Bob', timestamp: TS_MS, tags: [] },
];

vi.mock('@/lib/connections', () => ({
    getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
    fetchTouka: vi.fn((_url: string, path: string) => {
        if (path.includes('/memory/list')) {
            return Promise.resolve({ ok: true, json: () => Promise.resolve({ memories: MEMORIES }) });
        }
        return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ facts: true, vector: true, legacy: true, personal: true }),
        });
    }),
}));

// 頁面在讀取失敗時會請側欄重抓狀態；本測試只看成功的列表
vi.mock('@/lib/SystemStatusContext', () => ({
    useSystemStatus: () => ({ status: {}, checkStatus: vi.fn() }),
}));

// 隔離：管理分頁元件與本測試無關
vi.mock('@/components/settings/tabs/LegacyMonitorTab', () => ({
    LegacyMonitorTab: () => null,
}));

describe('VaultDashboard 記憶寶庫', () => {
    it('每個 agent 顯示完整記憶筆數（前端不再截斷）', async () => {
        render(<VaultDashboard />);
        expect(await screen.findByText('Agent: Alice')).toBeDefined();
        expect(screen.getByText('Agent: Bob')).toBeDefined();
        // Alice 2 則、Bob 1 則，皆完整呈現
        expect(screen.getByText('2 則回憶')).toBeDefined();
        expect(screen.getByText('1 則回憶')).toBeDefined();
    });

    it('時間以台北時間正確顯示為 2026（毫秒），而非 1970', async () => {
        render(<VaultDashboard />);
        // 以搜尋強制展開所有 agent 與記憶項目，讓時間欄位渲染
        const search = await screen.findByPlaceholderText(/檢索記憶內容/i);
        fireEvent.change(search, { target: { value: '記憶' } });

        const dates = await screen.findAllByText(/2026/);
        expect(dates.length).toBeGreaterThan(0);
        expect(screen.queryByText(/1970/)).toBeNull();
    });
});
