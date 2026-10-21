/**
 * HAX G15/G6 — 倒讚原因彈窗。textarea 限長 500；送出帶 reason；取消觸發 onCancel。
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { FeedbackReasonDialog } from '@/components/chat/FeedbackReasonDialog';

describe('FeedbackReasonDialog（HAX G15/G6）', () => {
  it('送出時以輸入文字回呼 onSubmit', () => {
    const onSubmit = vi.fn();
    render(<FeedbackReasonDialog open onSubmit={onSubmit} onCancel={vi.fn()} />);
    const box = screen.getByRole('textbox');
    fireEvent.change(box, { target: { value: '回答不準確' } });
    fireEvent.click(screen.getByRole('button', { name: /送出/ }));
    expect(onSubmit).toHaveBeenCalledWith('回答不準確');
  });

  it('textarea 限長 500', () => {
    render(<FeedbackReasonDialog open onSubmit={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByRole('textbox')).toHaveAttribute('maxLength', '500');
  });

  it('取消觸發 onCancel', () => {
    const onCancel = vi.fn();
    render(<FeedbackReasonDialog open onSubmit={vi.fn()} onCancel={onCancel} />);
    fireEvent.click(screen.getByRole('button', { name: /取消/ }));
    expect(onCancel).toHaveBeenCalled();
  });
});
