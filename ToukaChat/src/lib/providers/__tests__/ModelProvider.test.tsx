/**
 * ModelProvider — deleteModel 與 triggerBackendRefresh 行為修復測試
 *
 * Bug 1: deleteModel 在 !res.ok 時必須拋出例外（讓 ModelsTab catch 顯示 toast.error）
 * Bug 2: triggerBackendRefresh 無論 res.ok 與否都必須呼叫 refreshModels 刷新列表
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import React from 'react';

vi.mock('@/lib/connections', () => ({
    fetchTouka: vi.fn(),
    getActiveProxyUrl: vi.fn().mockReturnValue('http://localhost:8000'),
}));

vi.mock('@/lib/error_handler', () => ({
    logSSOTError: vi.fn(),
}));

import { fetchTouka } from '@/lib/connections';
import { pickDefaultModel } from '@/lib/default_model';
import { ModelProvider, useModels, type ModelInfo } from '../ModelProvider';

const mockFetchTouka = fetchTouka as ReturnType<typeof vi.fn>;

const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(ModelProvider, null, children);

const mockModelsResponse = { ok: true, json: async () => ({ data: [] }) };

describe('ModelProvider — deleteModel 錯誤傳遞', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockFetchTouka.mockResolvedValue(mockModelsResponse);
    });

    it('deleteModel 在後端回 !res.ok 時必須拋出例外', async () => {
        mockFetchTouka.mockImplementation((_url: string, path: string) => {
            if (path.startsWith('/v1/models/') && !path.endsWith('/settings')) {
                // DELETE 模型時回傳 403
                return Promise.resolve({
                    ok: false,
                    status: 403,
                    json: async () => ({ detail: { message: '無權限' } }),
                });
            }
            return Promise.resolve(mockModelsResponse);
        });

        const { result } = renderHook(() => useModels(), { wrapper });
        await waitFor(() => expect(mockFetchTouka).toHaveBeenCalled());

        await expect(result.current.deleteModel('some-model')).rejects.toThrow();
    });

    it('deleteModel 成功時呼叫 refreshModels 更新列表', async () => {
        let getModelsCallCount = 0;
        mockFetchTouka.mockImplementation((_url: string, path: string, options?: RequestInit) => {
            if (path === '/v1/models') {
                getModelsCallCount++;
                return Promise.resolve(mockModelsResponse);
            }
            if (options?.method === 'DELETE') {
                return Promise.resolve({ ok: true, json: async () => ({}) });
            }
            return Promise.resolve(mockModelsResponse);
        });

        const { result } = renderHook(() => useModels(), { wrapper });
        await waitFor(() => expect(mockFetchTouka).toHaveBeenCalled());
        const beforeCount = getModelsCallCount;

        await act(async () => {
            await result.current.deleteModel('some-model');
        });

        expect(getModelsCallCount).toBeGreaterThan(beforeCount);
    });
});

describe('ModelProvider — triggerBackendRefresh 永遠刷新列表', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockFetchTouka.mockResolvedValue(mockModelsResponse);
    });

    it('triggerBackendRefresh 在後端回 !res.ok 時仍須呼叫 refreshModels', async () => {
        let getModelsCallCount = 0;
        mockFetchTouka.mockImplementation((_url: string, path: string, options?: RequestInit) => {
            if (path === '/v1/models') {
                getModelsCallCount++;
                return Promise.resolve(mockModelsResponse);
            }
            if (path === '/v1/models/refresh' && options?.method === 'POST') {
                return Promise.resolve({ ok: false, status: 500 });
            }
            return Promise.resolve(mockModelsResponse);
        });

        const { result } = renderHook(() => useModels(), { wrapper });
        await waitFor(() => expect(mockFetchTouka).toHaveBeenCalled());
        const beforeCount = getModelsCallCount;

        await act(async () => {
            await result.current.triggerBackendRefresh();
        });

        // 無論 refresh 是否成功，GET /v1/models 都必須被再次呼叫
        expect(getModelsCallCount).toBeGreaterThan(beforeCount);
    });

    it('triggerBackendRefresh 在後端回 ok 時也呼叫 refreshModels', async () => {
        let getModelsCallCount = 0;
        mockFetchTouka.mockImplementation((_url: string, path: string, options?: RequestInit) => {
            if (path === '/v1/models') {
                getModelsCallCount++;
                return Promise.resolve(mockModelsResponse);
            }
            if (path === '/v1/models/refresh' && options?.method === 'POST') {
                return Promise.resolve({ ok: true, json: async () => ({ new_count: 2, total: 5 }) });
            }
            return Promise.resolve(mockModelsResponse);
        });

        const { result } = renderHook(() => useModels(), { wrapper });
        await waitFor(() => expect(mockFetchTouka).toHaveBeenCalled());
        const beforeCount = getModelsCallCount;

        await act(async () => {
            await result.current.triggerBackendRefresh();
        });

        expect(getModelsCallCount).toBeGreaterThan(beforeCount);
    });
});

describe('ModelProvider — availableModels 可見性過濾（聊天選單來源）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('排除 enabled 但 hidden 的模型、排除 disabled，保留 enabled+visible，且保留當前選中模型即使隱藏', async () => {
        const models = [
            { id: 'vis', name: 'Vis', owned_by: 'p', enabled: true, visibility: 'visible' },
            { id: 'hid', name: 'Hid', owned_by: 'p', enabled: true, visibility: 'hidden' },
            { id: 'off', name: 'Off', owned_by: 'p', enabled: false, visibility: 'visible' },
            // 使用者選中的模型，即使 hidden 也須保留
            { id: 'sel', name: 'Sel', owned_by: 'p', enabled: true, visibility: 'hidden' },
        ];
        mockFetchTouka.mockImplementation((_url: string, path: string) => {
            if (path === '/v1/models') {
                return Promise.resolve({ ok: true, json: async () => ({ data: models }) });
            }
            return Promise.resolve(mockModelsResponse);
        });

        const { result } = renderHook(() => useModels(), { wrapper });
        await waitFor(() => expect(result.current.availableModels.length).toBeGreaterThan(0));
        act(() => { result.current.setSelectedModel('sel'); });

        const ids = result.current.availableModels.map(m => m.id);
        expect(ids).toContain('vis');                  // enabled + visible → 顯示
        expect(ids).not.toContain('hid');              // enabled + hidden → 排除
        expect(ids).not.toContain('off');              // disabled → 排除
        expect(ids).toContain('sel');                  // 當前選中即使 hidden 仍保留
    });
});

// ADR-0250 A2：不寫死模型名；清單載入後選第一個「啟用、沒隱藏、非代理、不離線」的模型。
const PICK_LIST: ModelInfo[] = [
    { id: 'a-agent', name: 'A', owned_by: 'touka', enabled: true, is_agent: true },
    { id: 'b-disabled', name: 'B', owned_by: 'p', enabled: false },
    { id: 'c-hidden', name: 'C', owned_by: 'p', enabled: true, visibility: 'hidden' },
    { id: 'd-offline', name: 'D', owned_by: 'p', enabled: true, status: 'offline' },
    { id: 'e-ok', name: 'E', owned_by: 'p', enabled: true, status: 'online' },
    { id: 'f-ok', name: 'F', owned_by: 'p', enabled: true },
];

describe('pickDefaultModel（ChatProvider 與 ModelProvider 共用）', () => {
    it('跳過代理、未啟用、隱藏、離線，回傳第一個可用的一般模型', () => {
        expect(pickDefaultModel(PICK_LIST)?.id).toBe('e-ok');
        expect(pickDefaultModel(PICK_LIST.filter(m => m.id !== 'e-ok'))?.id).toBe('f-ok');
    });

    it('只有代理或都不可用時回傳 undefined（不補寫死的模型名）', () => {
        expect(pickDefaultModel(PICK_LIST.slice(0, 4))).toBeUndefined();
        expect(pickDefaultModel([])).toBeUndefined();
    });
});

describe('ModelProvider — 預設選擇不寫死模型名（ADR-0250 A2）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    function serve(models: ModelInfo[]) {
        let calls = 0;
        mockFetchTouka.mockImplementation((_url: string, path: string) => {
            if (path === '/v1/models') {
                calls++;
                return Promise.resolve({ ok: true, json: async () => ({ data: models }) });
            }
            return Promise.resolve(mockModelsResponse);
        });
        return () => calls;
    }

    it('初值是空字串；清單載入後選第一個可用的一般模型', async () => {
        serve(PICK_LIST);
        const { result } = renderHook(() => useModels(), { wrapper });
        expect(result.current.selectedModel).toBe('');
        await waitFor(() => expect(result.current.selectedModel).toBe('e-ok'));
    });

    it('已經選了模型時，重新載入清單不蓋掉選擇', async () => {
        const getCalls = serve(PICK_LIST);
        const { result } = renderHook(() => useModels(), { wrapper });
        await waitFor(() => expect(result.current.selectedModel).toBe('e-ok'));
        act(() => { result.current.setSelectedModel('f-ok'); });
        const before = getCalls();
        await act(async () => { window.dispatchEvent(new Event('touka_refresh_models')); });
        await waitFor(() => expect(getCalls()).toBeGreaterThan(before));
        expect(result.current.selectedModel).toBe('f-ok');
    });

    it('沒有可用的一般模型時維持空字串', async () => {
        const getCalls = serve(PICK_LIST.slice(0, 4));
        const { result } = renderHook(() => useModels(), { wrapper });
        await waitFor(() => expect(getCalls()).toBeGreaterThan(0));
        await waitFor(() => expect(result.current.allModels.length).toBe(4));
        expect(result.current.selectedModel).toBe('');
    });
});

describe('ModelProvider — testModel 健康探測（P2）', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockFetchTouka.mockResolvedValue(mockModelsResponse);
    });

    it('testModel POST /health 並回傳解析結果', async () => {
        mockFetchTouka.mockImplementation((_u: string, path: string, options?: RequestInit) => {
            if (path === '/v1/models/m1/health' && options?.method === 'POST') {
                return Promise.resolve({ ok: true, json: async () => ({ status: 'healthy', latency_ms: 42 }) });
            }
            return Promise.resolve(mockModelsResponse);
        });
        const { result } = renderHook(() => useModels(), { wrapper });
        await waitFor(() => expect(mockFetchTouka).toHaveBeenCalled());
        const res = await result.current.testModel('m1');
        expect(res).toEqual({ status: 'healthy', latency_ms: 42 });
    });

    it('testModel 在 !res.ok 時回傳 request_failed（不拋例外）', async () => {
        mockFetchTouka.mockImplementation((_u: string, path: string, options?: RequestInit) => {
            if (path.endsWith('/health') && options?.method === 'POST') {
                return Promise.resolve({ ok: false, status: 500 });
            }
            return Promise.resolve(mockModelsResponse);
        });
        const { result } = renderHook(() => useModels(), { wrapper });
        await waitFor(() => expect(mockFetchTouka).toHaveBeenCalled());
        const res = await result.current.testModel('m1');
        expect(res).toEqual({ status: 'unavailable', error: 'request_failed' });
    });

    it('testModel 不觸發額外的 GET /v1/models（探測不得改動設定）', async () => {
        let getModelsCallCount = 0;
        mockFetchTouka.mockImplementation((_u: string, path: string, options?: RequestInit) => {
            if (path === '/v1/models') { getModelsCallCount++; return Promise.resolve(mockModelsResponse); }
            if (path.endsWith('/health') && options?.method === 'POST') {
                return Promise.resolve({ ok: true, json: async () => ({ status: 'healthy' }) });
            }
            return Promise.resolve(mockModelsResponse);
        });
        const { result } = renderHook(() => useModels(), { wrapper });
        await waitFor(() => expect(mockFetchTouka).toHaveBeenCalled());
        const before = getModelsCallCount;
        await act(async () => { await result.current.testModel('m1'); });
        expect(getModelsCallCount).toBe(before);
    });
});
