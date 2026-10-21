import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DegradedBanner } from '../DegradedBanner';

describe('DegradedBanner', () => {
  it('renders an alert with the degraded reason when degraded', () => {
    render(<DegradedBanner degraded reason="Memory System module not found." />);
    const banner = screen.getByTestId('degraded-banner');
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveAttribute('role', 'alert');
    expect(screen.getByText(/Memory System module not found/)).toBeInTheDocument();
    expect(screen.getByText(/系統降級運行中/)).toBeInTheDocument();
  });

  it('falls back to a default reason when none is provided', () => {
    render(<DegradedBanner degraded reason="" />);
    expect(screen.getByText(/記憶系統未載入/)).toBeInTheDocument();
  });

  it('renders nothing when the system is healthy', () => {
    const { container } = render(<DegradedBanner degraded={false} reason="x" />);
    expect(container).toBeEmptyDOMElement();
  });
});
