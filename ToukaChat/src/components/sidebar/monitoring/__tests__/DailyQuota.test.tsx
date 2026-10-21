import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DailyQuota } from '../DailyQuota';

describe('DailyQuota', () => {
  it('renders count / max / remaining', () => {
    render(<DailyQuota quota={{ daily_count: 12, daily_max: 50, remaining: 38 }} />);
    expect(screen.getByText(/12\/50/)).toBeInTheDocument();
    expect(screen.getByText(/38 left/)).toBeInTheDocument();
  });

  it('renders nothing when quota is null', () => {
    const { container } = render(<DailyQuota quota={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
