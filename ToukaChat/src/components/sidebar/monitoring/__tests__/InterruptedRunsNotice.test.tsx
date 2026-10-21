import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { InterruptedRunsNotice } from '../InterruptedRunsNotice';
import { InterruptedRun } from '@/hooks/useSystemMonitoring';
import * as connections from '@/lib/connections';
import { toast } from 'sonner';

vi.mock('@/lib/connections');

describe('InterruptedRunsNotice', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(connections, 'getActiveProxyUrl').mockReturnValue('http://localhost:8000');
    vi.spyOn(connections, 'fetchTouka').mockResolvedValue({ ok: true } as Response);
  });

  it('renders a warning with the count and agents when there are interrupted runs', () => {
    const runs: InterruptedRun[] = [
      { session_id: 's1', agent: 'Alice', total: 5 },
      { session_id: 's2', agent: 'Bob', total: 2 },
    ];
    render(<InterruptedRunsNotice runs={runs} />);
    expect(screen.getByTestId('interrupted-runs-notice')).toBeInTheDocument();
    expect(screen.getByText(/2 個未完成/)).toBeInTheDocument();
    expect(screen.getByText('Alice')).toBeInTheDocument();
  });

  it('falls back to session_id and hides the button when agent is missing', () => {
    const runs: InterruptedRun[] = [{ session_id: 'sess-xyz', total: 1 }];
    render(<InterruptedRunsNotice runs={runs} />);
    expect(screen.getByText('sess-xyz')).toBeInTheDocument();
    // 缺 agent → 無法重新觸發，不顯示按鈕
    expect(screen.queryByRole('button', { name: /重新觸發/ })).not.toBeInTheDocument();
  });

  it('renders nothing when there are no interrupted runs', () => {
    const { container } = render(<InterruptedRunsNotice runs={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('posts to the retrigger endpoint and removes the run on success', async () => {
    const successSpy = vi.spyOn(toast, 'success').mockImplementation((() => '') as never);
    const runs: InterruptedRun[] = [{ session_id: 's1', agent: 'Alice', total: 5 }];
    render(<InterruptedRunsNotice runs={runs} />);

    fireEvent.click(screen.getByRole('button', { name: /重新觸發/ }));

    await waitFor(() => {
      expect(connections.fetchTouka).toHaveBeenCalledWith(
        'http://localhost:8000',
        '/v1/heartbeat/retrigger',
        expect.objectContaining({ method: 'POST' }),
      );
    });
    // 送出的 body 含 agent_id 與 session_id
    const body = JSON.parse(
      (vi.mocked(connections.fetchTouka).mock.calls[0][2] as RequestInit).body as string,
    );
    expect(body).toEqual({ agent_id: 'Alice', session_id: 's1' });
    expect(successSpy).toHaveBeenCalled();
    // 成功後該筆自畫面移除 → 整個通知消失
    await waitFor(() => {
      expect(screen.queryByTestId('interrupted-runs-notice')).not.toBeInTheDocument();
    });
  });

  it('shows an error toast when retrigger fails', async () => {
    const errorSpy = vi.spyOn(toast, 'error').mockImplementation((() => '') as never);
    vi.spyOn(connections, 'fetchTouka').mockResolvedValue({ ok: false } as Response);
    const runs: InterruptedRun[] = [{ session_id: 's1', agent: 'Alice', total: 5 }];
    render(<InterruptedRunsNotice runs={runs} />);

    fireEvent.click(screen.getByRole('button', { name: /重新觸發/ }));

    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalled();
    });
    // 失敗 → 該筆仍在
    expect(screen.getByTestId('interrupted-runs-notice')).toBeInTheDocument();
  });
});
