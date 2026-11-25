/**
 * 存專案被拒絕時帶回錯誤碼與原因（ADR-0332：專用空間的登記規則），畫面才說得出為什麼；
 * 讀不到錯誤內容（代理的錯誤頁、斷線）時兩個都沒有。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { fetchTouka } from '@/lib/connections';
import { useProjects } from '../useProjects';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://proxy.test',
  fetchTouka: vi.fn(),
}));

const PROJECT = { id: 'proj-1', name: 'repo', root_path: '/app/workspace/touka-projects/repo', description: '' };

const reply = (status: number, body: unknown) =>
  new Response(typeof body === 'string' ? body : JSON.stringify(body), { status });

async function save() {
  const { result } = renderHook(() => useProjects());
  let outcome: Awaited<ReturnType<typeof result.current.saveProject>> | undefined;
  await act(async () => {
    outcome = await result.current.saveProject(PROJECT);
  });
  return outcome;
}

describe('useProjects.saveProject 的結果', () => {
  beforeEach(() => vi.mocked(fetchTouka).mockReset());

  it('成功 → ok，並重新載入清單', async () => {
    vi.mocked(fetchTouka).mockResolvedValueOnce(reply(200, { status: 'success' }));
    vi.mocked(fetchTouka).mockResolvedValueOnce(reply(200, []));
    expect(await save()).toEqual({ ok: true });
    expect(vi.mocked(fetchTouka).mock.calls.map((call) => call[1])).toEqual([
      '/v1/ui/projects/save',
      '/v1/ui/projects/list',
    ]);
  });

  it('被拒絕的形狀 → 帶回驗證錯誤與原因', async () => {
    vi.mocked(fetchTouka).mockResolvedValueOnce(
      reply(422, { detail: { code: 'VALIDATION_ERROR', id: 3001, reason: 'project_not_top_level' } }),
    );
    expect(await save()).toEqual({ ok: false, code: 'VALIDATION_ERROR', reason: 'project_not_top_level' });
  });

  it('資料夾已是別的專案 → 帶回錯誤碼', async () => {
    vi.mocked(fetchTouka).mockResolvedValueOnce(
      reply(409, { detail: { code: 'PROJECT_FOLDER_TAKEN', id: 1225 } }),
    );
    expect(await save()).toEqual({ ok: false, code: 'PROJECT_FOLDER_TAKEN', reason: undefined });
  });

  it('不是 Touka 的錯誤內容（代理的錯誤頁）→ 沒有錯誤碼與原因', async () => {
    vi.mocked(fetchTouka).mockResolvedValueOnce(reply(502, '<html>bad gateway</html>'));
    expect(await save()).toEqual({ ok: false, code: undefined, reason: undefined });
  });

  it('斷線 → 只有 ok: false', async () => {
    vi.mocked(fetchTouka).mockRejectedValueOnce(new TypeError('Failed to fetch'));
    expect(await save()).toEqual({ ok: false });
  });
});
