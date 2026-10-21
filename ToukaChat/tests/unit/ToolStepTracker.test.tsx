import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ToolStepTracker } from '@/components/chat/ToolStepTracker';
import { ToolStep } from '@/lib/tree_history';

describe('ToolStepTracker — HAX G11 記憶用量徽章', () => {
  it('記憶工具以 query 顯示「記憶：…」徽章', () => {
    const steps: ToolStep[] = [
      { id: '1', tool: 'memory', args: { action: 'recall', query: '咖啡偏好' }, status: 'success', result: 'ok' },
    ];
    render(<ToolStepTracker steps={steps} />);
    expect(screen.getByText(/記憶：咖啡偏好/)).toBeInTheDocument();
  });

  it('vault 工具以 path 顯示徽章', () => {
    const steps: ToolStep[] = [
      { id: '1', tool: 'vault_tools', args: { action: 'read', path: 'notes/todo.md' }, status: 'success', result: 'ok' },
    ];
    render(<ToolStepTracker steps={steps} />);
    expect(screen.getByText(/記憶：notes\/todo\.md/)).toBeInTheDocument();
  });

  it('非記憶工具不顯示記憶徽章', () => {
    const steps: ToolStep[] = [
      { id: '1', tool: 'bash', args: { command: 'ls' }, status: 'success', result: 'ok' },
    ];
    render(<ToolStepTracker steps={steps} />);
    expect(screen.queryByText(/記憶：/)).not.toBeInTheDocument();
  });
});
