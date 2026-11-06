/**
 * ADR-0292：無痕對話的內容也會交給 hook。有這種 hook 才提示；一般對話不查也不提示；
 * 送出訊息後（recheckKey 變了）再查一次，hook 在對話中途打開時下一則就提示。
 */
import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({ incognito: true, fetch: vi.fn() }));
vi.mock('@/lib/providers/SessionListProvider', () => ({
  useSessionList: () => ({ getSession: () => ({ incognito: h.incognito }) }),
}));
vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://proxy.test',
  fetchTouka: h.fetch,
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
import { IncognitoHookNotice } from '../IncognitoHookNotice';

const NOTICE = 'chat.incognito_hook_notice';

function answers(active: boolean) {
  return { ok: true, json: async () => ({ active }) };
}

describe('IncognitoHookNotice', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    h.incognito = true;
    h.fetch.mockResolvedValue(answers(true));
  });

  it('無痕對話、有 hook 時提示', async () => {
    render(<IncognitoHookNotice sessionId="s" recheckKey={0} />);
    expect(await screen.findByRole('status')).toHaveTextContent(NOTICE);
    expect(h.fetch).toHaveBeenCalledWith('http://proxy.test', '/v1/hooks/active');
  });

  it('無痕對話、沒有 hook 時不提示', async () => {
    h.fetch.mockResolvedValue(answers(false));
    render(<IncognitoHookNotice sessionId="s" recheckKey={0} />);
    await waitFor(() => expect(h.fetch).toHaveBeenCalledTimes(1));
    await act(async () => {});
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('一般對話不查也不提示', async () => {
    h.incognito = false;
    render(<IncognitoHookNotice sessionId="s" recheckKey={0} />);
    await act(async () => {});
    expect(h.fetch).not.toHaveBeenCalled();
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('hook 在對話中途打開時，送出訊息後就提示', async () => {
    h.fetch.mockResolvedValue(answers(false));
    const { rerender } = render(<IncognitoHookNotice sessionId="s" recheckKey={false} />);
    await waitFor(() => expect(h.fetch).toHaveBeenCalledTimes(1));
    h.fetch.mockResolvedValue(answers(true));
    rerender(<IncognitoHookNotice sessionId="s" recheckKey={true} />);
    expect(await screen.findByRole('status')).toHaveTextContent(NOTICE);
  });

  it('查不到時維持上一次的結果', async () => {
    const { rerender } = render(<IncognitoHookNotice sessionId="s" recheckKey={false} />);
    await screen.findByRole('status');
    h.fetch.mockResolvedValue({ ok: false, json: async () => ({}) });
    rerender(<IncognitoHookNotice sessionId="s" recheckKey={true} />);
    await waitFor(() => expect(h.fetch).toHaveBeenCalledTimes(2));
    await act(async () => {});
    expect(screen.getByRole('status')).toBeInTheDocument();
  });
});
