/**
 * HAX G15 — 擁有者檢視頁元件 FeedbackLog。
 * 自取 /v1/feedback/recent，唯讀列出回饋；空集合顯示空狀態。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { FeedbackLog } from '@/components/sidebar/monitoring/FeedbackLog';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://x',
  fetchTouka: vi.fn(),
}));
import { fetchTouka } from '@/lib/connections';
const mockFetch = fetchTouka as unknown as ReturnType<typeof vi.fn>;

describe('FeedbackLog（HAX G15 檢視頁）', () => {
  beforeEach(() => mockFetch.mockReset());

  it('渲染最近回饋（含原因）', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => [
        { session_id: 's1', message_id: 'm1', agent_id: 'DebugAgent', feedback_type: 'down', reason: '不準確', created_at: 1, updated_at: 2 },
      ],
    });
    render(<FeedbackLog />);
    await waitFor(() => expect(screen.getByText(/不準確/)).toBeInTheDocument());
  });

  it('空集合顯示空狀態', async () => {
    mockFetch.mockResolvedValue({ ok: true, json: async () => [] });
    render(<FeedbackLog />);
    await waitFor(() => expect(screen.getByText(/尚無回饋/)).toBeInTheDocument());
  });
});
