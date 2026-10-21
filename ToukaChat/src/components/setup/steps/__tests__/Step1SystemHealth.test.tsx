/**
 * Step1SystemHealth（嵌入模型）——bug①：立即下載＋進度＋切換防呆。
 * 未變更＝直接前進；變更＝下載（輪詢進度）→切換；既有記憶時需確認（confirmation_required）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { Step1SystemHealth } from '../Step1SystemHealth';
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
  api_keys_configured: ['anthropic'],
  is_admin_temp_password: false,
  data_path_is_default: false,
  workspace_root_is_default: false,
  embedding_model: 'all-MiniLM-L6-v2',
};

function jsonRes(body: unknown, ok = true) {
  return Promise.resolve({ ok, json: () => Promise.resolve(body) });
}

/** MODEL_OPTIONS 順序：[0]=all-MiniLM（預設）、[1]=paraphrase-multilingual。 */
function selectMultilingual() {
  fireEvent.click(document.querySelectorAll('input[type="radio"]')[1]);
}

describe('Step1SystemHealth 嵌入模型下載＋切換防呆', () => {
  beforeEach(() => vi.clearAllMocks());

  it('模型未變更 → 不呼叫任何端點，直接 onNext', () => {
    const onNext = vi.fn();
    render(<Step1SystemHealth status={status} onNext={onNext} />);
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
    expect(fetchTouka).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalledOnce();
  });

  it('變更模型 → 下載完成→切換成功→onNext', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockImplementation((_u: string, path: string) => {
      if (path.includes('/embedding/download-status')) return jsonRes({ status: 'done', pct: 100 });
      if (path.includes('/embedding/download')) return jsonRes({ status: 'downloading', pct: 0 });
      if (path.includes('/embedding/switch')) return jsonRes({ status: 'ok' });
      return jsonRes({});
    });
    const onNext = vi.fn();
    render(<Step1SystemHealth status={status} onNext={onNext} />);
    selectMultilingual();
    fireEvent.click(screen.getByRole('button'));
    await waitFor(() => expect(onNext).toHaveBeenCalledOnce());
    const paths = (fetchTouka as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[1] as string);
    expect(paths.some((p) => p.includes('/embedding/download'))).toBe(true);
    expect(paths.some((p) => p.includes('/embedding/switch'))).toBe(true);
  });

  it('既有記憶需確認 → 顯示警告、不前進；按「仍要切換」→ force 切換→onNext', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockImplementation((_u: string, path: string, opts?: RequestInit) => {
      if (path.includes('/embedding/download-status')) return jsonRes({ status: 'done', pct: 100 });
      if (path.includes('/embedding/download')) return jsonRes({ status: 'downloading', pct: 0 });
      if (path.includes('/embedding/switch')) {
        const force = JSON.parse((opts?.body as string) ?? '{}').force;
        return jsonRes(force
          ? { status: 'ok' }
          : { status: 'confirmation_required', current_model: 'all-MiniLM-L6-v2', target_model: 'paraphrase-multilingual-MiniLM-L12-v2' });
      }
      return jsonRes({});
    });
    const onNext = vi.fn();
    render(<Step1SystemHealth status={status} onNext={onNext} />);
    selectMultilingual();
    fireEvent.click(screen.getByRole('button'));
    const confirmBtn = await screen.findByRole('button', { name: /仍要切換/ });
    expect(onNext).not.toHaveBeenCalled();
    fireEvent.click(confirmBtn);
    await waitFor(() => expect(onNext).toHaveBeenCalledOnce());
  });

  it('下載失敗（status error）→ 顯示錯誤、不前進', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockImplementation((_u: string, path: string) => {
      if (path.includes('/embedding/download-status')) return jsonRes({ status: 'error', error: '下載失敗了' });
      if (path.includes('/embedding/download')) return jsonRes({ status: 'downloading', pct: 0 });
      return jsonRes({});
    });
    const onNext = vi.fn();
    render(<Step1SystemHealth status={status} onNext={onNext} />);
    selectMultilingual();
    fireEvent.click(screen.getByRole('button'));
    expect(await screen.findByText('下載失敗了')).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
  });
});
