/**
 * ADR-0249 第 4a 段：排隊清單（灰色、可收回或修改）與工具時間軸的「第 N 步送達」標記。
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { QueuedMessageList } from '@/components/chat/QueuedMessageList';
import { InterjectionMarker } from '@/components/chat/InterjectionMarker';
import type { QueuedInterjection } from '@/lib/chat/interjectionStore';
import type { Interjection } from '@/lib/types';

const item = (id: string, text: string, extra: Partial<QueuedInterjection> = {}): QueuedInterjection =>
  ({ id, text, urgent: false, status: 'queued', step: null, created_at: 1, ...extra });

describe('QueuedMessageList', () => {
  it('沒有排隊的訊息時不渲染', () => {
    const { container } = render(<QueuedMessageList items={[]} onRetract={vi.fn()} onEdit={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('逐則列出，標示優先與送出時機；收回、修改各自回呼該則', () => {
    const onRetract = vi.fn();
    const onEdit = vi.fn();
    const items = [item('a', '改用 B 方案', { urgent: true }), item('b', '晚點再說', { status: 'undelivered', local: true })];
    render(<QueuedMessageList items={items} onRetract={onRetract} onEdit={onEdit} />);
    const list = screen.getByRole('list', { name: '排隊中的補充' });
    const rows = within(list).getAllByRole('listitem');
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText('改用 B 方案')).toBeInTheDocument();
    expect(within(rows[0]).getByText('優先')).toBeInTheDocument();
    expect(within(rows[0]).getByText('下一個工具步驟交給 AI')).toBeInTheDocument();
    expect(within(rows[1]).getByText('回合結束後送出')).toBeInTheDocument();
    fireEvent.click(within(rows[0]).getByRole('button', { name: '收回' }));
    fireEvent.click(within(rows[1]).getByRole('button', { name: '修改' }));
    expect(onRetract).toHaveBeenCalledWith(items[0]);
    expect(onEdit).toHaveBeenCalledWith(items[1]);
  });
});

describe('InterjectionMarker', () => {
  const delivered = (id: string, step: number, text: string): Interjection =>
    ({ id, text, urgent: false, status: 'delivered', step, created_at: 1 });

  it('照步數標出「你的補充在第 N 步送達」與原文', () => {
    render(<InterjectionMarker interjections={[delivered('b', 4, '第二則'), delivered('a', 2, '第一則')]} />);
    const rows = within(screen.getByRole('list', { name: '本回合中的補充' })).getAllByRole('listitem');
    expect(rows.map(r => r.textContent)).toEqual(['你的補充在第 2 步送達第一則', '你的補充在第 4 步送達第二則']);
  });

  it('沒有已送達的插話時不渲染', () => {
    const { container } = render(
      <InterjectionMarker interjections={[{ ...delivered('a', 1, 'x'), status: 'undelivered' }]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
