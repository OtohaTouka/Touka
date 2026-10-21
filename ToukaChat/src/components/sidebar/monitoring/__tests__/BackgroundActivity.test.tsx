import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { BackgroundActivity } from '../BackgroundActivity';
import { BackgroundActivityMap } from '@/hooks/useSystemMonitoring';

describe('BackgroundActivity', () => {
  it('renders running sessions with pulse progress', () => {
    const activity: BackgroundActivityMap = {
      s1: { kind: 'heartbeat', current: 2, total: 5, agent: 'Alice' },
    };
    render(<BackgroundActivity activity={activity} />);
    expect(screen.getByTestId('background-activity')).toBeInTheDocument();
    expect(screen.getByText('Alice')).toBeInTheDocument();
    expect(screen.getByText(/2\/5/)).toBeInTheDocument();
  });

  it('renders nothing when there is no background activity', () => {
    const { container } = render(<BackgroundActivity activity={{}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
