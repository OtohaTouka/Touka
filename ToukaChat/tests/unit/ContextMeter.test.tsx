import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ContextMeter } from '@/components/chat/ContextMeter';
import { MessageNode } from '@/lib/tree_history';

const msgs: MessageNode[] = [
  { id: 'u1', parentId: null, childrenIds: ['a1'], role: 'user', content: 'hi' },
  { id: 'a1', parentId: 'u1', childrenIds: [], role: 'assistant', content: 'hello' },
];

describe('ContextMeter — HAX G12 上下文載入面板', () => {
  it('顯示 context 百分比，點擊開啟說明面板（揭露載入則數與上限）', () => {
    render(<ContextMeter messages={msgs} />);
    const meter = screen.getByTestId('context-meter');
    expect(meter).toBeInTheDocument();

    fireEvent.click(meter);
    expect(screen.getByText(/本次對話載入了什麼/)).toBeInTheDocument();
    expect(screen.getByText(/上限/)).toBeInTheDocument();
    expect(screen.getByText(/滑動視窗/)).toBeInTheDocument();
  });

  it('空對話不顯示', () => {
    const { container } = render(<ContextMeter messages={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
