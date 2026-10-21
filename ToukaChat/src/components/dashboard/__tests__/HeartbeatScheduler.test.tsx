import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { HeartbeatScheduler } from '../HeartbeatScheduler';
import React from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { useHeartbeatSchedules } from '@/hooks/useHeartbeatSchedules';

function authState(role = 'normal'): ReturnType<typeof useAuth> {
    return {
        user: { id: 'owned-user', email: 'owned@example.com', status: 'active', role, owned_agents: ['AgentAlpha'] },
        isAuthenticated: true, isLoading: false, accessToken: 'synthetic',
        login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
    };
}

vi.mock('@/contexts/AuthContext', () => ({
    useAuth: vi.fn(() => ({ user: { role: 'normal', owned_agents: ['AgentAlpha'] } })),
}));
vi.mock('@/hooks/useHeartbeatSchedules', () => ({
    useHeartbeatSchedules: vi.fn(),
}));

// Mock dependencies
vi.mock('@/lib/connections', () => ({
    getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
    getActiveHeartbeatUrl: vi.fn(() => 'http://localhost:8082'),
    fetchTouka: vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ data: [] })
    }),
}));

vi.mock('@/lib/providers/AgentProvider', () => ({
    useAgents: vi.fn(() => ({ agents: [{ id: 'AgentAlpha', name: 'AgentAlpha' }, { id: 'Other', name: 'Other' }] })),
}));

vi.mock('@/lib/providers/SessionListProvider', () => ({
    useSessionList: vi.fn(() => ({ sessions: [] })),
}));

// Mock window.fetch for Heartbeat API
global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve([])
});

describe('HeartbeatScheduler', () => {
    const save = vi.fn();
    beforeEach(() => {
        vi.clearAllMocks();
        save.mockResolvedValue(false);
        vi.mocked(useHeartbeatSchedules).mockReturnValue({
            schedules: [], isLoading: false, isSaving: false, loadError: null, mutationError: null,
            mutationState: 'idle', loadSchedules: vi.fn(), saveSchedule: save, deleteSchedule: vi.fn(),
        } as ReturnType<typeof useHeartbeatSchedules>);
        vi.mocked(useAuth).mockReturnValue(authState());
    });
    it('應該能在每日模式下新增、顯示與刪除時間點標籤', async () => {
        render(<HeartbeatScheduler />);

        // 切換到新增模式
        const addBtn = screen.getByText(/新增任務/i);
        fireEvent.click(addBtn);

        // 切換到每日模式
        const dailyBtn = screen.getByText(/每日固定/i);
        fireEvent.click(dailyBtn);

        // 尋找輸入框並新增時間
        const timeInput = screen.getByLabelText(/新增時間點/i);
        fireEvent.change(timeInput, { target: { value: '12:30' } });

        const plusBtn = screen.getByRole('button', { name: /plus/i });
        fireEvent.click(plusBtn);

        // 1. 驗證標籤是否出現
        expect(screen.getByText('12:30')).toBeDefined();

        // 2. 驗證「已設定的時間清單」標題是否出現
        expect(screen.getByText(/已設定的時間清單/i)).toBeDefined();

        // 3. 測試刪除功能
        const removeBtn = screen.getByLabelText(/remove 12:30/i);
        fireEvent.click(removeBtn);

        // 4. 驗證標籤是否消失
        expect(screen.queryByText('12:30')).toBeNull();
    });

    it('只提供真人可管理的 agent，admin 可選全部', () => {
        const view = render(<HeartbeatScheduler />);
        fireEvent.click(screen.getByText(/新增任務/i));
        expect(screen.queryByRole('option', { name: 'Other' })).toBeNull();
        vi.mocked(useAuth).mockReturnValue(authState('admin'));
        view.rerender(<HeartbeatScheduler />);
        expect(screen.getByRole('option', { name: 'Other' })).toBeDefined();
    });

    it('每日儲存失敗保留編輯值，不修改原 state 陣列', async () => {
        render(<HeartbeatScheduler />);
        fireEvent.click(screen.getByText(/新增任務/i));
        fireEvent.click(screen.getByText(/每日固定/i));
        const input = screen.getByLabelText(/新增時間點/i);
        fireEvent.change(input, { target: { value: '12:30' } });
        fireEvent.click(screen.getByRole('button', { name: /plus/i }));
        fireEvent.change(input, { target: { value: '13:45' } });
        fireEvent.click(screen.getByRole('button', { name: /建立排程|新增排程|啟動排程/i }));
        await waitFor(() => expect(save).toHaveBeenCalled());
        fireEvent.change(screen.getByRole('textbox'), { target: { value: 'retained edit' } });
        expect(screen.getByDisplayValue('retained edit')).toBeDefined();
        expect(screen.getByText('12:30')).toBeDefined();
        expect(screen.queryByText('13:45')).toBeNull();
        expect(save.mock.calls[0][1].daily_times).toEqual(['12:30', '13:45']);
    });

    it('模式標籤和後端對應一致：safe→Auto、full→YOLO、看不懂→Manual（ADR-0246）', () => {
        const base = { agent_id: 'AgentAlpha', active: true, mode: 'interval' as const, interval_minutes: 60,
            pulse_count: 1, session_mode: 'pulse', session_id: 'x' };
        const current = vi.mocked(useHeartbeatSchedules).getMockImplementation()!();
        vi.mocked(useHeartbeatSchedules).mockReturnValue({ ...current, schedules: [
            { ...base, id: 'a', security_level: 'safe' },
            { ...base, id: 'b', security_level: 'full' },
            { ...base, id: 'c', security_level: 'readonly' },
        ] });
        render(<HeartbeatScheduler />);
        expect(screen.getAllByText('Auto')).toHaveLength(1);
        expect(screen.getAllByText('YOLO')).toHaveLength(1);
        expect(screen.getAllByText('Manual')).toHaveLength(1);
        expect(screen.queryByText('safe')).toBeNull();
        fireEvent.click(screen.getByText(/新增任務/i));
        expect(screen.getByRole('option', { name: 'Auto（無人在場時不跑指令）' })).toHaveValue('safe');
        expect(screen.getByRole('option', { name: 'YOLO（⚠️ 有風險）' })).toHaveValue('full');
        expect(screen.queryByText(/唯讀/)).toBeNull();
    });

    it('清單失敗顯示錯誤和重試，不能當成空排程', () => {
        const current = vi.mocked(useHeartbeatSchedules).getMockImplementation()!();
        vi.mocked(useHeartbeatSchedules).mockReturnValue({ ...current, loadError: 'owned visible error' });
        render(<HeartbeatScheduler />);
        expect(screen.getByRole('alert')).toHaveTextContent('owned visible error');
        expect(screen.getByRole('button', { name: /重新載入/ })).toBeDefined();
        expect(screen.queryByText(/目前沒有|沒有排程/)).toBeNull();
    });
});
