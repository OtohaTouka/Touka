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

// 摘要模型選項（ADR-0305）：只列已設定金鑰的模型
const modelState: { models: Array<{ id: string; name: string; provider: string }> | null; loadFailed: boolean } = {
    models: [], loadFailed: false,
};
vi.mock('@/hooks/settings/useKeyedModels', () => ({
    useKeyedModels: () => modelState,
}));

// Radix Select 在 jsdom 不支援 fireEvent.change，換成原生 <select>
vi.mock('@/components/ui/select', async () => {
    const { createElement, Fragment } = await import('react');
    const Select = ({ children, value, onValueChange }: { children: React.ReactNode; value?: string; onValueChange?: (v: string) => void }) =>
        createElement('select', { value, onChange: (e: React.ChangeEvent<HTMLSelectElement>) => onValueChange?.(e.target.value) }, children);
    const Pass = ({ children }: { children?: React.ReactNode }) => createElement(Fragment, null, children);
    const SelectItem = ({ children, value }: { children: React.ReactNode; value: string }) => createElement('option', { value }, children);
    return { Select, SelectTrigger: Pass, SelectValue: () => null, SelectContent: Pass, SelectItem };
});

function setConfig(over: Record<string, unknown>) {
    mockState.config = {
        enabled: true, trigger_pct: 85, target_floor_pct: 60,
        summary_budget_pct: 10, summary_model: 'haiku', prelude_summary_enabled: true, ...over,
    };
}

const HAIKU_45 = { id: 'claude-haiku-4-5', name: 'Claude Haiku 4.5', provider: 'anthropic' };
const HAIKU_55 = { id: 'claude-haiku-5-5', name: 'Claude Haiku 5.5', provider: 'anthropic' };

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

describe('SlidingWindowTab 摘要模型與前情摘要（ADR-0305）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        modelState.models = [HAIKU_45, HAIKU_55];
        modelState.loadFailed = false;
        mockState.saving = false;
    });

    it('換摘要模型之後存檔，送出新的模型', async () => {
        setConfig({ summary_model: 'claude-haiku-4-5' });
        mockState.saveConfig = vi.fn().mockResolvedValue(true);
        const { container } = render(<SlidingWindowTab />);
        const select = container.querySelector('select') as HTMLSelectElement;
        expect(select.value).toBe('claude-haiku-4-5');
        fireEvent.change(select, { target: { value: 'claude-haiku-5-5' } });
        fireEvent.click(screen.getByRole('button', { name: /儲存設定/ }));
        await waitFor(() => expect(mockState.saveConfig).toHaveBeenCalledWith(
            expect.objectContaining({ summary_model: 'claude-haiku-5-5' }),
        ));
    });

    it('前情摘要開關：關掉後存檔送 false；滑動視窗沒開時也能改', async () => {
        setConfig({ enabled: false, prelude_summary_enabled: true });
        mockState.saveConfig = vi.fn().mockResolvedValue(true);
        render(<SlidingWindowTab />);
        const toggle = screen.getByRole('switch', { name: /前情摘要/ });
        expect(toggle.getAttribute('aria-checked')).toBe('true');
        expect(toggle.closest('.pointer-events-none')).toBeNull();
        fireEvent.click(toggle);
        fireEvent.click(screen.getByRole('button', { name: /儲存設定/ }));
        await waitFor(() => expect(mockState.saveConfig).toHaveBeenCalledWith(
            expect.objectContaining({ prelude_summary_enabled: false }),
        ));
    });

    it('目前的模型沒有設定金鑰時，照樣列出並提醒', () => {
        setConfig({ summary_model: 'claude-opus-4' });
        const { container } = render(<SlidingWindowTab />);
        const values = Array.from(container.querySelectorAll('option')).map(o => o.getAttribute('value'));
        expect(values).toEqual(expect.arrayContaining(['claude-opus-4', 'claude-haiku-5-5']));
        expect(container.textContent || '').toContain('claude-opus-4 不在已設定金鑰的模型裡');
    });

    it('模型清單讀不到時顯示提示，目前的模型照樣留著', () => {
        modelState.models = null;
        modelState.loadFailed = true;
        setConfig({ summary_model: 'claude-haiku-5-5' });
        const { container } = render(<SlidingWindowTab />);
        expect(screen.getByRole('alert').textContent).toContain('讀不到模型清單');
        expect((container.querySelector('select') as HTMLSelectElement).value).toBe('claude-haiku-5-5');
        expect(container.textContent || '').not.toContain('不在已設定金鑰的模型裡');
    });
});
