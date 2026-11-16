/**
 * ADR-0317 — 審批卡上的「小幫手掃描到的清單」。
 *
 * 卡片完整顯示清單時，核准請求帶回卡片上的 ack（後端據此算「配對的人看過整份清單」）；
 * 放不下時說明還要在小幫手視窗再確認、也不送 ack；小幫手會再跳視窗時另外說一句。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ToolApprovalCard } from '../ToolApprovalCard';
import type { ToolStep, ToolStepPreview } from '@/lib/tree_history';
import zhTW from '@/locales/zh-TW/translation.json';
import en from '@/locales/en/translation.json';

const fetchToukaMock = vi.fn();
vi.mock('@/lib/connections', () => ({
  fetchTouka: (...args: unknown[]) => fetchToukaMock(...args),
  getActiveProxyUrl: () => 'http://proxy.test',
}));

const LIST = '丟到回收桶  D:\\下載\\a.txt\n搬移  D:\\下載\\b.txt → D:\\整理\\b.txt';

const step = (preview?: ToolStepPreview, extra: Partial<ToolStep['approval']> = {}): ToolStep => ({
  id: 'c1',
  tool: 'local_files',
  args: { action: 'propose', reason: '整理下載' },
  status: 'awaiting_approval',
  approval: { execId: 'ex-9', risk: 'dangerous', timeout: 180, ...(preview ? { preview } : {}), ...extra },
});

beforeEach(() => {
  fetchToukaMock.mockReset();
  fetchToukaMock.mockResolvedValue({ ok: true });
});

describe('HelperPreview on the approval card', () => {
  it('shows the whole list the helper scanned', () => {
    render(<ToolApprovalCard step={step({ text: LIST, complete: true, helperWindow: false, ack: 'ack-1' })} />);
    expect(screen.getByText('小幫手掃描到的清單')).toBeInTheDocument();
    const block = screen.getByTestId('helper-preview');
    expect(block.textContent).toBe(LIST);
    expect(block).toHaveClass('font-mono', 'whitespace-pre');
    expect(screen.queryByText(/卡片沒有全部顯示/)).toBeNull();
    expect(screen.queryByText(/還會在電腦上跳視窗/)).toBeNull();
  });

  it('approving a complete list sends back its ack', async () => {
    render(<ToolApprovalCard step={step({ text: LIST, complete: true, helperWindow: false, ack: 'ack-1' })} />);
    fireEvent.click(screen.getByRole('button', { name: /批准/ }));
    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenCalledWith('http://proxy.test', '/v1/tools/approve/ex-9', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payload: { preview_ack: 'ack-1' } }),
      });
    });
  });

  it('a list cut short says so and sends no ack', async () => {
    render(<ToolApprovalCard step={step({ text: LIST, complete: false, helperWindow: false })} />);
    expect(screen.getByText(/卡片沒有全部顯示/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /批准/ }));
    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenCalledWith('http://proxy.test', '/v1/tools/approve/ex-9', { method: 'POST' });
    });
  });

  it('says when the helper will ask again on the computer', () => {
    render(<ToolApprovalCard step={step({ text: LIST, complete: true, helperWindow: true, ack: 'ack-1' })} />);
    expect(screen.getByText(/還會在電腦上跳視窗/)).toBeInTheDocument();
    // 原因可能是底線、開關沒開或舊配對碼，文字不能只說底線
    expect(zhTW.chat.tool_approval_card.preview_helper_window).not.toContain('底線');
  });

  it('rejecting never sends the ack', async () => {
    render(<ToolApprovalCard step={step({ text: LIST, complete: true, helperWindow: false, ack: 'ack-1' })} />);
    fireEvent.click(screen.getByRole('button', { name: /拒絕/ }));
    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenCalledWith('http://proxy.test', '/v1/tools/reject/ex-9', { method: 'POST' });
    });
  });

  it('sends both acks when the card also shows an unisolated fallback', async () => {
    render(
      <ToolApprovalCard
        step={step(
          { text: LIST, complete: true, helperWindow: false, ack: 'ack-1' },
          { isolation: { level: 'none', fallback: 'landlock', features: ['fs'] } },
        )}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /批准/ }));
    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenCalledWith('http://proxy.test', '/v1/tools/approve/ex-9', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payload: { isolation_ack: 'landlock', preview_ack: 'ack-1' } }),
      });
    });
  });

  it('the scanned list comes after anything the agent wrote', () => {
    render(<ToolApprovalCard step={step({ text: LIST, complete: true, helperWindow: false, ack: 'ack-1' })} />);
    const block = screen.getByTestId('helper-preview');
    const reason = screen.getByText('整理下載');
    // 代理人寫的理由可能假造一份清單，真的清單放在最後、緊接著按鈕
    expect(reason.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('a card without a preview has no list block', () => {
    render(<ToolApprovalCard step={step()} />);
    expect(screen.queryByTestId('helper-preview')).toBeNull();
  });

  it('has the same lines in both languages', () => {
    for (const key of ['preview_title', 'preview_incomplete', 'preview_helper_window'] as const) {
      expect(zhTW.chat.tool_approval_card[key]).toBeTruthy();
      expect(en.chat.tool_approval_card[key]).toBeTruthy();
    }
  });
});
