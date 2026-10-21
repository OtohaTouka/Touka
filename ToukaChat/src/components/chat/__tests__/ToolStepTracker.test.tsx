import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ToolStepTracker } from '../ToolStepTracker';
import { ToolStep } from '@/lib/tree_history';

describe('ToolStepTracker — P2b live output', () => {
  it('lets a still-running step with streamed output expand to show it', () => {
    const steps: ToolStep[] = [
      { id: 't1', tool: 'bash', args: { command: 'ls' }, status: 'running', result: 'live-line-1\n' },
    ];
    const { container } = render(<ToolStepTracker steps={steps} />);
    // 折疊時尚未顯示輸出
    expect(screen.queryByText(/live-line-1/)).not.toBeInTheDocument();
    // 執行中（running）仍可展開（canExpand = !!result）
    const header = container.querySelector('.cursor-pointer') as HTMLElement;
    expect(header).toBeTruthy();
    fireEvent.click(header);
    expect(screen.getByText(/live-line-1/)).toBeInTheDocument();
  });

  it('keeps a running step without output non-expandable', () => {
    const steps: ToolStep[] = [
      { id: 't2', tool: 'bash', args: {}, status: 'running' },
    ];
    const { container } = render(<ToolStepTracker steps={steps} />);
    expect(container.querySelector('.cursor-pointer')).toBeNull();
  });

  it('renders nothing for empty steps', () => {
    const { container } = render(<ToolStepTracker steps={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('ToolStepTracker — P3 structured card', () => {
  it('shows permission_denied as warning (拒絕黃) + human message, not raw JSON', () => {
    const steps: ToolStep[] = [
      {
        id: 'd1', tool: 'execute_command', args: {}, status: 'error',
        result: '{"error_class":"permission_denied","message":"Blocked by policy","suggestion":"use file verbs"}',
      },
    ];
    const { container } = render(<ToolStepTracker steps={steps} />);
    expect(container.querySelector('.text-status-warning')).toBeTruthy();
    fireEvent.click(container.querySelector('.cursor-pointer') as HTMLElement);
    expect(screen.getByText(/Blocked by policy/)).toBeInTheDocument();
    expect(screen.getByText(/use file verbs/)).toBeInTheDocument();
    expect(screen.queryByText(/error_class/)).not.toBeInTheDocument(); // 模型面 JSON 不外顯
  });

  it('shows a structured error (non-denied) message in red, not raw JSON', () => {
    const steps: ToolStep[] = [
      {
        id: 'e1', tool: 'write', args: {}, status: 'error',
        result: '{"error_class":"validation_error","message":"missing scope"}',
      },
    ];
    const { container } = render(<ToolStepTracker steps={steps} />);
    expect(container.querySelector('.text-status-error')).toBeTruthy();
    fireEvent.click(container.querySelector('.cursor-pointer') as HTMLElement);
    expect(screen.getByText(/missing scope/)).toBeInTheDocument();
    expect(screen.queryByText(/validation_error/)).not.toBeInTheDocument();
  });

  it('shows structured success preview (回最終狀態、省重讀)', () => {
    const steps: ToolStep[] = [
      {
        id: 's1', tool: 'edit', args: {}, status: 'success',
        result: '{"ok":true,"message":"Edited foo.py","preview":"> 10| changed line"}',
      },
    ];
    const { container } = render(<ToolStepTracker steps={steps} />);
    fireEvent.click(container.querySelector('.cursor-pointer') as HTMLElement);
    expect(screen.getByText(/Edited foo\.py/)).toBeInTheDocument();
    expect(screen.getByText(/changed line/)).toBeInTheDocument();
  });
});
