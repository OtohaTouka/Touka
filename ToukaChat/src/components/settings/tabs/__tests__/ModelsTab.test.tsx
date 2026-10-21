import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';

const mockTestModel = vi.fn();
const mockUpdateModelSetting = vi.fn();
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const baseModels: any[] = [
    { id: 'gpt-x', name: 'GPT X', owned_by: 'openai', enabled: true, visibility: 'visible', status: 'online' },
];
let modelsForTest = baseModels;

vi.mock('@/lib/providers/ModelProvider', () => ({
    useModels: () => ({
        allModels: modelsForTest,
        updateModelSetting: mockUpdateModelSetting,
        deleteModel: vi.fn(),
        triggerBackendRefresh: vi.fn(),
        isRefreshing: false,
        testModel: mockTestModel,
    }),
}));
const mockConfirmToast = vi.fn();
vi.mock('@/hooks/useConfirmToast', () => ({ useConfirmToast: () => ({ confirmToast: mockConfirmToast }) }));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { ModelsTab } from '../ModelsTab';

describe('ModelsTab 空狀態（P2）', () => {
    beforeEach(() => { vi.clearAllMocks(); modelsForTest = baseModels; });

    it('無任何模型 → 顯示「尚未發現模型」空狀態', () => {
        modelsForTest = [];
        render(<ModelsTab />);
        expect(screen.getByText(/尚未發現任何模型/)).toBeDefined();
    });

    it('有模型但搜尋無結果 → 顯示「無符合」空狀態', () => {
        render(<ModelsTab />);
        const search = screen.getByPlaceholderText(/搜尋模型/);
        fireEvent.change(search, { target: { value: 'zzz-nonexistent' } });
        expect(screen.getByText(/沒有符合搜尋或篩選條件/)).toBeDefined();
    });

    it('有符合的模型 → 不顯示空狀態', () => {
        render(<ModelsTab />);
        expect(screen.queryByText(/尚未發現任何模型/)).toBeNull();
        expect(screen.queryByText(/沒有符合搜尋或篩選條件/)).toBeNull();
    });
});

describe('ModelsTab 測試鈕（P2）', () => {
    beforeEach(() => { vi.clearAllMocks(); modelsForTest = baseModels; });

    it('點測試鈕 → 呼叫 testModel(id) 並顯示健康結果', async () => {
        mockTestModel.mockResolvedValue({ status: 'healthy', latency_ms: 42 });
        render(<ModelsTab />);
        fireEvent.click(screen.getByRole('button', { name: /測試/ }));
        await waitFor(() => expect(mockTestModel).toHaveBeenCalledWith('gpt-x'));
        expect(await screen.findByText(/正常/)).toBeDefined();
    });
});

describe('ModelsTab 「可啟用」徽章在地化（P4）', () => {
    beforeEach(() => { vi.clearAllMocks(); });

    it('線上但關閉的模型顯示在地化「可啟用」徽章（非寫死 New / Hidden）', () => {
        modelsForTest = [
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            { id: 'off-model', name: 'Off Model', owned_by: 'openai', enabled: false, visibility: 'visible', status: 'online' } as any,
        ];
        render(<ModelsTab />);
        expect(screen.getByText('可啟用')).toBeDefined();
        expect(screen.queryByText('New / Hidden')).toBeNull();
    });
});

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const nativeOpenAI: any = {
    id: 'gpt-x', name: 'GPT X', owned_by: 'openai', enabled: true, visibility: 'visible', status: 'online',
    params_editable: true, supports_top_k: false, temperature_max: 2,
};

describe('ModelsTab 沒有取樣參數面板（ADR-0250 A2：取樣參數不會送出）', () => {
    beforeEach(() => { vi.clearAllMocks(); });

    it('params_editable 的原生模型也沒有參數鈕與滑桿；啟用／隱藏照常', () => {
        modelsForTest = [nativeOpenAI];
        const { container } = render(<ModelsTab />);
        expect(container.querySelectorAll('[aria-pressed]')).toHaveLength(0);
        expect(screen.queryByRole('button', { name: /取樣參數|models_tab\.params/ })).toBeNull();
        expect(screen.queryAllByRole('slider')).toHaveLength(0);
        fireEvent.click(screen.getByRole('button', { name: '可見' }));
        expect(mockUpdateModelSetting).toHaveBeenCalledWith('gpt-x', { visibility: 'hidden' });
    });
});
