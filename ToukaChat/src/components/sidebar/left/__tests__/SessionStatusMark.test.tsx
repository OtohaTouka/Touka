/**
 * 工作狀態的記號（ADR-0300）：工作中呼吸、等您處理不動、減少動態時改用文字。
 * 截圖會把動畫停掉，類別在不在是這裡驗的；真的有沒有在動由實際驗證的 getAnimations() 驗。
 */
import { render, renderHook, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { SessionWorkStatus } from '@/lib/session_activity';
import { SessionAttentionDot, SessionStatusLead, SessionStatusTail, useBreathingClass, useStatusLabel } from '../SessionStatusMark';

const motion = vi.hoisted(() => ({ reduced: false }));
vi.mock('@/hooks/useReducedMotionPreference', () => ({ useReducedMotionPreference: () => motion.reduced }));

const REPLYING: SessionWorkStatus = { kind: 'replying' };
const HEARTBEAT: SessionWorkStatus = { kind: 'heartbeat', current: 2, total: 5 };
const AWAITING: SessionWorkStatus = { kind: 'awaiting' };
const UNSEEN: SessionWorkStatus = { kind: 'unseen' };

beforeEach(() => {
  motion.reduced = false;
});

describe('狀態的文字', () => {
  it.each([
    [REPLYING, '回覆中'],
    [HEARTBEAT, '心跳 2/5'],
    [AWAITING, '等您處理'],
    [UNSEEN, '有新內容'],
    [null, null],
  ])('%j → %s', (status, label) => {
    expect(renderHook(() => useStatusLabel(status)).result.current).toBe(label);
  });
});

describe('工作中', () => {
  it.each([[REPLYING], [HEARTBEAT]])('%j：桌面版名稱呼吸；手機版名稱前的小點呼吸', (status) => {
    expect(renderHook(() => useBreathingClass(status)).result.current).toBe('md:animate-breathe');
    render(<SessionStatusLead status={status} />);
    const dot = screen.getByTestId('session-status-working-dot');
    expect(dot).toHaveClass('animate-breathe', 'md:hidden');
  });

  it('心跳寫進度；回覆中不多寫字', () => {
    const { rerender } = render(<SessionStatusTail status={HEARTBEAT} />);
    expect(screen.getByTestId('session-status-text')).toHaveTextContent('心跳 2/5');
    rerender(<SessionStatusTail status={REPLYING} />);
    expect(screen.queryByTestId('session-status-text')).toBeNull();
  });

  it.each([[AWAITING], [UNSEEN], [null]])('%j：名稱不呼吸', (status) => {
    expect(renderHook(() => useBreathingClass(status)).result.current).toBeUndefined();
  });
});

describe('減少動態', () => {
  beforeEach(() => {
    motion.reduced = true;
  });

  it('不動、沒有小點，改顯示文字', () => {
    expect(renderHook(() => useBreathingClass(REPLYING)).result.current).toBeUndefined();
    render(<><SessionStatusLead status={REPLYING} /><SessionStatusTail status={REPLYING} /></>);
    expect(screen.queryByTestId('session-status-working-dot')).toBeNull();
    expect(screen.getByTestId('session-status-text')).toHaveTextContent('回覆中');
  });

  it('按鈕上的提示點還在，但不動', () => {
    render(<SessionAttentionDot status={HEARTBEAT} />);
    expect(screen.getByTestId('session-attention-dot')).not.toHaveClass('animate-breathe');
  });
});

describe('等您處理與有新內容', () => {
  it('等您處理：不動的圖示，沒有小點也沒有文字', () => {
    render(<><SessionStatusLead status={AWAITING} /><SessionStatusTail status={AWAITING} /></>);
    expect(screen.getByTestId('session-status-awaiting')).not.toHaveClass('animate-breathe');
    expect(screen.queryByTestId('session-status-working-dot')).toBeNull();
    expect(screen.queryByTestId('session-status-text')).toBeNull();
  });

  it('有新內容：只有名稱後面的小點', () => {
    render(<><SessionStatusLead status={UNSEEN} /><SessionStatusTail status={UNSEEN} /></>);
    expect(screen.getByTestId('session-status-unseen')).not.toHaveClass('animate-breathe');
    expect(screen.queryByTestId('session-status-awaiting')).toBeNull();
  });
});

describe('按鈕上的提示點', () => {
  it('沒有狀態不畫', () => {
    render(<SessionAttentionDot status={null} />);
    expect(screen.queryByTestId('session-attention-dot')).toBeNull();
  });

  it.each([
    [HEARTBEAT, 'heartbeat', true],
    [REPLYING, 'replying', true],
    [AWAITING, 'awaiting', false],
    [UNSEEN, 'unseen', false],
  ])('%j：標出種類，只有工作中才呼吸', (status, kind, breathes) => {
    render(<SessionAttentionDot status={status} />);
    const dot = screen.getByTestId('session-attention-dot');
    expect(dot).toHaveAttribute('data-kind', kind);
    expect(dot.classList.contains('animate-breathe')).toBe(breathes);
  });
});
