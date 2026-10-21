import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TokenUsage } from '../TokenUsage';
import { TokenUsageMap } from '@/hooks/useSystemMonitoring';

describe('TokenUsage — P3b nested shape', () => {
  it('aggregates nested agent→provider stats into total tokens + cost', () => {
    const usage: TokenUsageMap = {
      Alice: {
        Anthropic: { prompt_tokens: 1000, completion_tokens: 200, total_cost_usd: 0.006 },
        Gemini: { prompt_tokens: 300, completion_tokens: 100, total_cost_usd: 0.0024 },
      },
    };
    render(<TokenUsage tokenUsage={usage} />);
    // total = 1000+200+300+100 = 1,600；cost = 0.006+0.0024 = 0.0084
    expect(screen.getByText('Alice')).toBeInTheDocument();
    expect(screen.getByText(/1,600/)).toBeInTheDocument();
    expect(screen.getByText(/\$0\.0084/)).toBeInTheDocument();
  });

  it('shows a placeholder when there is no usage', () => {
    render(<TokenUsage tokenUsage={{}} />);
    expect(screen.getByText('No usage recorded')).toBeInTheDocument();
  });
});
