import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { FailedHandoversNotice } from '../FailedHandoversNotice';

describe('FailedHandoversNotice（ADR-0307）', () => {
  it('列出失敗的交接：哪個靈魂、哪一段舊家', () => {
    render(<FailedHandoversNotice handovers={[{ agent: 'Alice', old_session: 'old-1', failures: 3 }]} />);
    expect(screen.getByTestId('failed-handovers-notice')).toBeInTheDocument();
    expect(screen.getByText(/Alice/)).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('1');
  });

  it('沒有失敗就不顯示', () => {
    const { container } = render(<FailedHandoversNotice handovers={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
