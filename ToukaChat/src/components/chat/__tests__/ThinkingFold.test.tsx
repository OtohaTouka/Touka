/**
 * ADR-0152 T2 — ThinkingFold 思考摺疊區塊。
 *
 * 預設收合（內容不可見）→ 點擊展開；串流中（isThinking）標題為「思考中…」，
 * 完成後為「思考過程」。
 */
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ThinkingFold } from '../ThinkingFold';

describe('ThinkingFold (ADR-0152)', () => {
  it('預設收合：標題可見、思考內容不可見', () => {
    render(<ThinkingFold thinking="祕密推理內容" />);
    expect(screen.getByText('思考過程')).toBeInTheDocument();
    expect(screen.queryByText('祕密推理內容')).not.toBeInTheDocument();
    expect(screen.getByRole('button')).toHaveAttribute('aria-expanded', 'false');
  });

  it('點擊展開顯示思考內容，再點收合', () => {
    render(<ThinkingFold thinking="祕密推理內容" />);
    const btn = screen.getByRole('button');
    fireEvent.click(btn);
    expect(screen.getByText('祕密推理內容')).toBeInTheDocument();
    expect(btn).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(btn);
    expect(screen.queryByText('祕密推理內容')).not.toBeInTheDocument();
  });

  it('串流中（isThinking）標題顯示「思考中…」', () => {
    render(<ThinkingFold thinking="推理進行中" isThinking />);
    expect(screen.getByText('思考中…')).toBeInTheDocument();
    expect(screen.queryByText('思考過程')).not.toBeInTheDocument();
  });
});
