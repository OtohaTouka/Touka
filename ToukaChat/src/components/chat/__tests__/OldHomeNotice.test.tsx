import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const handleSetHome = vi.fn();
vi.mock('@/lib/providers/ChatProvider', () => ({
  useChat: () => ({ handleSetHome, handleUnsetHome: vi.fn() }),
}));

import { OldHomeNotice } from '../OldHomeNotice';

describe('OldHomeNotice（ADR-0306）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('設為家要先確認', async () => {
    render(<OldHomeNotice sessionId="old" />);
    fireEvent.click(within(screen.getByTestId('old-home-notice')).getByRole('button'));
    expect(handleSetHome).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: '設為家' }));
    expect(handleSetHome).toHaveBeenCalledWith('old');
  });

  it('按先不要：焦點回到按鈕，不掉到頁面最前面', async () => {
    render(<OldHomeNotice sessionId="old" />);
    const button = within(screen.getByTestId('old-home-notice')).getByRole('button');
    fireEvent.click(button);
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: '先不要' }));
    await waitFor(() => expect(document.activeElement).toBe(button));
  });
});
