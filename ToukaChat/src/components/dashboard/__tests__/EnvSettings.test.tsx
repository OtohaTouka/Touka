import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import React from 'react';
import i18n from '@/lib/i18n';

// fetchTouka mock：依路徑/方法分支，模擬後端回應。
vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn((_url: string, path: string, opts?: { method?: string; body?: string }) => {
    if (path === '/v1/system/backup/schedule') {
      if (opts?.method === 'POST') {
        const body = JSON.parse(opts.body as string);
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ ...body, last_run_epoch: null }) });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          enabled: false, frequency: 'daily', time_of_day: '03:00',
          weekday: 0, retention_count: 7, last_run_epoch: null,
        }),
      });
    }
    if (path === '/v1/system/backups') {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve([
          { filename: 'backup_1.zip', size_mb: 1.2, created_at: '2026-06-22 10:00' },
        ]),
      });
    }
    if (path === '/v1/system/backups/stats') {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          count: 1, total_size_mb: 1.2, oldest: '2026-06-22 10:00',
          newest: '2026-06-22 10:00', free_disk_mb: 5000,
        }),
      });
    }
    if (path === '/v1/system/backup/run') {
      return Promise.resolve({ ok: false, status: 403 }); // F2: 非 ok 失敗
    }
    if (path === '/v1/system/restore/run') {
      return Promise.resolve({ ok: false, status: 404 }); // F3: 檔案不存在
    }
    if (path.endsWith('/download')) {
      return Promise.resolve({ ok: true, blob: () => Promise.resolve(new Blob(['enc-bytes'])) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve([]) }); // DELETE fallback
  }),
}));

vi.mock('sonner', () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}));

// confirmToast 直接執行回呼，便於測「確認後刪除」流程（無需掛 Toaster）。
vi.mock('@/hooks/useConfirmToast', () => ({
  useConfirmToast: () => ({ confirmToast: (_m: string, cb: () => void) => cb() }),
}));

import { EnvSettings } from '../EnvSettings';
import { fetchTouka } from '@/lib/connections';
import { toast } from 'sonner';

const t = (k: string, opts?: Record<string, unknown>) => i18n.t(k, opts);

describe('EnvSettings P1-D 回饋', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('F2：建立備份非 ok（403）時跳錯誤 toast（不再靜默）', async () => {
    render(<EnvSettings />);
    const btn = await screen.findByText(t('dashboard.env_settings.create_backup_now'));
    fireEvent.click(btn);
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
  });

  it('F3：還原失敗（404）時跳錯誤 toast（依錯誤碼分流，非一律權限）', async () => {
    render(<EnvSettings />);
    const restoreBtn = await screen.findByText(t('dashboard.env_settings.restore_this_version'));
    fireEvent.click(restoreBtn);
    // P1a：須先輸入密碼，確認鈕才啟用
    fireEvent.change(
      screen.getByPlaceholderText(t('dashboard.env_settings.confirm_restore_password_placeholder')),
      { target: { value: 'mypassword' } },
    );
    const confirmBtn = await screen.findByText(t('dashboard.env_settings.confirm_restore_action'));
    fireEvent.click(confirmBtn);
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
  });

  it('I3: 還原確認框以 <Trans> 帶入檔名（不再硬寫拼接）', async () => {
    render(<EnvSettings />);
    const restoreBtn = await screen.findByText(t('dashboard.env_settings.restore_this_version'));
    fireEvent.click(restoreBtn);
    expect(await screen.findByText(/backup_1\.zip/, { selector: 'span' })).toBeTruthy();
  });

  it('A4: 重新整理鈕有 aria-label', async () => {
    render(<EnvSettings />);
    expect(await screen.findByLabelText(t('dashboard.env_settings.refresh_backups_aria'))).toBeTruthy();
  });

  it('mount 時抓取備份清單（GET /v1/system/backups）', async () => {
    render(<EnvSettings />);
    await waitFor(() =>
      expect(fetchTouka).toHaveBeenCalledWith('http://localhost:8000', '/v1/system/backups'),
    );
  });
});

describe('EnvSettings 軌1 Phase 1', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('P1a: 還原確認鈕在未輸入密碼前停用、輸入後啟用', async () => {
    render(<EnvSettings />);
    fireEvent.click(await screen.findByText(t('dashboard.env_settings.restore_this_version')));
    const confirmBtn = (await screen.findByText(t('dashboard.env_settings.confirm_restore_action'))).closest('button')!;
    expect(confirmBtn.disabled).toBe(true);
    fireEvent.change(
      screen.getByPlaceholderText(t('dashboard.env_settings.confirm_restore_password_placeholder')),
      { target: { value: 'pw' } },
    );
    expect(confirmBtn.disabled).toBe(false);
  });

  it('P1a: 還原請求帶 current_password', async () => {
    render(<EnvSettings />);
    fireEvent.click(await screen.findByText(t('dashboard.env_settings.restore_this_version')));
    fireEvent.change(
      screen.getByPlaceholderText(t('dashboard.env_settings.confirm_restore_password_placeholder')),
      { target: { value: 'secret-pw' } },
    );
    fireEvent.click(await screen.findByText(t('dashboard.env_settings.confirm_restore_action')));
    await waitFor(() => {
      const call = vi.mocked(fetchTouka).mock.calls.find(c => c[1] === '/v1/system/restore/run');
      expect(call).toBeTruthy();
      expect(JSON.parse((call![2] as { body: string }).body).current_password).toBe('secret-pw');
    });
  });

  it('P1b: 刪除備份 → 確認後打 DELETE 並刷新', async () => {
    render(<EnvSettings />);
    const delBtn = await screen.findByLabelText(
      t('dashboard.env_settings.delete_backup_aria', { filename: 'backup_1.zip' }),
    );
    fireEvent.click(delBtn);
    await waitFor(() => {
      const call = vi.mocked(fetchTouka).mock.calls.find(c => c[1] === '/v1/system/backups/backup_1.zip');
      expect(call).toBeTruthy();
      expect((call![2] as { method: string }).method).toBe('DELETE');
    });
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
  });

  it('P1c: 顯示備份統計列（數量）', async () => {
    render(<EnvSettings />);
    expect(await screen.findByText(t('dashboard.env_settings.stats_backups', { count: 1 }))).toBeTruthy();
  });
});

describe('EnvSettings 軌1 Phase 2 — 加密下載（ADR-0108）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // jsdom 無 URL.createObjectURL / blob anchor 下載 → 樁住。
    URL.createObjectURL = vi.fn(() => 'blob:mock');
    URL.revokeObjectURL = vi.fn();
  });

  it('P2: 下載確認鈕在密碼短語 <8 字前停用、≥8 後啟用', async () => {
    render(<EnvSettings />);
    fireEvent.click(await screen.findByLabelText(
      t('dashboard.env_settings.download_backup_aria', { filename: 'backup_1.zip' }),
    ));
    const dlBtn = (await screen.findByText(t('dashboard.env_settings.download_action'))).closest('button')!;
    expect(dlBtn.disabled).toBe(true);
    fireEvent.change(
      screen.getByPlaceholderText(t('dashboard.env_settings.download_passphrase_placeholder')),
      { target: { value: 'short' } },
    );
    expect(dlBtn.disabled).toBe(true); // 5 字仍停用
    fireEvent.change(
      screen.getByPlaceholderText(t('dashboard.env_settings.download_passphrase_placeholder')),
      { target: { value: 'long-enough-pass' } },
    );
    expect(dlBtn.disabled).toBe(false);
  });

  it('P2: 確認後 POST 帶 passphrase 並觸發 blob 下載', async () => {
    render(<EnvSettings />);
    fireEvent.click(await screen.findByLabelText(
      t('dashboard.env_settings.download_backup_aria', { filename: 'backup_1.zip' }),
    ));
    fireEvent.change(
      screen.getByPlaceholderText(t('dashboard.env_settings.download_passphrase_placeholder')),
      { target: { value: 'long-enough-pass' } },
    );
    fireEvent.click(await screen.findByText(t('dashboard.env_settings.download_action')));
    await waitFor(() => {
      const call = vi.mocked(fetchTouka).mock.calls.find(c => c[1] === '/v1/system/backups/backup_1.zip/download');
      expect(call).toBeTruthy();
      expect(JSON.parse((call![2] as { body: string }).body).passphrase).toBe('long-enough-pass');
    });
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalled());
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
  });
});

describe('EnvSettings 軌1 Phase 3 — 自動備份排程（ADR-0109）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('P3: 顯示排程面板（啟用勾選框）', async () => {
    render(<EnvSettings />);
    expect(await screen.findByLabelText(t('dashboard.env_settings.schedule_enable'))).toBeTruthy();
  });

  it('P3: 勾選啟用後儲存 → POST 帶 enabled=true', async () => {
    render(<EnvSettings />);
    const chk = await screen.findByLabelText(t('dashboard.env_settings.schedule_enable'));
    fireEvent.click(chk);
    fireEvent.click(screen.getByText(t('dashboard.env_settings.schedule_save')));
    await waitFor(() => {
      const call = vi.mocked(fetchTouka).mock.calls.find(
        c => c[1] === '/v1/system/backup/schedule' && c[2]?.method === 'POST',
      );
      expect(call).toBeTruthy();
      expect(JSON.parse(call![2]!.body as string).enabled).toBe(true);
    });
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
  });
});

describe('EnvSettings H1 — 還原後重整前先查狀態', () => {
  const baseFetch = vi.mocked(fetchTouka).getMockImplementation()!;
  const realLocation = window.location;
  let reload: ReturnType<typeof vi.fn>;

  // 依序回應每次狀態查詢；'down' 模擬伺服器重啟中連不上。
  const statusSequence = (...outcomes: Array<'idle' | 'running' | 'failed' | 'down'>) => {
    vi.mocked(fetchTouka).mockImplementation((url: string, path: string, opts?: RequestInit) => {
      if (path === '/v1/system/restore/run') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ status: 'accepted' }) } as Response);
      }
      if (path === '/v1/system/restore/status') {
        const next = outcomes.shift() ?? 'idle';
        if (next === 'down') return Promise.reject(new TypeError('Failed to fetch'));
        const error_code = next === 'failed' ? 'SYSTEM_BACKUP_FAILED' : null;
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ state: next, error_code }) } as Response);
      }
      return baseFetch(url, path, opts);
    });
  };

  const startRestore = async () => {
    render(<EnvSettings />);
    fireEvent.click(await screen.findByText(t('dashboard.env_settings.restore_this_version')));
    fireEvent.change(
      screen.getByPlaceholderText(t('dashboard.env_settings.confirm_restore_password_placeholder')),
      { target: { value: 'pw' } },
    );
    const confirm = await screen.findByText(t('dashboard.env_settings.confirm_restore_action'));
    vi.useFakeTimers();
    fireEvent.click(confirm);
  };
  const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
  const statusCalls = () => vi.mocked(fetchTouka).mock.calls.filter(c => c[1] === '/v1/system/restore/status');

  beforeEach(() => {
    vi.clearAllMocks();
    reload = vi.fn();
    Object.defineProperty(window, 'location', { value: { ...realLocation, reload }, writable: true, configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.mocked(fetchTouka).mockImplementation(baseFetch);
    Object.defineProperty(window, 'location', { value: realLocation, writable: true, configurable: true });
  });

  it('還原失敗：跳錯誤、不自動重整、回到備份清單', async () => {
    statusSequence('failed');
    await startRestore();
    await advance(15_000);
    await advance(60_000);
    expect(toast.error).toHaveBeenCalledWith(t('dashboard.env_settings.restore_failed'), expect.anything());
    expect(reload).not.toHaveBeenCalled();
    expect(statusCalls()).toHaveLength(1);
    expect(screen.getByText(t('dashboard.env_settings.restore_this_version'))).toBeTruthy();
  });

  it('還原還在跑：先不重整、5 秒後再查，伺服器重啟後才重整', async () => {
    statusSequence('running', 'down');
    await startRestore();
    await advance(15_000);
    expect(reload).not.toHaveBeenCalled();
    await advance(5_000);
    expect(statusCalls()).toHaveLength(2);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it.each(['idle', 'down'] as const)('成功後重啟或查不到（%s）：照舊重整、不跳錯誤', async (outcome) => {
    statusSequence(outcome);
    await startRestore();
    await advance(14_000);
    expect(statusCalls()).toHaveLength(0);
    await advance(1_000);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(toast.error).not.toHaveBeenCalled();
  });
});
