import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryNoticeBar } from '../MemoryNoticeBar';
import { DegradedBanner } from '../DegradedBanner';

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

  it('renders nothing without notices or for codes it cannot describe', () => {
    const unknown = render(<MemoryNoticeBar notices={[{ code: 'not_a_known_code', level: 'info' }]} />);
    expect(unknown.container).toBeEmptyDOMElement();
    const empty = render(<MemoryNoticeBar />);
    expect(empty.container).toBeEmptyDOMElement();
  });
});
