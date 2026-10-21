import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ContextMeter } from '../ContextMeter';
import { MessageNode } from '@/lib/tree_history';

const mk = (id: string, role: MessageNode['role']): MessageNode => ({
  id, role, content: '', parentId: null, childrenIds: [],
});

const withUsage = (usagePct: number, breakdown?: Record<string, number>): MessageNode[] => [
  mk('1', 'user'),
  {
    ...mk('2', 'assistant'),
    progress: {
      token_usage: {
        kind: 'token_usage',
        total: 1000,
        cost: 0.01,
        usage_pct: usagePct,
        context_breakdown: breakdown
          ? { usage_pct: usagePct, window: 200000, total_tokens: 90000, breakdown }
          : undefined,
      },
    },
  },
];

describe('ContextMeter', () => {
  it('renders a context percentage from rounds', () => {
    render(<ContextMeter messages={[mk('1', 'user'), mk('2', 'assistant')]} />);
    expect(screen.getByTestId('context-meter')).toBeInTheDocument();
    expect(screen.getByText(/context 1%/)).toBeInTheDocument();
  });

  it('renders nothing for an empty conversation', () => {
    const { container } = render(<ContextMeter messages={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('ContextMeter — ADR-0092 Phase B 真實 token%', () => {
  it('有 token_usage 進度時顯示真實 token 百分比（非則數）', () => {
    render(<ContextMeter messages={withUsage(73)} />);
    // 2 則訊息則數估算會是 1%，但 token% 應蓋過顯示 73%。
    expect(screen.getByText(/context 73%/)).toBeInTheDocument();
  });

  it('分解面板列出各類別', () => {
    render(<ContextMeter messages={withUsage(50, { persona: 40000, conversation: 50000 })} />);
    fireEvent.click(screen.getByTestId('context-meter'));
    expect(screen.getByText(/Context 占用/)).toBeInTheDocument();
    expect(screen.getByText('人格')).toBeInTheDocument();
    expect(screen.getByText('對話')).toBeInTheDocument();
  });
});
