/**
 * Step3ApiKeys（精靈金鑰輸入，#4 批次 1）—— 安全：永不回填既有金鑰。
 * root-cause-C：apply 失敗（res.ok=false 或 throw）時不得默默前進，須顯示錯誤並停在本步。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';
import { Step3ApiKeys } from '../Step3ApiKeys';
import type { SetupStatus } from '@/hooks/useSetupStatus';
import { PROVIDER_KEYS } from '@/lib/providerKeys';
import { fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn(),
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

const status: SetupStatus = {
  dispatch_token_is_default: false,
  auth_secret_persisted: true,
  api_keys_configured: ['anthropic'], // 已設定者亦不得回填
  is_admin_temp_password: false,
  data_path_is_default: false,
  workspace_root_is_default: false,
  embedding_model: 'all-MiniLM-L6-v2',
};

describe('Step3ApiKeys 精靈金鑰輸入（永不回填＋不吞失敗）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('所有輸入框一律留空（含已設定的 anthropic）', () => {
    render(<Step3ApiKeys status={status} onNext={vi.fn()} />);
    const pwds = document.querySelectorAll('input[type="password"]');
    expect(pwds.length).toBe(PROVIDER_KEYS.length);
    for (const el of Array.from(pwds)) {
      expect((el as HTMLInputElement).value).toBe('');
    }
  });

  it('有輸入且成功 → POST /v1/config/env 後 onNext', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
    const onNext = vi.fn();
    render(<Step3ApiKeys status={status} onNext={onNext} />);
    const pwds = document.querySelectorAll('input[type="password"]');
    // index 1 = openai（PROVIDER_KEYS 順序：anthropic, openai, ...）
    fireEvent.change(pwds[1], { target: { value: 'sk-openai' } });
    fireEvent.click(screen.getByRole('button'));
    await waitFor(() => expect(onNext).toHaveBeenCalledOnce());
    expect(fetchTouka).toHaveBeenCalledWith(
      'http://localhost:8000',
      '/v1/config/env',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('有輸入但套用失敗（res.ok=false）→ 不前進並顯示 detail', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({ detail: '沒有權限' }),
    });
    const onNext = vi.fn();
    render(<Step3ApiKeys status={status} onNext={onNext} />);
    const pwds = document.querySelectorAll('input[type="password"]');
    fireEvent.change(pwds[1], { target: { value: 'sk-openai' } });
    fireEvent.click(screen.getByRole('button'));
    expect(await screen.findByText('沒有權限')).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
  });

  it('網路錯誤（throw）→ 不前進並記錄 SSOT 錯誤', async () => {
    (fetchTouka as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('network down'));
    const onNext = vi.fn();
    render(<Step3ApiKeys status={status} onNext={onNext} />);
    const pwds = document.querySelectorAll('input[type="password"]');
    fireEvent.change(pwds[1], { target: { value: 'sk-openai' } });
    fireEvent.click(screen.getByRole('button'));
    await waitFor(() => expect(logSSOTError).toHaveBeenCalled());
    expect(onNext).not.toHaveBeenCalled();
  });

  it('無輸入 → 直接 onNext（不 POST）', () => {
    const onNext = vi.fn();
    render(<Step3ApiKeys status={status} onNext={onNext} />);
    fireEvent.click(screen.getByRole('button'));
    expect(fetchTouka).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalledOnce();
  });
});
