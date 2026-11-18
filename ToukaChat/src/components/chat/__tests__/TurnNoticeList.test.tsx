/**
 * ADR-0322 PR 2b：回覆下方的小提示（灰字、可收合）。種類照 i18n 顯示，參數當純文字；
 * 全是工具計時這類例行提示時預設收合，有警告、中斷就預設打開。
 */
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { TurnNoticeList } from '../TurnNoticeList';

describe('TurnNoticeList (ADR-0322 PR 2b)', () => {
  it('opens when there is more than routine timing and shows each kind', () => {
    render(<TurnNoticeList notices={[
      { kind: 'tool_finished', params: { tool: 'read', time: '10:00' } },
      { kind: 'rounds_exhausted', params: { rounds: 12 } },
      { kind: 'interrupted', params: {} },
    ]} />);
    expect(screen.getByRole('button')).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('系統提示（3）')).toBeInTheDocument();
    expect(screen.getByText('read 完成：10:00')).toBeInTheDocument();
    expect(screen.getByText('工具輪額度已盡（12 輪），進行最後總結。')).toBeInTheDocument();
    expect(screen.getByText('已由使用者中斷。')).toBeInTheDocument();
  });

  it('starts folded when every notice is routine tool timing', () => {
    render(<TurnNoticeList notices={[{ kind: 'tool_finished', params: { tool: 'read', time: '10:00' } }]} />);
    const button = screen.getByRole('button');
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('read 完成：10:00')).not.toBeInTheDocument();
    fireEvent.click(button);
    expect(screen.getByText('read 完成：10:00')).toBeInTheDocument();
  });

  it('renders params as literal text', () => {
    const hostile = '<img src=x onerror=alert(1)>';
    const { container } = render(<TurnNoticeList notices={[{ kind: 'tool_blocked', params: { tool: hostile } }]} />);
    expect(screen.getByText(`攔截未授權調用：${hostile}`)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
  });

  it('renders nothing without notices', () => {
    const { container } = render(<TurnNoticeList notices={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
