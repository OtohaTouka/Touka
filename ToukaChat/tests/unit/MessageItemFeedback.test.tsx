/**
 * HAX G15（細粒度回饋）— MessageItem 讚/倒讚鈕。
 * 僅 assistant 訊息、且有 onFeedback 時顯示；讚即時送、倒讚請求原因、再點同鍵＝撤回。
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MessageItem } from '@/components/chat/MessageItem';
import React from 'react';

vi.mock('@/components/chat/ToolStepTracker', () => ({
  ToolStepTracker: () => <div data-testid="tool-step-tracker" />,
}));
vi.mock('next/image', () => ({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any, @next/next/no-img-element
  default: (props: any) => <img {...props} alt={props.alt || ''} />,
}));

const history = {
  messages: {
    '1': { id: '1', role: 'user', content: 'hi', childrenIds: ['2'] },
    '2': { id: '2', role: 'assistant', content: 'hello', parentId: '1', childrenIds: [] },
  },
  currentId: '2',
};

const base = {
  history: history as never,
  isSending: false,
  isIDE: false,
  editingMessageId: null,
  setEditingMessageId: vi.fn(),
  editValue: '',
  setEditValue: vi.fn(),
  handleCopy: vi.fn(),
  handleEditMessage: vi.fn(),
  handleDeleteMessage: vi.fn(),
  handleRegenerate: vi.fn(),
  handleSwitchMessage: vi.fn(),
  handleApplyCode: vi.fn().mockResolvedValue(true),
};

const assistantMsg = history.messages['2'] as never;

describe('MessageItem 回饋鈕（HAX G15）', () => {
  it('assistant 訊息在提供 onFeedback 時顯示讚/倒讚', () => {
    render(
      <MessageItem {...base} msg={assistantMsg} onFeedback={vi.fn()} onRequestDislike={vi.fn()} />,
    );
    expect(screen.getByRole('button', { name: '讚' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '倒讚' })).toBeInTheDocument();
  });

  it('點讚（無既有回饋）→ onFeedback(id, "up")', () => {
    const onFeedback = vi.fn();
    render(
      <MessageItem {...base} msg={assistantMsg} onFeedback={onFeedback} onRequestDislike={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole('button', { name: '讚' }));
    expect(onFeedback).toHaveBeenCalledWith('2', 'up');
  });

  it('點倒讚（無既有回饋）→ 請求原因 onRequestDislike(id)', () => {
    const onRequestDislike = vi.fn();
    render(
      <MessageItem {...base} msg={assistantMsg} onFeedback={vi.fn()} onRequestDislike={onRequestDislike} />,
    );
    fireEvent.click(screen.getByRole('button', { name: '倒讚' }));
    expect(onRequestDislike).toHaveBeenCalledWith('2');
  });

  it('已讚時再點讚＝撤回 → onFeedback(id, "none")', () => {
    const onFeedback = vi.fn();
    render(
      <MessageItem
        {...base}
        msg={assistantMsg}
        feedback={{ type: 'up' }}
        onFeedback={onFeedback}
        onRequestDislike={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '讚' }));
    expect(onFeedback).toHaveBeenCalledWith('2', 'none');
  });

  it('user 訊息不顯示回饋鈕', () => {
    render(
      <MessageItem
        {...base}
        msg={history.messages['1'] as never}
        onFeedback={vi.fn()}
        onRequestDislike={vi.fn()}
      />,
    );
    expect(screen.queryByRole('button', { name: '讚' })).not.toBeInTheDocument();
  });
});
