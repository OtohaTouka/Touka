import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PersistenceState } from '@/lib/session_persistence';
const h = vi.hoisted(() => ({ state: undefined as PersistenceState | undefined, retry: false,
  recheck: vi.fn(), save: vi.fn(), incognito: false }));
vi.mock('@/lib/providers/SessionListProvider', () => ({ useSessionList: () => ({
  getPersistence: () => h.state, getSession: () => ({ incognito: h.incognito }),
  canRetrySave: () => h.retry, recheckSession: h.recheck, retrySaveSession: h.save,
}) }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
import { SessionPersistenceNotice } from '../SessionPersistenceNotice';

describe('persistence result and explicit recovery controls', () => {
  beforeEach(() => { vi.clearAllMocks(); h.state = { status: 'unknown', error_code: 1307 }; h.retry = false; h.incognito = false; });
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
    h.state = { status: 'failed', error_code: 1307 }; h.retry = true;
    h.save.mockResolvedValue({ status: 'saved', representation: 'full', revision: 'r' });
    render(<SessionPersistenceNotice sessionId="s" />);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'chat.persistence.retry' })); });
    expect(h.save).toHaveBeenCalledWith('s');
    expect(h.recheck).not.toHaveBeenCalled();
  });
  it('incognito never displays persistence recovery controls', () => {
    h.incognito = true;
    render(<SessionPersistenceNotice sessionId="s" />);
    expect(screen.queryByRole('status')).toBeNull();
  });
});
