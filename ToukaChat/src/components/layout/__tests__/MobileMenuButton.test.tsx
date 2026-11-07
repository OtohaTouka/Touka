/** 手機頂列開抽屜的按鈕（ADR-0300）：抽屜關著也看得到狀態，和首次設定的紅點並存，名稱涵蓋兩者。 */
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { SessionWorkStatus } from '@/lib/session_activity';
import { MobileMenuButton } from '../MobileMenuButton';

const state = vi.hoisted(() => ({ notice: false, attention: null as SessionWorkStatus | null }));
vi.mock('@/components/setup/SetupNoticesProvider', () => ({ useHasSetupNotice: () => state.notice }));
vi.mock('@/lib/providers/SessionActivityProvider', () => ({ useSessionActivity: () => ({ attention: state.attention }) }));

beforeEach(() => {
  state.notice = false;
  state.attention = null;
});

describe('MobileMenuButton', () => {
  it('沒事：只有基本名稱，沒有點', () => {
    render(<MobileMenuButton />);
    expect(screen.getByRole('button')).toHaveAccessibleName('導覽選單');
    expect(screen.queryByTestId('session-attention-dot')).toBeNull();
    expect(screen.queryByTestId('setup-notice-dot')).toBeNull();
  });

  it('有對話在工作：名稱寫狀態、亮提示點', () => {
    state.attention = { kind: 'heartbeat', current: 1, total: 3 };
    render(<MobileMenuButton />);
    expect(screen.getByRole('button')).toHaveAccessibleName('導覽選單，心跳 1/3');
    expect(screen.getByTestId('session-attention-dot')).toHaveAttribute('data-kind', 'heartbeat');
  });

  it('和首次設定提醒同時存在：兩個點都在、名稱涵蓋兩者', () => {
    state.notice = true;
    state.attention = { kind: 'awaiting' };
    render(<MobileMenuButton />);
    const name = screen.getByRole('button').getAttribute('aria-label') ?? '';
    expect(name).toContain('設定');
    expect(name).toContain('等您處理');
    expect(screen.getByTestId('session-attention-dot')).toBeInTheDocument();
    expect(screen.getByTestId('setup-notice-dot')).toBeInTheDocument();
  });

  it('帶進來的 props 會傳下去（SheetTrigger 靠這個開抽屜）', () => {
    render(<MobileMenuButton aria-expanded={true} data-state="open" />);
    expect(screen.getByRole('button')).toHaveAttribute('data-state', 'open');
  });
});
