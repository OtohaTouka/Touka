import { afterEach, describe, it, expect, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import React from 'react';
import LeftSidebar from '@/components/LeftSidebar';

const chat = vi.hoisted(() => ({ handleSwitchSession: vi.fn(), handleNewSession: vi.fn() }));

vi.mock('@/lib/SystemStatusContext', () => ({ useSystemStatus: () => ({ status: { mode: 'hybrid' } }) }));
vi.mock('@/lib/providers/ChatProvider', () => ({
  useChat: () => ({
    activeSessionId: 'open',
    handleNewSession: chat.handleNewSession,
    handleSwitchSession: chat.handleSwitchSession,
  }),
}));
vi.mock('@/components/sidebar/left/NavigationArea', () => ({ NavigationArea: () => null }));
vi.mock('@/components/sidebar/left/SessionTree', () => ({
  SessionTree: ({ onSwitch }: { onSwitch: (id: string) => void }) => (
    <>
      <button onClick={() => onSwitch('open')}>開著的</button>
      <button onClick={() => onSwitch('other')}>另一段</button>
    </>
  ),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('LeftSidebar — 點對話', () => {
  it('在其他畫面點開著的這段：只回聊天，不重新載入（和以前的聊天大廳一樣）', () => {
    const onChatClick = vi.fn();
    render(<LeftSidebar activeMode="settings" onChatClick={onChatClick} />);
    fireEvent.click(screen.getByRole('button', { name: '開著的' }));
    expect(onChatClick).toHaveBeenCalledTimes(1);
    expect(chat.handleSwitchSession).not.toHaveBeenCalled();
  });

  it('在其他畫面點另一段：切過去，也回聊天', () => {
    const onChatClick = vi.fn();
    render(<LeftSidebar activeMode="settings" onChatClick={onChatClick} />);
    fireEvent.click(screen.getByRole('button', { name: '另一段' }));
    expect(chat.handleSwitchSession).toHaveBeenCalledWith('other');
    expect(onChatClick).toHaveBeenCalledTimes(1);
  });

  it('已經在聊天：點哪一段都照常切換（重新載入看得到背景寫入的內容），不再換畫面', () => {
    const onChatClick = vi.fn();
    render(<LeftSidebar activeMode="chat" onChatClick={onChatClick} />);
    fireEvent.click(screen.getByRole('button', { name: '開著的' }));
    fireEvent.click(screen.getByRole('button', { name: '另一段' }));
    expect(chat.handleSwitchSession.mock.calls).toEqual([['open'], ['other']]);
    expect(onChatClick).not.toHaveBeenCalled();
  });
});
