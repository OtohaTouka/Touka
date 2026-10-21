/**
 * HAX G10 — AskUserModal。一頁列出全部問題：單選=radio、複選=checkbox、每題可附「其他」自由填。
 * 送出時回傳依問題順序的答案陣列（string[][]）。
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { AskUserModal } from '@/components/modals/AskUserModal';

const request = {
  id: 'tok1',
  questions: [
    { question: '單選題', options: ['甲', '乙'], multiSelect: false, allowOther: false },
    { question: '複選題', options: ['X', 'Y'], multiSelect: true, allowOther: true },
  ],
};

describe('AskUserModal（HAX G10）', () => {
  it('一頁渲染全部問題與選項', () => {
    render(<AskUserModal request={request as never} onSubmit={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByText('單選題')).toBeInTheDocument();
    expect(screen.getByText('複選題')).toBeInTheDocument();
    expect(screen.getByText('甲')).toBeInTheDocument();
    expect(screen.getByText('X')).toBeInTheDocument();
  });

  it('單選＋複選作答後送出，payload 依問題順序', () => {
    const onSubmit = vi.fn();
    render(<AskUserModal request={request as never} onSubmit={onSubmit} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByText('甲'));
    fireEvent.click(screen.getByText('X'));
    fireEvent.click(screen.getByText('Y'));
    fireEvent.click(screen.getByRole('button', { name: /送出/ }));
    expect(onSubmit).toHaveBeenCalledWith([['甲'], ['X', 'Y']]);
  });

  it('「其他」自由填入會併入該題答案', () => {
    const onSubmit = vi.fn();
    render(<AskUserModal request={request as never} onSubmit={onSubmit} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByText('甲'));
    fireEvent.click(screen.getByText('X'));
    const otherInput = screen.getByPlaceholderText(/其他/);
    fireEvent.change(otherInput, { target: { value: 'zzz' } });
    fireEvent.click(screen.getByRole('button', { name: /送出/ }));
    expect(onSubmit).toHaveBeenCalledWith([['甲'], ['X', '其他：zzz']]);
  });

  it('取消觸發 onCancel', () => {
    const onCancel = vi.fn();
    render(<AskUserModal request={request as never} onSubmit={vi.fn()} onCancel={onCancel} />);
    fireEvent.click(screen.getByRole('button', { name: /取消/ }));
    expect(onCancel).toHaveBeenCalled();
  });
});
