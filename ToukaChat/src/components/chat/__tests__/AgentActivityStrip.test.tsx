import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AgentActivityStrip } from '../AgentActivityStrip';
import { ProgressMap } from '@/lib/tree_history';

describe('AgentActivityStrip', () => {
  it('renders nothing when there is no progress', () => {
    const { container } = render(<AgentActivityStrip progress={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders a chip per active progress kind', () => {
    const progress: ProgressMap = {
      react_slots: { kind: 'react_slots', current: 2, total: 5 },
      repair: { kind: 'repair', attempt: 1, max: 3 },
    };
    render(<AgentActivityStrip progress={progress} />);
    expect(screen.getByText('自主回合 2/5')).toBeInTheDocument();
    expect(screen.getByText('自我修復 1/3')).toBeInTheDocument();
  });

  it('renders the group label with the agent alias', () => {
    const progress: ProgressMap = {
      group: { kind: 'group', index: 2, total: 3, agent: 'Bob' },
    };
    render(<AgentActivityStrip progress={progress} />);
    expect(screen.getByText('群聊 2/3 @Bob')).toBeInTheDocument();
  });

  it('renders the per-turn token_usage chip with cost', () => {
    const progress: ProgressMap = {
      token_usage: { kind: 'token_usage', total: 1200, cost: 0.0036 },
    };
    render(<AgentActivityStrip progress={progress} />);
    expect(screen.getByText('本回合 1,200 tok · $0.0036')).toBeInTheDocument();
  });
});
