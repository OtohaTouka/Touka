/**
 * 修復 C：useProjects.fetchProjects 必須維持「projects 永遠是陣列」的 SSOT 不變式。
 *
 * 背景：後端 /v1/ui/projects/list 對任一非 2xx（401 token 過期 / 403 權限 / 500）會回
 * 合法 JSON 錯誤物件 {detail:{...}}（ToukaException）。舊版 fetchProjects 未檢查 res.ok
 * 就 setProjects(物件)，使 projects 變成非陣列，導致 ProjectsView 的 projects.map 在
 * render 期丟 "projects.map is not a function" → 整個專案頁白屏。
 *
 * 不變式（單一資料來源 hook 內收斂）：fetchProjects 結束後 projects 必為陣列。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useProjects } from '@/hooks/useProjects';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://x',
  fetchTouka: vi.fn(),
}));
vi.mock('@/lib/error_handler', () => ({
  logSSOTError: vi.fn(),
}));

import { fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';

const mockFetch = fetchTouka as unknown as ReturnType<typeof vi.fn>;
const mockLog = logSSOTError as unknown as ReturnType<typeof vi.fn>;

const sampleProjects = [
  { id: 'proj-1', name: 'A', root_path: '/x', description: '', updatedAt: 1 },
];

describe('useProjects.fetchProjects（修復 C：陣列不變式）', () => {
  beforeEach(() => {
    mockFetch.mockReset();
    mockLog.mockReset();
  });

  it('成功且回陣列：projects 設為該陣列、isLoading 收斂為 false、不記錯誤', async () => {
    mockFetch.mockResolvedValue({ ok: true, status: 200, json: async () => sampleProjects });
    const { result } = renderHook(() => useProjects());
    await act(async () => {
      await result.current.fetchProjects();
    });
    expect(result.current.projects).toEqual(sampleProjects);
    expect(result.current.isLoading).toBe(false);
    expect(mockLog).not.toHaveBeenCalled();
  });

  it('後端 403 回 JSON 錯誤物件：projects 不被汙染成非陣列、記錄錯誤', async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({ detail: { code: 'AUTH_PERMISSION_DENIED', message: 'nope' } }),
    });
    const { result } = renderHook(() => useProjects());
    await act(async () => {
      await result.current.fetchProjects();
    });
    expect(Array.isArray(result.current.projects)).toBe(true);
    expect(result.current.projects).toEqual([]);
    expect(result.current.isLoading).toBe(false);
    expect(mockLog).toHaveBeenCalled();
  });

  it('200 但 body 非陣列（防禦）：強制收斂為空陣列', async () => {
    mockFetch.mockResolvedValue({ ok: true, status: 200, json: async () => ({ detail: 'oops' }) });
    const { result } = renderHook(() => useProjects());
    await act(async () => {
      await result.current.fetchProjects();
    });
    expect(Array.isArray(result.current.projects)).toBe(true);
    expect(result.current.projects).toEqual([]);
  });

  it('錯誤不抹除前次成功載入的清單（保留最後良好狀態）', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => sampleProjects });
    const { result } = renderHook(() => useProjects());
    await act(async () => {
      await result.current.fetchProjects();
    });
    expect(result.current.projects).toEqual(sampleProjects);

    mockFetch.mockResolvedValueOnce({ ok: false, status: 500, json: async () => ({ detail: 'err' }) });
    await act(async () => {
      await result.current.fetchProjects();
    });
    expect(result.current.projects).toEqual(sampleProjects);
    expect(Array.isArray(result.current.projects)).toBe(true);
  });

  it('fetchTouka 連線層拋例外：記錄錯誤且 projects 維持陣列、isLoading 收斂', async () => {
    mockFetch.mockRejectedValue(new Error('network down'));
    const { result } = renderHook(() => useProjects());
    await act(async () => {
      await result.current.fetchProjects();
    });
    expect(Array.isArray(result.current.projects)).toBe(true);
    expect(mockLog).toHaveBeenCalled();
    expect(result.current.isLoading).toBe(false);
  });
});

describe('useProjects save/delete（修復 D：誠實回傳成敗）', () => {
  beforeEach(() => {
    mockFetch.mockReset();
    mockLog.mockReset();
  });

  const draft = { id: 'p1', name: 'A', root_path: '/x', description: '' };

  it('saveProject：後端 403/422/500 → 回傳 false（不再佯裝成功）', async () => {
    mockFetch.mockImplementation((_url: string, path: string) =>
      path === '/v1/ui/projects/save'
        ? Promise.resolve({ ok: false, status: 403, json: async () => ({ detail: {} }) })
        : Promise.resolve({ ok: true, status: 200, json: async () => [] }),
    );
    const { result } = renderHook(() => useProjects());
    let ret: boolean | undefined;
    await act(async () => {
      ret = await result.current.saveProject(draft);
    });
    expect(ret).toBe(false);
  });

  it('saveProject：成功 → 回傳 true', async () => {
    mockFetch.mockResolvedValue({ ok: true, status: 200, json: async () => [] });
    const { result } = renderHook(() => useProjects());
    let ret: boolean | undefined;
    await act(async () => {
      ret = await result.current.saveProject(draft);
    });
    expect(ret).toBe(true);
  });

  it('deleteProject：後端 403 → 回傳 false（不再佯裝成功）', async () => {
    mockFetch.mockImplementation((_url: string, path: string) =>
      path === '/v1/ui/projects/p1'
        ? Promise.resolve({ ok: false, status: 403 })
        : Promise.resolve({ ok: true, status: 200, json: async () => [] }),
    );
    const { result } = renderHook(() => useProjects());
    let ret: boolean | undefined;
    await act(async () => {
      ret = await result.current.deleteProject('p1');
    });
    expect(ret).toBe(false);
  });

  it('deleteProject：成功 → 回傳 true', async () => {
    mockFetch.mockResolvedValue({ ok: true, status: 200, json: async () => [] });
    const { result } = renderHook(() => useProjects());
    let ret: boolean | undefined;
    await act(async () => {
      ret = await result.current.deleteProject('p1');
    });
    expect(ret).toBe(true);
  });
});
