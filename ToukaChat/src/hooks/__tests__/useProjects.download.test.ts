/**
 * 下載專案（ADR-0332 PR B）：經 fetchTouka 帶登入拿 zip，交給瀏覽器存檔（不用帶 token 的連結）；
 * 被拒絕時帶回錯誤碼與原因。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { fetchTouka } from '@/lib/connections';
import { useProjects } from '../useProjects';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://proxy.test',
  fetchTouka: vi.fn(),
}));
vi.mock('@/lib/time_utils', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/time_utils')>()),
  getSystemNow: () => new Date('2026-11-25T08:00:00Z'),
}));

const PROJECT = {
  id: 'proj 1',
  name: 'my/repo',
  root_path: '/app/workspace/touka-projects/repo',
  description: '',
  updatedAt: 0,
};

const reply = (status: number, body: BodyInit) => new Response(body, { status });

async function download() {
  const { result } = renderHook(() => useProjects());
  let outcome: Awaited<ReturnType<typeof result.current.downloadProject>> | undefined;
  await act(async () => {
    outcome = await result.current.downloadProject(PROJECT);
  });
  return outcome;
}

describe('useProjects.downloadProject', () => {
  const clicked: string[] = [];
  let clickSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.mocked(fetchTouka).mockReset();
    clicked.length = 0;
    URL.createObjectURL = vi.fn(() => 'blob:zip');
    URL.revokeObjectURL = vi.fn();
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push(`${this.href}|${this.download}`);
    });
  });

  afterEach(() => clickSpy.mockRestore());

  it('成功 → 用 fetchTouka 拿 zip，存成安全的檔名，用完收回網址', async () => {
    vi.mocked(fetchTouka).mockResolvedValueOnce(reply(200, new Blob(['PK'])));
    expect(await download()).toEqual({ ok: true });
    expect(vi.mocked(fetchTouka).mock.calls[0][1]).toBe('/v1/ui/projects/proj%201/download');
    expect(clicked).toEqual(['blob:zip|my_repo-2026-11-25.zip']);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:zip');
  });

  it('被拒絕 → 帶回錯誤碼與原因，不存檔', async () => {
    vi.mocked(fetchTouka).mockResolvedValueOnce(
      reply(413, JSON.stringify({ detail: { code: 'REQUEST_TOO_LARGE', id: 4001, reason: 'too_large' } })),
    );
    expect(await download()).toEqual({ ok: false, code: 'REQUEST_TOO_LARGE', reason: 'too_large' });
    expect(clicked).toEqual([]);
  });

  it('同時只能一個 → 帶回錯誤碼', async () => {
    vi.mocked(fetchTouka).mockResolvedValueOnce(
      reply(429, JSON.stringify({ detail: { code: 'PROJECT_DOWNLOAD_BUSY', id: 1228 } })),
    );
    expect(await download()).toEqual({ ok: false, code: 'PROJECT_DOWNLOAD_BUSY', reason: undefined });
  });

  it('斷線 → 只有 ok: false', async () => {
    vi.mocked(fetchTouka).mockRejectedValueOnce(new TypeError('Failed to fetch'));
    expect(await download()).toEqual({ ok: false });
    expect(clicked).toEqual([]);
  });
});
