import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';

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
});
