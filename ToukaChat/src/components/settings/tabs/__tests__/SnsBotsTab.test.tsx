import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { SnsBotsTab } from '@/components/settings/tabs/SnsBotsTab';
import type { useSnsBotSettings } from '@/hooks/settings/useSnsBotSettings';

// Mock hook
const mockUseSnsBotSettings: ReturnType<typeof useSnsBotSettings> = {
    snsBots: {
        discord: [
            { token: "discord-token-1", agent_id: "MockAgentA", enabled: true, session_mode: "independent", is_multi_agent: false }
        ],
        telegram: [
            { token: "telegram-token-1", agent_id: "MockAgentB", enabled: false, session_mode: "home", is_multi_agent: true }
        ]
    },
    setSnsBots: vi.fn(),
    fetchSnsBots: vi.fn().mockResolvedValue(true),
    resetSnsDrafts: vi.fn(),
    loadState: 'ready',
    isSaving: false,
    tokenDraftCleared: false,
    saveSnsBots: vi.fn().mockResolvedValue(true)
};


// F5：刪 bot 需經 confirmToast；mock 可控（預設不自動確認）。
const confirmToast = vi.fn();
vi.mock('@/hooks/useConfirmToast', () => ({
    useConfirmToast: () => ({ confirmToast }),
}));

const mockAllAgents = [
    { id: "MockAgentA", name: "MockAgentA", type: "soul", is_group: false },
    { id: "MockAgentB", name: "MockAgentB", type: "soul", is_group: false },
    { id: "MockAgentC", name: "MockAgentC", type: "soul", is_group: false }
];

describe('SnsBotsTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockUseSnsBotSettings.loadState = 'ready';
        mockUseSnsBotSettings.isSaving = false;
        mockUseSnsBotSettings.tokenDraftCleared = false;
    });

    it('renders existing SNS bots configuration', () => {
        render(<SnsBotsTab allAgents={mockAllAgents} settings={mockUseSnsBotSettings} />);
        
        // 驗證 Discord / Telegram 標題
        expect(screen.getByText(/Discord 機器人/i)).toBeDefined();
        expect(screen.getByText(/Telegram 機器人/i)).toBeDefined();

        // 驗證現有 token 渲染
        expect(screen.getByDisplayValue('discord-token-1')).toBeDefined();
        expect(screen.getByDisplayValue('telegram-token-1')).toBeDefined();
    });

    it('handles adding new bot configuration item', () => {
        render(<SnsBotsTab allAgents={mockAllAgents} settings={mockUseSnsBotSettings} />);
        
        // 點擊新增 Discord 機器人
        const addBtns = screen.getAllByRole('button', { name: /新增機器人/i });
        fireEvent.click(addBtns[0]); // Discord add button

        // 應觸發 setSnsBots 呼叫
        expect(mockUseSnsBotSettings.setSnsBots).toHaveBeenCalled();
    });

    it('assigns a stable id to a newly added bot (B2 React-key SSOT)', () => {
        render(<SnsBotsTab allAgents={mockAllAgents} settings={mockUseSnsBotSettings} />);
        const addBtns = screen.getAllByRole('button', { name: /新增機器人/i });
        fireEvent.click(addBtns[0]); // Discord add

        const update = vi.mocked(mockUseSnsBotSettings.setSnsBots).mock.calls[0][0];
        const updated = typeof update === 'function' ? update(mockUseSnsBotSettings.snsBots) : update;
        const newBot = updated.discord[updated.discord.length - 1];
        expect(typeof newBot.id).toBe('string');
        expect(newBot.id).toMatch(/./);
    });

    it('F5: 刪除前先要求二次確認（未確認則不刪）', () => {
        confirmToast.mockImplementation(() => { /* 不自動確認 */ });
        render(<SnsBotsTab allAgents={mockAllAgents} settings={mockUseSnsBotSettings} />);
        const deleteBtns = screen.getAllByRole('button').filter(b => b.className.includes('hover:text-destructive'));
        expect(deleteBtns.length).toBeGreaterThanOrEqual(1);
        fireEvent.click(deleteBtns[0]);
        // 走確認流程，但未確認 → 尚未實際刪除
        expect(confirmToast).toHaveBeenCalled();
        expect(mockUseSnsBotSettings.setSnsBots).not.toHaveBeenCalled();
    });

    it('F5: 確認後才實際刪除（setSnsBots）', () => {
        confirmToast.mockImplementation((_msg: string, onConfirm: () => void) => onConfirm());
        render(<SnsBotsTab allAgents={mockAllAgents} settings={mockUseSnsBotSettings} />);
        const deleteBtns = screen.getAllByRole('button').filter(b => b.className.includes('hover:text-destructive'));
        fireEvent.click(deleteBtns[0]);
        expect(mockUseSnsBotSettings.setSnsBots).toHaveBeenCalled();
    });

    it('A5: Bot Token 欄有 label 關聯（getByLabelText）', () => {
        render(<SnsBotsTab allAgents={mockAllAgents} settings={mockUseSnsBotSettings} />);
        expect(screen.getAllByLabelText(/Bot Token/i).length).toBeGreaterThan(0);
    });

    it('A6: 啟用/多代理開關有可及名稱（aria-label）', () => {
        render(<SnsBotsTab allAgents={mockAllAgents} settings={mockUseSnsBotSettings} />);
        expect(screen.getAllByRole('switch', { name: /啟用此機器人/ }).length).toBeGreaterThan(0);
        expect(screen.getAllByRole('switch', { name: /多代理人共同對話/ }).length).toBeGreaterThan(0);
    });

    it('A5: 跨平台（discord/telegram）欄位 id 不重複（無 id 時以平台前綴避免碰撞）', () => {
        // mock 的 bot 皆無 id → 退路用 index；無平台前綴會讓 discord[0] 與 telegram[0] 撞 id
        const { container } = render(<SnsBotsTab allAgents={mockAllAgents} settings={mockUseSnsBotSettings} />);
        const ids = Array.from(container.querySelectorAll('[id^="sns-token-"],[id^="sns-agent-"]')).map(el => el.id);
        expect(ids.length).toBeGreaterThan(0);
        expect(new Set(ids).size).toBe(ids.length); // 全部唯一
    });

    it('triggers saveSnsBots when clicking save button', async () => {
        render(<SnsBotsTab allAgents={mockAllAgents} settings={mockUseSnsBotSettings} />);
        
        // 點擊儲存按鈕
        const saveBtn = screen.getByRole('button', { name: /儲存設定/i });
        fireEvent.click(saveBtn);
        
        expect(mockUseSnsBotSettings.saveSnsBots).toHaveBeenCalled();
    });

    it('uses the shared settings and reloads on entering the form', () => {
        const { unmount } = render(<SnsBotsTab allAgents={mockAllAgents} settings={mockUseSnsBotSettings} />);
        expect(mockUseSnsBotSettings.fetchSnsBots).toHaveBeenCalledOnce();
        unmount();
        expect(mockUseSnsBotSettings.resetSnsDrafts).toHaveBeenCalledOnce();
    });

    it.each(['loading', 'error'] as const)('blocks editing and saving while configuration is %s', (state) => {
        mockUseSnsBotSettings.loadState = state;
        render(<SnsBotsTab allAgents={mockAllAgents} settings={mockUseSnsBotSettings} />);
        expect(screen.getByRole('button', { name: /儲存設定/ })).toBeDisabled();
        expect(screen.getAllByLabelText(/Bot Token/)[0]).toBeDisabled();
        if (state === 'error') {
            fireEvent.click(screen.getByRole('button', { name: /重新載入設定/ }));
            expect(mockUseSnsBotSettings.fetchSnsBots).toHaveBeenCalledTimes(2);
        }
    });

    it('blocks controls during submission and explains why tokens need re-entry after failure', () => {
        mockUseSnsBotSettings.isSaving = true;
        mockUseSnsBotSettings.tokenDraftCleared = true;
        render(<SnsBotsTab allAgents={mockAllAgents} settings={mockUseSnsBotSettings} />);
        expect(screen.getAllByLabelText(/Bot Token/)[0]).toBeDisabled();
        expect(screen.getByRole('status')).toHaveTextContent('Token 已清除');
    });
});
