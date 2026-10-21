/**
 * Step5DataPath（資料目錄＋工作區根目錄）——bug② 修復：兩個宿主路徑皆可設定，
 * 送出 data_path / workspace_root；失敗（含 ToukaException 物件 detail）顯示訊息且不前進。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { Step5DataPath } from '../Step5DataPath';
import type { SetupStatus } from '@/hooks/useSetupStatus';
import { fetchTouka } from '@/lib/connections';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn(),
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

const status: SetupStatus = {
  dispatch_token_is_default: false,
  auth_secret_persisted: true,
  api_keys_configured: [],
  is_admin_temp_password: false,
  data_path_is_default: true,
  workspace_root_is_default: true,
  embedding_model: 'all-MiniLM-L6-v2',
};

describe('Step5DataPath 雙欄位（資料目錄＋工作區根目錄）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('顯示資料目錄與工作區根目錄兩個欄位', () => {
    render(<Step5DataPath status={status} onNext={vi.fn()} />);
    expect(document.querySelector('#data-path')).toBeInTheDocument();
    expect(document.querySelector('#workspace-root')).toBeInTheDocument();
  });

  it('兩者皆空 → 直接 onNext（不 POST）', () => {
    const onNext = vi.fn();
    render(<Step5DataPath status={status} onNext={onNext} />);
    fireEvent.click(screen.getByRole('button'));
    expect(fetchTouka).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalledOnce();
  });

  it('填兩者且成功 → POST 帶 data_path 與 workspace_root 後 onNext', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
    const onNext = vi.fn();
    render(<Step5DataPath status={status} onNext={onNext} />);
    fireEvent.change(document.querySelector('#data-path')!, { target: { value: 'D:/Data' } });
    fireEvent.change(document.querySelector('#workspace-root')!, { target: { value: 'D:/Work' } });
    fireEvent.click(screen.getByRole('button'));
    await waitFor(() => expect(onNext).toHaveBeenCalledOnce());
    const opts = (fetchTouka as ReturnType<typeof vi.fn>).mock.calls[0][2];
    const sent = JSON.parse((opts as RequestInit).body as string);
    expect(sent).toEqual({ data_path: 'D:/Data', workspace_root: 'D:/Work' });
  });

  it('只填工作區根目錄 → POST 只帶 workspace_root', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
    render(<Step5DataPath status={status} onNext={vi.fn()} />);
    fireEvent.change(document.querySelector('#workspace-root')!, { target: { value: 'D:/Work' } });
    fireEvent.click(screen.getByRole('button'));
    await waitFor(() => expect(fetchTouka).toHaveBeenCalledOnce());
    const opts = (fetchTouka as ReturnType<typeof vi.fn>).mock.calls[0][2];
    expect(JSON.parse((opts as RequestInit).body as string)).toEqual({ workspace_root: 'D:/Work' });
  });

  it('失敗（ToukaException 物件 detail）→ 顯示 message 且不前進', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({ detail: { code: 'VALIDATION_ERROR', message: '路徑必須是既有目錄' } }),
    });
    const onNext = vi.fn();
    render(<Step5DataPath status={status} onNext={onNext} />);
    fireEvent.change(document.querySelector('#data-path')!, { target: { value: 'D:/None' } });
    fireEvent.click(screen.getByRole('button'));
    expect(await screen.findByText('路徑必須是既有目錄')).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
  });
});
