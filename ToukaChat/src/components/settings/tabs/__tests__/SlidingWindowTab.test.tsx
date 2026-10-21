import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';
import { SlidingWindowTab } from '../SlidingWindowTab';

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
import { toast } from 'sonner';

// 可變 mock：每個案例先 setConfig 再 render，故與隨機測試次序無關。
const mockState: { config: Record<string, unknown>; loading: boolean; saving: boolean; saveConfig: ReturnType<typeof vi.fn> } = {
    config: {}, loading: false, saving: false, saveConfig: vi.fn(),
};
vi.mock('@/hooks/settings/useSlidingWindowSettings', () => ({
    useSlidingWindowSettings: () => mockState,
}));

function setConfig(over: Record<string, unknown>) {
    mockState.config = {
        enabled: true, trigger_pct: 85, target_floor_pct: 60,
        summary_budget_pct: 10, summary_model: 'haiku', ...over,
    };
}

describe('SlidingWindowTab summary line (<Trans> interpolation, i18n Phase 1)', () => {
    it('with-summary: composes the sentence with interpolated percentages + mono spans', () => {
        setConfig({ summary_budget_pct: 10 });
        const { container } = render(<SlidingWindowTab />);
        const text = container.textContent || '';
        expect(text).toContain('當前設定：context 占用達');
        expect(text).toContain('85%'); // trigger
        expect(text).toContain('60%'); // floor
        expect(text).toContain('10%'); // summary
        expect(text).toContain('70%'); // after = floor + summary
        expect(text).toContain('的摘要，剪後約');
    });

    it('no-summary: shows the pure-lossless-trim sentence (no summary fragment)', () => {
        setConfig({ summary_budget_pct: 0 });
        const { container } = render(<SlidingWindowTab />);
        const text = container.textContent || '';
        expect(text).toContain('（不摘要，純無損剪裁）');
        expect(text).toContain('85%');
        expect(text).toContain('60%');
        expect(text).not.toContain('的摘要，剪後約');
    });
});

describe('SlidingWindowTab P2 feedback (F4/F7/F8)', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('F4: 存檔成功跳 toast.success', async () => {
        setConfig({});
        mockState.saving = false;
        mockState.saveConfig = vi.fn().mockResolvedValue(true);
        render(<SlidingWindowTab />);
        // 先動滑桿 → dirty=true 解鎖存檔鈕
        fireEvent.change(screen.getAllByRole('slider')[0], { target: { value: '80' } });
        fireEvent.click(screen.getByRole('button', { name: /儲存設定/ }));
        await waitFor(() => expect(toast.success).toHaveBeenCalled());
        expect(toast.error).not.toHaveBeenCalled();
    });

    it('F7: 伺服器存檔失敗跳 toast.error（非僅 inline）', async () => {
        setConfig({});
        mockState.saving = false;
        mockState.saveConfig = vi.fn().mockResolvedValue(false);
        render(<SlidingWindowTab />);
        fireEvent.change(screen.getAllByRole('slider')[0], { target: { value: '80' } });
        fireEvent.click(screen.getByRole('button', { name: /儲存設定/ }));
        await waitFor(() => expect(toast.error).toHaveBeenCalled());
    });

    it('F8: 降低 trigger 至低於 floor 時，state 即時收斂以維持不變式（不脫鉤）', () => {
        setConfig({ trigger_pct: 85, target_floor_pct: 60, summary_budget_pct: 10 });
        mockState.saveConfig = vi.fn();
        const { container } = render(<SlidingWindowTab />);
        // 把 trigger 拉到 55（低於原 floor 60；trigger 滑桿 min=50）
        fireEvent.change(screen.getAllByRole('slider')[0], { target: { value: '55' } });
        const sliders = screen.getAllByRole('slider') as HTMLInputElement[];
        const trigger = Number(sliders[0].value);
        const floor = Number(sliders[1].value);
        const summary = Number(sliders[2].value);
        // 不變式維持：floor + summary < trigger，且 floor 已從 60 收斂下來
        expect(floor).toBeLessThan(trigger);
        expect(floor + summary).toBeLessThan(trigger);
        expect(floor).toBeLessThan(60);
        // 顯示值與 slider 一致（無 desync）
        expect(container.textContent || '').toContain(`${floor}%`);
    });

    it('A1: 啟用開關為可及 switch（role=switch + 可及名稱）', () => {
        setConfig({});
        render(<SlidingWindowTab />);
        expect(screen.getByRole('switch', { name: /啟用滑動視窗/ })).toBeTruthy();
    });

    it('A2: 三個滑桿皆有可及名稱（aria-label）', () => {
        setConfig({});
        render(<SlidingWindowTab />);
        expect(screen.getByRole('slider', { name: /觸發百分比/ })).toBeTruthy();
        expect(screen.getByRole('slider', { name: /地板百分比/ })).toBeTruthy();
        expect(screen.getByRole('slider', { name: /摘要百分比/ })).toBeTruthy();
    });

    it('F8: 多次連續調整（升/降交錯）後不變式恆成立', () => {
        setConfig({ trigger_pct: 90, target_floor_pct: 60, summary_budget_pct: 20 });
        mockState.saveConfig = vi.fn();
        render(<SlidingWindowTab />);
        const inv = () => {
            const s = screen.getAllByRole('slider') as HTMLInputElement[];
            return { trigger: Number(s[0].value), floor: Number(s[1].value), summary: Number(s[2].value) };
        };
        const seq = ['55', '99', '50', '70', '51'];
        for (const v of seq) {
            fireEvent.change(screen.getAllByRole('slider')[0], { target: { value: v } }); // 反覆動 trigger
            const { trigger, floor, summary } = inv();
            expect(floor + summary).toBeLessThan(trigger); // 不變式每步都成立
            expect(floor).toBeGreaterThanOrEqual(0);
            expect(summary).toBeGreaterThanOrEqual(0);
        }
    });
});
