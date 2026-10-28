/**
 * hitl-unification H3 — 聊天內嵌審批卡。
 *
 * 待決（awaiting_approval）：渲染工具名/參數/風險章/批准/拒絕；點批准 →
 * POST /v1/tools/approve/{execId}（fetchTouka）、按鈕鎖定等 resolved 事件收斂；
 * 已解算：只剩狀態徽章（無按鈕）。經 ToolStepTracker 掛載（行 → 卡）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ToolStepTracker } from '../ToolStepTracker';
import { ToolApprovalCard } from '../ToolApprovalCard';
import type { ToolStep, ToolStepIsolation } from '@/lib/tree_history';
import zhTW from '@/locales/zh-TW/translation.json';
import en from '@/locales/en/translation.json';

const fetchToukaMock = vi.fn();
vi.mock('@/lib/connections', () => ({
  fetchTouka: (...args: unknown[]) => fetchToukaMock(...args),
  getActiveProxyUrl: () => 'http://proxy.test',
}));

const awaitingStep: ToolStep = {
  id: 'c1',
  tool: 'send_message',
  args: { to: 'Alice', content: 'hello' },
  status: 'awaiting_approval',
  approval: { execId: 'ex-123', risk: 'dangerous', timeout: 180 },
};

beforeEach(() => {
  fetchToukaMock.mockReset();
  fetchToukaMock.mockResolvedValue({ ok: true });
});

describe('ToolApprovalCard (H3)', () => {
  it('renders tool, args preview, risk chip and both action buttons', () => {
    render(<ToolApprovalCard step={awaitingStep} />);
    expect(screen.getByText(/send_message/)).toBeInTheDocument();
    expect(screen.getByText(/Alice/)).toBeInTheDocument();
    expect(screen.getByText(/高風險/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /批准/ })).toBeEnabled();
    expect(screen.getByRole('button', { name: /拒絕/ })).toBeEnabled();
  });

  it('shows a long command in full, wrapped instead of single-line ellipsis (A7)', () => {
    const command = `python -m pytest tests/ ${'-k case '.repeat(40)}--maxfail=1`;
    render(
      <ToolApprovalCard
        step={{ ...awaitingStep, tool: 'execute_command', args: { command } }}
      />,
    );
    const value = screen.getByText(command);
    expect(value.textContent).toBe(command);
    expect(value).not.toHaveClass('truncate');
    expect(value).toHaveClass('whitespace-pre-wrap', 'break-all');
  });

  it('approve posts to /v1/tools/approve/{execId} and locks buttons', async () => {
    render(<ToolApprovalCard step={awaitingStep} />);
    fireEvent.click(screen.getByRole('button', { name: /批准/ }));
    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenCalledWith(
        'http://proxy.test', '/v1/tools/approve/ex-123', { method: 'POST' },
      );
    });
    // 成功送出後按鈕鎖定，終態交給 tool_approval_resolved 事件
    expect(screen.getByRole('button', { name: /批准/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /拒絕/ })).toBeDisabled();
  });

  it('reject posts to /v1/tools/reject/{execId}', async () => {
    render(<ToolApprovalCard step={awaitingStep} />);
    fireEvent.click(screen.getByRole('button', { name: /拒絕/ }));
    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenCalledWith(
        'http://proxy.test', '/v1/tools/reject/ex-123', { method: 'POST' },
      );
    });
  });

  it('failed resolution (404/403) re-enables buttons', async () => {
    fetchToukaMock.mockResolvedValue({
      ok: false,
      clone: () => ({ json: async () => ({ detail: { code: 'TERMINAL_APPROVAL_NOT_FOUND' } }) }),
    });
    render(<ToolApprovalCard step={awaitingStep} />);
    fireEvent.click(screen.getByRole('button', { name: /批准/ }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /批准/ })).toBeEnabled();
    });
  });

  it('resolved step shows decision badge only (no buttons)', () => {
    const resolved: ToolStep = {
      ...awaitingStep,
      status: 'error',
      approval: { ...awaitingStep.approval!, decision: 'rejected' },
    };
    render(<ToolApprovalCard step={resolved} />);
    expect(screen.getByText(/已拒絕/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /批准/ })).not.toBeInTheDocument();
  });

  // 隔離審批的兩種未核准結果（approval_flow 的 verdict）：從別處核准、卡片已解算時不能顯示 key 原文。
  it.each([
    ['isolation_unacknowledged', /沒有確認卡上的隔離標示/, /didn't confirm the isolation shown on the card/],
    ['isolation_incomplete', /卡上看不到完整的執行內容/, /card can't show everything it would run/],
  ] as const)('resolved %s shows its translated badge in both languages', (decision, zhText, enText) => {
    const resolved: ToolStep = {
      ...awaitingStep,
      status: 'error',
      approval: { ...awaitingStep.approval!, decision },
    };
    render(<ToolApprovalCard step={resolved} />);
    expect(screen.getByText(zhText)).toBeInTheDocument();
    const enDecisions = en.chat.tool_approval_card.decision as Record<string, string>;
    expect(enDecisions[decision]).toMatch(enText);
  });
});

describe('ToolStepTracker × approval (H3)', () => {
  it('awaiting step renders yellow row with the inline card', () => {
    const { container } = render(<ToolStepTracker steps={[awaitingStep]} />);
    expect(container.querySelector('.text-status-warning')).toBeTruthy();
    expect(screen.getByText(/等待審批/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /批准/ })).toBeInTheDocument();
  });

  it('timeout decision renders badge via tracker', () => {
    const timedOut: ToolStep = {
      ...awaitingStep,
      status: 'error',
      result: '{"error_class":"permission_denied","message":"timeout","reason":"approval_timeout"}',
      approval: { ...awaitingStep.approval!, decision: 'timeout' },
    };
    render(<ToolStepTracker steps={[timedOut]} />);
    expect(screen.getByText(/逾時，已自動拒絕/)).toBeInTheDocument();
  });
});

describe('ToolApprovalCard × Auto reviewer (ADR-0246 U2c-6)', () => {
  it('shows the reviewer reason and the only-you badge with its explanation', () => {
    render(
      <ToolApprovalCard
        step={{ ...awaitingStep, approval: { ...awaitingStep.approval!, reviewReason: '要求裡沒有提到刪檔', humanOnly: true } }}
      />,
    );
    expect(screen.getByText('審查員轉給你的理由：要求裡沒有提到刪檔')).toBeInTheDocument();
    expect(screen.getByText('只有你能核准')).toBeInTheDocument();
    expect(screen.getByText(/審查員和 YOLO 都不能代為放行/)).toBeInTheDocument();
  });

  it('shows neither when the event did not carry them', () => {
    render(<ToolApprovalCard step={awaitingStep} />);
    expect(screen.queryByText(/審查員轉給你的理由/)).toBeNull();
    expect(screen.queryByText('只有你能核准')).toBeNull();
    expect(screen.queryByText(/管理員設定的 hook/)).toBeNull();
  });
});

describe('ToolApprovalCard × hook ask (ADR-0292)', () => {
  it('labels the hook reason as the hook\'s, not the reviewer\'s', () => {
    render(
      <ToolApprovalCard step={{ ...awaitingStep, approval: { ...awaitingStep.approval!, hookReason: '會動到發行資料夾' } }} />,
    );
    expect(screen.getByText('管理員設定的 hook 要你確認：會動到發行資料夾')).toBeInTheDocument();
    expect(screen.queryByText(/審查員轉給你的理由/)).toBeNull();
  });

  it('still says a hook asked when it gave no reason', () => {
    render(<ToolApprovalCard step={{ ...awaitingStep, approval: { ...awaitingStep.approval!, hookReason: '' } }} />);
    expect(screen.getByText('管理員設定的 hook 要你確認這個動作。')).toBeInTheDocument();
  });
});

describe('ToolApprovalCard × isolation (iso-c2)', () => {
  const withIsolation = (isolation: ToolStepIsolation): ToolStep => ({
    ...awaitingStep,
    tool: 'execute_command',
    args: { command: 'pytest' },
    approval: { ...awaitingStep.approval!, humanOnly: true, isolation },
  });
  const fullFeatures = ['fs', 'tcp', 'ioctl', 'signal', 'abstract_unix'];

  it('reduced isolation with every feature: files and internal services are walled off', () => {
    render(<ToolApprovalCard step={withIsolation({ level: 'none', fallback: 'landlock', features: fullFeatures })} />);
    expect(screen.getByText('隔離降級')).toBeInTheDocument();
    const desc = screen.getByText(/在 Touka 主容器內以檔案隔離執行/);
    // C2-1 沒有執行器：開頭不提執行器，只說這次用不到完整隔離
    expect(desc.textContent).toMatch(/^這次無法使用完整隔離，會在 Touka 主容器內以檔案隔離執行：/);
    expect(desc.textContent).not.toContain('執行器');
    expect(en.chat.tool_approval_card.isolation.degraded_head).not.toMatch(/executor/i);
    expect(desc.textContent).toContain('看不到 Touka 的金鑰、設定、原始碼與其他代理人的資料；連不到 Touka 內部服務');
    expect(desc.textContent).toContain('和 Touka 共用同一個容器與核心');
    expect(desc.textContent).not.toContain('連得到');
    expect(desc.textContent).not.toContain('重啟');
    expect(screen.queryByText('未隔離')).toBeNull();
    expect(screen.getByRole('button', { name: /批准/ })).toBeEnabled();
  });

  it('reduced isolation on an old kernel says internal services are reachable and Touka may restart', () => {
    render(
      <ToolApprovalCard
        step={withIsolation({ level: 'none', fallback: 'landlock', features: ['fs'], reason: 'landlock_abi_insufficient' })}
      />,
    );
    const desc = screen.getByText(/在 Touka 主容器內以檔案隔離執行/);
    expect(desc.textContent).toContain('但擋不住它連到 Touka 內部服務');
    expect(desc.textContent).toContain('可能讓 Touka 重啟');
    expect(desc.textContent).not.toContain('連不到 Touka 內部服務');
    expect(screen.getByText(/^原因：/).textContent).toContain('需要 6.7 以上');
  });

  // 網路設定不安全（比照 ABI 3）：檔案照樣隔離、埠沒擋；卡片不說「核心太舊」，原因那行寫要加的設定
  it.each([
    ['net_tcp_fastopen', 'net.ipv4.tcp_fastopen'],
    ['net_mptcp', 'net.mptcp.enabled'],
  ])('reduced isolation with unsafe network settings (%s) says the ports are not walled off', (reason, sysctl) => {
    const features = ['fs', 'ioctl', 'signal', 'abstract_unix'];
    render(<ToolApprovalCard step={withIsolation({ level: 'none', fallback: 'landlock', features, reason })} />);
    expect(screen.getByText('隔離降級')).toBeInTheDocument();
    expect(screen.queryByText('未隔離')).toBeNull();
    const desc = screen.getByText(/在 Touka 主容器內以檔案隔離執行/);
    expect(desc.textContent).toContain('看不到 Touka 的金鑰、設定、原始碼與其他代理人的資料；但擋不住它連到 Touka 內部服務');
    expect(desc.textContent).not.toContain('核心太舊');
    expect(desc.textContent).not.toContain('重啟');
    const line = screen.getByText(/^原因：/).textContent ?? '';
    expect(line).toContain(`${sysctl}: 0`);
    // 原生 Linux 改的是主機的 sysctl，不是 compose
    expect(line).toMatch(new RegExp(`直接在 Linux 主機上跑[\\s\\S]*${sysctl.replace(/\./g, '\\.')}=0`));
    expect(screen.getByRole('button', { name: /批准/ })).toBeEnabled();
  });

  it('the reduced-isolation wording without tcp no longer blames the kernel in English either', () => {
    expect(en.chat.tool_approval_card.isolation.degraded_tcp_open).not.toMatch(/kernel/i);
    const enReasons = en.chat.tool_approval_card.isolation.reasons as Record<string, string>;
    expect(enReasons.net_tcp_fastopen).toMatch(/directly on a Linux host[\s\S]*net\.ipv4\.tcp_fastopen=0/);
    expect(enReasons.net_mptcp).toMatch(/directly on a Linux host[\s\S]*net\.mptcp\.enabled=0/);
  });

  it('reduced isolation with tcp but no signal isolation only adds the restart warning', () => {
    render(<ToolApprovalCard step={withIsolation({ level: 'none', fallback: 'landlock', features: ['fs', 'tcp'] })} />);
    const desc = screen.getByText(/在 Touka 主容器內以檔案隔離執行/);
    expect(desc.textContent).toContain('連不到 Touka 內部服務');
    expect(desc.textContent).toContain('可能讓 Touka 重啟');
  });

  it('not isolated spells out the blast radius', () => {
    render(<ToolApprovalCard step={withIsolation({ level: 'none', fallback: 'direct', features: [] })} />);
    expect(screen.getByText('未隔離')).toBeInTheDocument();
    expect(screen.getByText(/會用 Touka 自己的權限執行/).textContent).toContain(
      '讀得到全部金鑰（含登入金鑰）、能改 Touka 的設定與原始碼、能讀寫所有代理人的資料、連得到 Touka 內部服務，而且改動在之後仍然有效',
    );
    expect(screen.queryByText('隔離降級')).toBeNull();
    expect(screen.getByRole('button', { name: /批准/ })).toBeEnabled();
  });

  // 後端（U1c 與後續單位）會送的每個原因代碼，都要有自己的說明，不能落到通用退路
  const REASONS: Array<[string, RegExp]> = [
    ['native_platform', /不是在 Linux 的 Docker 裡執行/],
    ['probe_pending', /還在檢查隔離環境能不能用/],
    ['executor_unreachable', /現在連不到它/],
    ['executor_runner_unavailable', /隔離執行器還沒接上/],
    ['isolation_unavailable', /隔離環境現在不能用/],
    ['landlock_abi_too_old', /需要 6\.2 以上/],
    ['landlock_abi_insufficient', /需要 6\.7 以上/],
    ['selftest_failed', /自我檢查沒有通過/],
    ['fs_9p', /隔離功能管不到的磁碟/],
    // 網路設定讓隔離擋不住連線：直接寫出要加的那一行 compose 設定
    ['net_tcp_fastopen', /TCP Fast Open[\s\S]*net\.ipv4\.tcp_fastopen: 0[\s\S]*docker compose up -d/],
    ['net_mptcp', /MPTCP[\s\S]*net\.mptcp\.enabled: 0[\s\S]*docker compose up -d/],
    ['kernel_outdated', /程式和映像不一致[\s\S]*docker compose up -d --build/],
    ['kernel_not_installed', /映像是舊版[\s\S]*docker compose up -d --build/],
    ['kernel_changed', /檢查之後被改過/],
    ['sandbox_state_unconfigured', /compose 設定還沒更新（缺少 touka-sandbox-state）[\s\S]*docker compose up -d --build/],
    ['spec_too_large', /隔離設定超過上限/],
    ['scan_limit', /專案太大（或掃描超過 2 秒）/],
    ['protected_location', /Touka 的受保護位置/],
    ['runner_not_wired', /這個工具還沒接上隔離環境/],
    ['landlock_unavailable', /核心沒有開啟 Landlock/],
    ['isolation_check_failed', /檢查隔離狀態時發生錯誤/],
  ];

  it.each(REASONS)('reason %s has its own explanation', (reason, text) => {
    render(<ToolApprovalCard step={withIsolation({ level: 'none', fallback: 'direct', features: [], reason })} />);
    expect(screen.getByText(text).textContent).toMatch(/^原因：/);
    expect(screen.queryByText(`原因：${reason}`)).toBeNull();
  });

  it('every reason has an English explanation too', () => {
    const zhReasons = zhTW.chat.tool_approval_card.isolation.reasons as Record<string, string>;
    const enReasons = en.chat.tool_approval_card.isolation.reasons as Record<string, string>;
    for (const [reason] of REASONS) {
      expect(zhReasons[reason], reason).toBeTruthy();
      expect(enReasons[reason], reason).toBeTruthy();
    }
    expect(en.chat.tool_approval_card.isolation.reason_details.sandbox_state_unconfigured.not_writable)
      .toContain(
        '1. docker compose down (without -v)\n2. docker volume rm touka_touka-sandbox-state\n3. docker compose up -d --build',
      );
  });

  it('a non-writable sandbox state shows the three repair steps', () => {
    render(
      <ToolApprovalCard
        step={withIsolation({
          level: 'none', fallback: 'direct', features: [],
          reason: 'sandbox_state_unconfigured', reasonDetail: 'not_writable',
        })}
      />,
    );
    const line = screen.getByText(/暫存區（touka-sandbox-state）權限不對/);
    expect(line.textContent).toContain(
      '1. docker compose down（不要加 -v）\n2. docker volume rm touka_touka-sandbox-state\n3. docker compose up -d --build',
    );
    expect(line.textContent).toContain('沒有你的資料');
    expect(line).toHaveClass('whitespace-pre-wrap');
    expect(screen.queryByText(/compose 設定還沒更新/)).toBeNull();
  });

  it('an unknown detail falls back to the reason text', () => {
    render(
      <ToolApprovalCard
        step={withIsolation({
          level: 'none', fallback: 'direct', features: [],
          reason: 'sandbox_state_unconfigured', reasonDetail: 'unset',
        })}
      />,
    );
    expect(screen.getByText(/compose 設定還沒更新/)).toBeInTheDocument();
  });

  it('an unknown reason code falls back to the generic line', () => {
    render(<ToolApprovalCard step={withIsolation({ level: 'none', fallback: 'direct', features: [], reason: 'brand_new_code' })} />);
    expect(screen.getByText('原因：brand_new_code')).toBeInTheDocument();
  });

  it('a reason with an unexpected shape is shown as-is', () => {
    render(<ToolApprovalCard step={withIsolation({ level: 'none', fallback: 'direct', features: [], reason: 'risk.dangerous' })} />);
    expect(screen.getByText('原因：risk.dangerous')).toBeInTheDocument();
  });

  it('an isolated run shows no isolation label', () => {
    render(<ToolApprovalCard step={withIsolation({ level: 'landlock', fallback: null, features: fullFeatures })} />);
    expect(screen.queryByText('隔離降級')).toBeNull();
    expect(screen.queryByText('未隔離')).toBeNull();
    expect(screen.queryByText(/隔離狀態/)).toBeNull();
    expect(screen.queryByText(/原因：/)).toBeNull();
    expect(screen.getByRole('button', { name: /批准/ })).toBeEnabled();
  });

  it('without the field the card looks as before', () => {
    render(<ToolApprovalCard step={awaitingStep} />);
    expect(screen.queryByText('隔離降級')).toBeNull();
    expect(screen.queryByText('未隔離')).toBeNull();
    expect(screen.queryByText(/隔離狀態/)).toBeNull();
    expect(screen.getByRole('button', { name: /批准/ })).toBeEnabled();
  });

  // 後端以核准時送回的標示記同意（isolation_ack）；對不上就回 422、不記同意
  it.each(['landlock', 'direct'] as const)(
    'approving a %s card sends back the fallback it showed',
    async (fallback) => {
      render(<ToolApprovalCard step={withIsolation({ level: 'none', fallback, features: ['fs'] })} />);
      fireEvent.click(screen.getByRole('button', { name: /批准/ }));
      await waitFor(() => {
        expect(fetchToukaMock).toHaveBeenCalledWith('http://proxy.test', '/v1/tools/approve/ex-123', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ payload: { isolation_ack: fallback } }),
        });
      });
    },
  );

  it('approving an isolated card or rejecting a none card sends no body', async () => {
    const { unmount } = render(
      <ToolApprovalCard step={withIsolation({ level: 'landlock', fallback: null, features: fullFeatures })} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /批准/ }));
    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenCalledWith('http://proxy.test', '/v1/tools/approve/ex-123', { method: 'POST' });
    });
    unmount();
    render(<ToolApprovalCard step={withIsolation({ level: 'none', fallback: 'direct', features: [] })} />);
    fireEvent.click(screen.getByRole('button', { name: /拒絕/ }));
    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenLastCalledWith('http://proxy.test', '/v1/tools/reject/ex-123', { method: 'POST' });
    });
  });

  // C2-3 必做 ③：未隔離而卡上沒有完整內容 → 核准鈕停用並說明（後端也不算核准，舊前端照送一樣）
  it('cannot approve an unisolated card that does not show everything that would run', async () => {
    render(<ToolApprovalCard step={withIsolation({ level: 'none', fallback: 'direct', features: [], complete: false })} />);
    expect(screen.getByText('未隔離')).toBeInTheDocument();
    expect(screen.getByText(/內容太長被截斷，或卡上看不到要執行的程式/).textContent)
      .toContain('未隔離時不能在這裡核准');
    const approve = screen.getByRole('button', { name: /批准/ });
    expect(approve).toBeDisabled();
    fireEvent.click(approve);
    fireEvent.click(screen.getByRole('button', { name: /拒絕/ }));
    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenCalledWith('http://proxy.test', '/v1/tools/reject/ex-123', { method: 'POST' });
    });
    expect(fetchToukaMock).toHaveBeenCalledTimes(1);
  });

  it('only an incomplete card carries the incomplete line, in both languages', () => {
    render(<ToolApprovalCard step={withIsolation({ level: 'none', fallback: 'direct', features: [] })} />);
    expect(screen.queryByText(/內容太長被截斷/)).toBeNull();
    expect(screen.getByRole('button', { name: /批准/ })).toBeEnabled();
    expect(zhTW.chat.tool_approval_card.isolation.incomplete).toContain('未隔離時不能在這裡核准');
    expect(en.chat.tool_approval_card.isolation.incomplete).toMatch(/can't be approved here while unisolated/);
  });

  it('cannot approve when the card cannot show which way it will run', async () => {
    render(<ToolApprovalCard step={withIsolation({ level: 'none', fallback: null, features: [], reason: 'probe_pending' })} />);
    expect(screen.getByText('看不懂這次的隔離狀態')).toBeInTheDocument();
    expect(screen.getByText(/還在檢查隔離環境能不能用/)).toBeInTheDocument();
    const approve = screen.getByRole('button', { name: /批准/ });
    expect(approve).toBeDisabled();
    fireEvent.click(approve);
    fireEvent.click(screen.getByRole('button', { name: /拒絕/ }));
    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenCalledWith('http://proxy.test', '/v1/tools/reject/ex-123', { method: 'POST' });
    });
    expect(fetchToukaMock).not.toHaveBeenCalledWith('http://proxy.test', '/v1/tools/approve/ex-123', { method: 'POST' });
  });
});
