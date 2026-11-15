import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryNoticeBar } from '../MemoryNoticeBar';
import { DegradedBanner } from '../DegradedBanner';
import type { MemoryNotice } from '@/lib/SystemStatusContext';

const colorTokens = (element: HTMLElement) =>
  element.className.split(/\s+/).filter(token => token.startsWith('bg-') || token.startsWith('text-')).sort();

describe('MemoryNoticeBar', () => {
  it('shows an information notice calmly, never as an alert', () => {
    render(<MemoryNoticeBar notices={[{ code: 'unmigrated_legacy_facts', level: 'info', count: 2 }]} />);
    const notice = screen.getByTestId('memory-notice');
    expect(notice).toHaveTextContent(/2/);
    expect(notice).toHaveTextContent(/舊記憶檔/);
    expect(notice).not.toHaveAttribute('role', 'alert');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(notice.className).not.toMatch(/destructive/);
  });

  it('shows a warning notice as an alert as prominent as the degraded banner', () => {
    render(
      <>
        <DegradedBanner degraded reason="synthetic reason" />
        <MemoryNoticeBar notices={[{ code: 'unmigrated_legacy_facts', level: 'warning', count: 1 }]} />
      </>,
    );
    const notice = screen.getByTestId('memory-notice');
    expect(notice).toHaveAttribute('role', 'alert');
    expect(colorTokens(notice)).toEqual(colorTokens(screen.getByTestId('degraded-banner')));
  });

  describe('embedding model mismatch (ADR-0315)', () => {
    const E5 = 'intfloat/multilingual-e5-base';
    const base = { code: 'embedding_model_mismatch', level: 'warning', count: 2, configured_model: E5 } as const;
    const shown = (notice: MemoryNotice) => {
      render(<MemoryNoticeBar notices={[notice]} />);
      return screen.getByTestId('memory-notice');
    };

    it('names the model the memories use when it is known', () => {
      const notice = shown({ ...base, basis: 'name', index_model: 'all-MiniLM-L6-v2', index_dim: 384 });
      expect(notice).toHaveAttribute('role', 'alert');
      expect(notice).toHaveTextContent('all-MiniLM-L6-v2');
      expect(notice).toHaveTextContent(E5);
      expect(notice).toHaveTextContent(/2/);
      expect(notice.textContent).not.toContain('{{');
    });

    it('gives the vector length when only the length differs', () => {
      const notice = shown({ ...base, basis: 'length', index_dim: 384 });
      expect(notice).toHaveTextContent('384');
      expect(notice).toHaveTextContent(E5);
      expect(notice.textContent).not.toContain('{{');
    });

    it('explains the spot check when the length is the same', () => {
      const notice = shown({ ...base, basis: 'sample', index_dim: 384 });
      expect(notice).toHaveTextContent(/抽查/);
      expect(notice).toHaveTextContent(E5);
      expect(notice.textContent).not.toContain('{{');
    });

    it('still shows a message for a basis it does not know', () => {
      const notice = shown({ ...base, basis: 'future_basis' });
      expect(notice).toHaveTextContent(E5);
      expect(notice.textContent).not.toContain('memory_notice');
    });
  });

  it('renders nothing without notices or for codes it cannot describe', () => {
    const unknown = render(<MemoryNoticeBar notices={[{ code: 'not_a_known_code', level: 'info' }]} />);
    expect(unknown.container).toBeEmptyDOMElement();
    const empty = render(<MemoryNoticeBar />);
    expect(empty.container).toBeEmptyDOMElement();
  });
});
