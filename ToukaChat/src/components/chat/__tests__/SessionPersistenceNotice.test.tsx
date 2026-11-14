import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PersistenceState } from '@/lib/session_persistence';
const h = vi.hoisted(() => ({ state: undefined as PersistenceState | undefined, retry: false, adoptable: false,
  recheck: vi.fn(), save: vi.fn(), adopt: vi.fn(), switchSession: vi.fn(), clearStale: vi.fn(),
  confirm: null as null | (() => unknown), incognito: false }));
vi.mock('@/lib/providers/SessionListProvider', () => ({ useSessionList: () => ({
  getPersistence: () => h.state, getSession: () => ({ incognito: h.incognito }),
  canRetrySave: () => h.retry, recheckSession: h.recheck, retrySaveSession: h.save,
  canAdoptStored: () => h.adoptable, adoptStoredSession: h.adopt,
}) }));
vi.mock('@/lib/providers/ChatProvider', () => ({ useChat: () => ({ handleSwitchSession: h.switchSession }) }));
vi.mock('@/lib/providers/SessionActivityProvider', () => ({ useSessionActivity: () => ({ clearStale: h.clearStale }) }));
vi.mock('@/hooks/useConfirmToast', () => ({ useConfirmToast: () => ({
  confirmToast: (_message: string, onConfirm: () => unknown) => { h.confirm = onConfirm; },
}) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
import { SessionPersistenceNotice } from '../SessionPersistenceNotice';

describe('persistence result and explicit recovery controls', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    h.state = { status: 'unknown', error_code: 1307 }; h.retry = false; h.adoptable = false; h.incognito = false; h.confirm = null;
  });
  it('unknown exposes only readback; pending readback cannot be submitted twice', async () => {
    let finish!: () => void;
    h.recheck.mockReturnValue(new Promise<void>(done => { finish = done; }));
    render(<SessionPersistenceNotice sessionId="s" />);
    expect(screen.getByRole('status')).toHaveTextContent('chat.persistence.unknown');
    expect(screen.queryByRole('button', { name: 'chat.persistence.retry' })).toBeNull();
    const button = screen.getByRole('button', { name: 'chat.persistence.recheck' });
    fireEvent.click(button); fireEvent.click(button);
    expect(h.recheck).toHaveBeenCalledTimes(1);
    expect(h.save).not.toHaveBeenCalled();
    expect(button).toBeDisabled();
    await act(async () => { finish(); });
  });
  it('only a confirmed conflict permits an explicit save-only retry', async () => {
    h.state = { status: 'failed', error_code: 1307 }; h.retry = true; h.adoptable = true;
    h.save.mockResolvedValue({ status: 'saved', representation: 'full', revision: 'r' });
    render(<SessionPersistenceNotice sessionId="s" />);
    expect(screen.queryByRole('button', { name: 'chat.persistence.adopt' })).toBeNull();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'chat.persistence.retry' })); });
    expect(h.save).toHaveBeenCalledWith('s');
    expect(h.recheck).not.toHaveBeenCalled();
  });
  // 存檔有別人的新內容（ADR-0313）：不給重試，改用存檔內容要先確認，換上後重新載入這段對話
  it('stored content from elsewhere offers only adopting it, after a confirmation', async () => {
    h.state = { status: 'failed', error_code: 1307 }; h.adoptable = true;
    h.adopt.mockResolvedValue({ status: 'saved', representation: 'full', revision: 'r2' });
    render(<SessionPersistenceNotice sessionId="s" />);
    expect(screen.queryByRole('button', { name: 'chat.persistence.retry' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'chat.persistence.adopt' }));
    expect(h.adopt).not.toHaveBeenCalled();
    await act(async () => { await h.confirm!(); });
    expect(h.adopt).toHaveBeenCalledWith('s');
    expect(h.clearStale).toHaveBeenCalled();
    expect(h.switchSession).toHaveBeenCalledWith('s');
  });
  it('a failed adoption keeps the page as it is', async () => {
    h.state = { status: 'failed', error_code: 1307 }; h.adoptable = true;
    h.adopt.mockResolvedValue({ status: 'unknown', error_code: 1302 });
    render(<SessionPersistenceNotice sessionId="s" />);
    fireEvent.click(screen.getByRole('button', { name: 'chat.persistence.adopt' }));
    await act(async () => { await h.confirm!(); });
    expect(h.switchSession).not.toHaveBeenCalled();
    expect(h.clearStale).not.toHaveBeenCalled();
  });
  it('incognito never displays persistence recovery controls', () => {
    h.incognito = true;
    render(<SessionPersistenceNotice sessionId="s" />);
    expect(screen.queryByRole('status')).toBeNull();
  });
});
