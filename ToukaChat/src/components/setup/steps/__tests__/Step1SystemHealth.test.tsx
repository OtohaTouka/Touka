/**
 * Step1SystemHealth（嵌入模型）——bug①：立即下載＋進度＋切換防呆。
 * 未變更＝目前模型還沒下載就先下載（失敗可先略過），不切換；變更＝下載（輪詢進度）→切換；
 * 既有記憶時需確認（confirmation_required）。新安裝預選 e5（ADR-0314）。
 */
import { StrictMode } from 'react';
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

const E5 = 'intfloat/multilingual-e5-base';
/** 新安裝：設定檔從範例複製，寫的是 e5（ADR-0314）。 */
const e5Status: SetupStatus = { ...status, embedding_model: E5 };

function jsonRes(body: unknown, ok = true) {
  return Promise.resolve({ ok, json: () => Promise.resolve(body) });
}

function calledPaths(): string[] {
  return (fetchTouka as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[1] as string);
}

function downloadBodies(): unknown[] {
  return (fetchTouka as ReturnType<typeof vi.fn>).mock.calls
    .filter((c) => c[1] === '/v1/admin/setup/embedding/download')
    .map((c) => JSON.parse((c[2] as RequestInit).body as string));
}

/** MODEL_OPTIONS 順序：[0]=all-MiniLM、[1]=paraphrase-multilingual、[2]=e5（中文推薦）。 */
function selectMultilingual() {
  fireEvent.click(document.querySelectorAll('input[type="radio"]')[1]);
}

describe('Step1SystemHealth 嵌入模型下載＋切換防呆', () => {
  beforeEach(() => vi.clearAllMocks());

  it('模型未變更且已下載 → 不切換，直接 onNext', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockImplementation((_u: string, path: string) => {
      if (path.includes('/embedding/download-status')) return jsonRes({ status: 'done', pct: 100, cached: true });
      if (path.includes('/embedding/download')) return jsonRes({ status: 'done', pct: 100, cached: true });
      return jsonRes({});
    });
    const onNext = vi.fn();
    render(<Step1SystemHealth status={status} onNext={onNext} />);
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalledOnce());
    expect(downloadBodies()).toEqual([{ model: 'all-MiniLM-L6-v2' }]);
    expect(calledPaths().some((p) => p.includes('/embedding/switch'))).toBe(false);
  });

  it('開發模式掛上兩次（StrictMode）→ 仍會等下載完成再前進', async () => {
    const states = [{ status: 'downloading', pct: 10 }, { status: 'done', pct: 100 }];
    (fetchTouka as ReturnType<typeof vi.fn>).mockImplementation((_u: string, path: string) => {
      if (path.includes('/embedding/download-status')) return jsonRes(states.shift() ?? { status: 'done', pct: 100 });
      if (path.includes('/embedding/download')) return jsonRes({ status: 'downloading', pct: 0 });
      return jsonRes({});
    });
    const onNext = vi.fn();
    render(<StrictMode><Step1SystemHealth status={e5Status} onNext={onNext} /></StrictMode>);
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
    await waitFor(() => expect(onNext).toHaveBeenCalledOnce(), { timeout: 3000 });
    expect(screen.queryByRole('button', { name: /先略過/ })).toBeNull();
  });

  it('目前的模型還沒下載 → 先下載並顯示進度，完成後前進、不切換', async () => {
    const states = [{ status: 'downloading', pct: 40 }, { status: 'done', pct: 100 }];
    (fetchTouka as ReturnType<typeof vi.fn>).mockImplementation((_u: string, path: string) => {
      if (path.includes('/embedding/download-status')) return jsonRes(states.shift() ?? { status: 'done', pct: 100 });
      if (path.includes('/embedding/download')) return jsonRes({ status: 'downloading', pct: 0 });
      return jsonRes({});
    });
    const onNext = vi.fn();
    render(<Step1SystemHealth status={e5Status} onNext={onNext} />);
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
    await waitFor(() => expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('40'));
    expect(onNext).not.toHaveBeenCalled();
    await waitFor(() => expect(onNext).toHaveBeenCalledOnce(), { timeout: 3000 });
    const downloads = (fetchTouka as ReturnType<typeof vi.fn>).mock.calls
      .filter((c) => c[1] === '/v1/admin/setup/embedding/download');
    expect(JSON.parse(downloads[0][2].body as string)).toEqual({ model: E5 });
    expect(calledPaths().some((p) => p.includes('/embedding/switch'))).toBe(false);
  });

  it('目前的模型下載失敗 → 顯示錯誤與「先略過」，按下才前進', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockImplementation((_u: string, path: string) => {
      if (path.includes('/embedding/download-status')) return jsonRes({ status: 'error', error: '連不上網路' });
      if (path.includes('/embedding/download')) return jsonRes({ status: 'downloading', pct: 0 });
      return jsonRes({});
    });
    const onNext = vi.fn();
    render(<Step1SystemHealth status={e5Status} onNext={onNext} />);
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
    expect(await screen.findByText('連不上網路')).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /先略過/ }));
    expect(onNext).toHaveBeenCalledOnce();
    expect(calledPaths().some((p) => p.includes('/embedding/switch'))).toBe(false);
  });

  it('下載狀態回到 idle（例如後端重啟）→ 不再等待，顯示錯誤並可先略過', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockImplementation((_u: string, path: string) => {
      if (path.includes('/embedding/download-status')) return jsonRes({ status: 'idle', pct: 0 });
      if (path.includes('/embedding/download')) return jsonRes({ status: 'downloading', pct: 0 });
      return jsonRes({});
    });
    const onNext = vi.fn();
    render(<Step1SystemHealth status={e5Status} onNext={onNext} />);
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
    expect(await screen.findByText('發生錯誤')).toBeInTheDocument();
    expect(calledPaths().filter((p) => p.includes('/download-status'))).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: /先略過/ }));
    expect(onNext).toHaveBeenCalledOnce();
  });

  it('下載請求本身失敗（例如離線）→ 也能先略過', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockImplementation(() => jsonRes({}, false));
    const onNext = vi.fn();
    render(<Step1SystemHealth status={e5Status} onNext={onNext} />);
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
    fireEvent.click(await screen.findByRole('button', { name: /先略過/ }));
    expect(onNext).toHaveBeenCalledOnce();
  });

  it('下載失敗後改選別的模型 → 「先略過」消失', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockImplementation(() => jsonRes({}, false));
    render(<Step1SystemHealth status={e5Status} onNext={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
    await screen.findByRole('button', { name: /先略過/ });
    selectMultilingual();
    expect(screen.queryByRole('button', { name: /先略過/ })).toBeNull();
  });

  it('換模型時下載請求本身失敗 → 顯示錯誤、不給略過', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockImplementation(() => jsonRes({}, false));
    const onNext = vi.fn();
    render(<Step1SystemHealth status={status} onNext={onNext} />);
    selectMultilingual();
    fireEvent.click(screen.getByRole('button'));
    expect(await screen.findByText('嵌入模型儲存失敗')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /先略過/ })).toBeNull();
    expect(onNext).not.toHaveBeenCalled();
  });

  it('沒讀到設定狀態 → 不知道目前的模型，不下載，直接 onNext', () => {
    const onNext = vi.fn();
    render(<Step1SystemHealth status={null} onNext={onNext} />);
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
    expect(fetchTouka).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalledOnce();
  });

  it('選單有標成中文推薦的 e5，設定寫 e5 的新安裝預選它', () => {
    render(<Step1SystemHealth status={e5Status} onNext={vi.fn()} />);
    const e5 = document.querySelector<HTMLInputElement>(`input[value="${E5}"]`);
    expect(e5?.checked).toBe(true);
    expect(e5?.closest('label')?.textContent).toMatch(/中文推薦/);
  });

  it('舊安裝預選自己原本的模型', () => {
    render(<Step1SystemHealth status={status} onNext={vi.fn()} />);
    expect(document.querySelector<HTMLInputElement>('input[value="all-MiniLM-L6-v2"]')?.checked).toBe(true);
    expect(document.querySelector<HTMLInputElement>(`input[value="${E5}"]`)?.checked).toBe(false);
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

  /** 換成多語模型時後端要確認，回這些欄位（ADR-0315：記憶實際用的模型，不是目前設定）。 */
  async function confirmWarning(extra: Record<string, unknown>): Promise<string> {
    (fetchTouka as ReturnType<typeof vi.fn>).mockImplementation((_u: string, path: string) => {
      if (path.includes('/embedding/download-status')) return jsonRes({ status: 'done', pct: 100 });
      if (path.includes('/embedding/download')) return jsonRes({ status: 'downloading', pct: 0 });
      if (path.includes('/embedding/switch')) {
        return jsonRes({
          status: 'confirmation_required', current_model: E5,
          target_model: 'paraphrase-multilingual-MiniLM-L12-v2', ...extra,
        });
      }
      return jsonRes({});
    });
    render(<Step1SystemHealth status={e5Status} onNext={vi.fn()} />);
    selectMultilingual();
    fireEvent.click(screen.getByRole('button'));
    await screen.findByRole('button', { name: /仍要切換/ });
    return document.body.textContent ?? '';
  }

  it('警告寫記憶實際用的模型，不是目前設定的', async () => {
    const text = await confirmWarning({ index_model: 'all-MiniLM-L6-v2', index_dim: 384, index_outcome: 'mismatch' });
    expect(text).toContain('以「all-MiniLM-L6-v2」嵌入');
    expect(text).not.toContain('以「' + E5 + '」嵌入');
  });

  it('只知道向量長度不同 → 警告寫長度', async () => {
    const text = await confirmWarning({ index_model: null, index_dim: 768, index_outcome: 'mismatch' });
    expect(text).toContain('768');
    expect(text).not.toContain('看不出');
  });

  it('判斷不了時就算回了名稱也說看不出（不寫「以目標模型嵌入」這種矛盾的話）', async () => {
    const text = await confirmWarning({
      index_model: 'paraphrase-multilingual-MiniLM-L12-v2', index_dim: 384, index_outcome: 'unknown',
    });
    expect(text).toContain('看不出');
    expect(text).not.toContain('以「paraphrase-multilingual-MiniLM-L12-v2」嵌入');
  });

  it('判斷不了 → 警告說看不出', async () => {
    const text = await confirmWarning({ index_model: null, index_dim: 384, index_outcome: 'unknown' });
    expect(text).toContain('看不出');
    expect(text).toContain('paraphrase-multilingual-MiniLM-L12-v2');
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
    // 換模型失敗不能略過：略過等於沒套用使用者的選擇
    expect(screen.queryByRole('button', { name: /先略過/ })).toBeNull();
  });
});
