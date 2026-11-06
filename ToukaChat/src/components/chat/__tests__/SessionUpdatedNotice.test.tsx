/** 開著的對話被別處寫入的提示（ADR-0300）。存檔失敗時點了也更新不了，不顯示。 */
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SessionUpdatedNotice } from '../SessionUpdatedNotice';

const state = vi.hoisted(() => ({
  stale: 'a' as string | null,
  persistence: undefined as string | undefined,
  clearStale: vi.fn(),
  handleSwitchSession: vi.fn(),
}));
vi.mock('@/lib/providers/SessionActivityProvider', () => ({
  useSessionActivity: () => ({ staleSessionId: state.stale, clearStale: state.clearStale }),
}));
vi.mock('@/lib/providers/SessionListProvider', () => ({
  useSessionList: () => ({ getPersistence: () => (state.persistence ? { status: state.persistence } : undefined) }),
}));
vi.mock('@/lib/providers/ChatProvider', () => ({
  useChat: () => ({ handleSwitchSession: state.handleSwitchSession }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  state.stale = 'a';
  state.persistence = undefined;
});

describe('SessionUpdatedNotice', () => {
  it('點一下：先收起提示，再重新讀這段對話', () => {
    render(<SessionUpdatedNotice sessionId="a" />);
    fireEvent.click(screen.getByRole('button', { name: '點一下更新' }));
    expect(state.clearStale).toHaveBeenCalledOnce();
    expect(state.handleSwitchSession).toHaveBeenCalledWith('a');
  });

  it.each([['saved'], ['saving'], [undefined]])('存檔狀態 %s：顯示', (persistence) => {
    state.persistence = persistence;
    render(<SessionUpdatedNotice sessionId="a" />);
    expect(screen.getByRole('status')).toBeInTheDocument();
  });

  it.each([['failed'], ['unknown']])('存檔狀態 %s：不顯示，留給存檔失敗的畫面', (persistence) => {
    state.persistence = persistence;
    const { container } = render(<SessionUpdatedNotice sessionId="a" />);
    expect(container).toBeEmptyDOMElement();
  });

  it.each([['別段對話', 'b'], ['沒有過時', null]])('%s：不顯示', (_name, stale) => {
    state.stale = stale;
    const { container } = render(<SessionUpdatedNotice sessionId="a" />);
    expect(container).toBeEmptyDOMElement();
  });
});
