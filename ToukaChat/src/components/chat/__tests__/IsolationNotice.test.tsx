/**
 * iso-c2 C2-2（設計 2.3 版 §2.8）：審批卡的隔離說明。
 *
 * 開頭＝原因子句（決定降級去處的那個原因）＋主句（看降級去處）；結尾看 features 與擋不全的原因；
 * 原因那幾行寫補救方式（網路設定不安全時，就算子句寫的是專案也照寫要加的覆寫設定）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { IsolationNotice } from '../IsolationNotice';
import { ToolApprovalCard } from '../ToolApprovalCard';
import type { ToolStep, ToolStepIsolation } from '@/lib/tree_history';
import zhTW from '@/locales/zh-TW/translation.json';
import en from '@/locales/en/translation.json';

const fetchToukaMock = vi.fn();
vi.mock('@/lib/connections', () => ({
  fetchTouka: (...args: unknown[]) => fetchToukaMock(...args),
  getActiveProxyUrl: () => 'http://proxy.test',
}));

beforeEach(() => {
  fetchToukaMock.mockReset();
  fetchToukaMock.mockResolvedValue({ ok: true });
});

const NO_TCP = ['fs', 'ioctl', 'signal', 'abstract_unix'];
const lead = () => screen.getByTestId('isolation-lead').textContent ?? '';
const reasonLines = () => screen.getAllByText(/^原因：/).map(line => line.textContent ?? '');
const notice = (isolation: ToolStepIsolation) => render(<IsolationNotice isolation={isolation} />);

describe('IsolationNotice — executor deployments (C2-2)', () => {
  it.each([
    'executor_unreachable', 'executor_auth_failed', 'executor_version_mismatch',
    'executor_roots_mismatch', 'executor_selftest_failed', 'executor_restarted', 'executor_rejected',
  ])('the executor being down (%s) leads the card', reason => {
    notice({ level: 'none', fallback: 'landlock', features: NO_TCP, reason });
    expect(lead()).toMatch(/^執行器目前不能用，這次會在 Touka 主容器內以檔案隔離執行：/);
    expect(reasonLines()).toHaveLength(1);
    expect(reasonLines()[0]).not.toContain(`原因：${reason}`);
  });

  it.each(['probe_pending', 'kernel_outdated'])('with the executor expected, %s also means it is down', reason => {
    const { unmount } = notice({ level: 'none', fallback: 'landlock', features: NO_TCP, reason, expected: 'executor' });
    expect(lead()).toMatch(/^執行器目前不能用，/);
    unmount();
    notice({ level: 'none', fallback: 'landlock', features: NO_TCP, reason, expected: 'landlock' });
    expect(lead()).toMatch(/^這次無法使用完整隔離，/);
  });

  it('a bound project with unsafe network settings names the project, then the network fix', () => {
    notice({
      level: 'none', fallback: 'landlock', features: NO_TCP, reason: 'project_not_in_executor',
      expected: 'executor', net: 'net_mptcp',
    });
    expect(lead()).toMatch(/^這個專案（或資料夾）不在執行器裡，這次會在 Touka 主容器內以檔案隔離執行：/);
    expect(lead()).toContain('；但擋不住它連到 Touka 內部服務與本機上游埠');
    const [project, net] = reasonLines();
    expect(project).toContain('專案資料夾沒有掛進執行器');
    expect(net).toContain('net.mptcp.enabled: 0');
    expect(net).toContain('touka-proxy 與 touka-executor');
  });

  it('an unreadable connections.json without a project still runs in the executor', () => {
    notice({
      level: 'none', fallback: 'executor', features: ['fs'], reason: 'block_ports_incomplete',
      expected: 'executor', blockPortsIssue: 'block_ports_incomplete', localIssue: 'probe_pending',
    });
    expect(screen.getByText('隔離降級')).toBeInTheDocument();
    expect(lead()).toMatch(
      /^Touka 讀不到自訂連線的設定（connections\.json），這次會在執行器裡執行：看不到 Touka 的金鑰、設定、原始碼與其他代理人的資料/,
    );
    expect(lead()).toContain('；Touka 自己的埠照樣擋，但自訂連線與 MCP 的上游埠可能沒擋');
    expect(lead()).not.toContain('擋不住它連到 Touka 內部服務');
    expect(lead()).not.toContain('同一個容器');
    expect(lead()).toContain('和 Touka 在不同容器');
    expect(lead()).not.toContain('重啟');
    const lines = reasonLines();
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain('修好檔案的內容或權限之後，約半分鐘內會自動恢復');
    expect(lines[0]).toContain('touka-init');
  });

  it('too many ports to block says so and how to recover', () => {
    notice({
      level: 'none', fallback: 'executor', features: NO_TCP, reason: 'block_ports_overflow',
      expected: 'executor', blockPortsIssue: 'block_ports_overflow',
    });
    expect(lead()).toMatch(/^要封鎖的埠太多，這次會在執行器裡執行：/);
    expect(reasonLines()[0]).toContain('減少自訂連線或 MCP 的上游位址，再重新啟動 Touka');
  });

  it('unsafe network settings in the executor keep it as the fallback and name both services', () => {
    notice({
      level: 'none', fallback: 'executor', features: NO_TCP, reason: 'net_tcp_fastopen',
      expected: 'executor', net: 'net_tcp_fastopen',
    });
    expect(lead()).toMatch(/^這台主機的網路設定讓有些連線擋不住，這次會在執行器裡執行：/);
    expect(lead()).toContain('；但擋不住它連到 Touka 內部服務與本機上游埠');
    const lines = reasonLines();
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain('net.ipv4.tcp_fastopen: 0');
    expect(lines[0]).toContain('touka-proxy 與 touka-executor');
  });

  it('network and block-list problems together list both endings and both fixes', () => {
    notice({
      level: 'none', fallback: 'executor', features: NO_TCP, reason: 'net_mptcp', expected: 'executor',
      net: 'net_mptcp', blockPortsIssue: 'block_ports_incomplete',
    });
    expect(lead()).toContain('；但擋不住它連到 Touka 內部服務與本機上游埠');
    expect(lead()).toContain('；自訂連線與 MCP 的上游埠也沒有全部列進封鎖名單');
    expect(lead()).not.toContain('Touka 自己的埠照樣擋');
    const lines = reasonLines();
    expect(lines).toHaveLength(2);
    expect(lines[0]).toContain('net.mptcp.enabled: 0');
    expect(lines[1]).toContain('connections.json');
  });

  it('an old kernel with an incomplete block list does not claim Touka ports are blocked', () => {
    notice({
      level: 'none', fallback: 'landlock', features: ['fs'], reason: 'landlock_abi_insufficient',
      blockPortsIssue: 'block_ports_incomplete',
    });
    expect(lead()).toContain('但擋不住它連到 Touka 內部服務與本機上游埠');
    expect(lead()).not.toContain('Touka 自己的埠照樣擋');
  });

  it('when the fallback is direct the local cause comes first', () => {
    notice({
      level: 'none', fallback: 'direct', features: [], reason: 'executor_unreachable',
      expected: 'executor', localIssue: 'landlock_unavailable',
    });
    expect(screen.getByText('未隔離')).toBeInTheDocument();
    const [local, executor] = reasonLines();
    expect(local).toContain('核心沒有開啟 Landlock');
    expect(executor).toContain('現在連不到它');
  });

  // U2e：直譯器不可信時本機 Landlock 不能用 → 降級只能未隔離；landlock 部署是原因，executor 部署在 local_issue
  it('an untrusted interpreter on a landlock deployment is not isolated and says why', () => {
    notice({ level: 'none', fallback: 'direct', features: [], reason: 'interpreter_untrusted', expected: 'landlock' });
    expect(screen.getByText('未隔離')).toBeInTheDocument();
    expect(screen.queryByTestId('isolation-lead')).toBeNull();
    const lines = reasonLines();
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain('啟動隔離的 Python 不可信');
  });

  it('an untrusted interpreter behind a down executor comes first on the card', () => {
    notice({
      level: 'none', fallback: 'direct', features: [], reason: 'executor_unreachable', expected: 'executor',
      localIssue: 'interpreter_untrusted',
    });
    const [local, executor] = reasonLines();
    expect(local).toContain('啟動隔離的 Python 不可信');
    expect(executor).toContain('現在連不到它');
  });

  it('a non-executor deployment keeps the C2-1 net fix on touka-proxy only', () => {
    notice({ level: 'none', fallback: 'landlock', features: NO_TCP, reason: 'net_mptcp', expected: 'landlock' });
    expect(lead()).toMatch(/^這次無法使用完整隔離，會在 Touka 主容器內以檔案隔離執行：/);
    expect(reasonLines()[0]).toContain('touka-proxy 加上');
    expect(reasonLines()[0]).not.toContain('touka-executor');
  });

  const NEW_REASONS: Array<[string, RegExp]> = [
    ['executor_auth_failed', /通行權杖/],
    ['executor_version_mismatch', /版本不一致[\s\S]*docker compose up -d --build/],
    ['executor_roots_mismatch', /掛載的資料夾[\s\S]*docker compose up -d --build/],
    ['executor_selftest_failed', /執行器的自我檢查沒有通過/],
    ['executor_restarted', /重新啟動過/],
    ['executor_rejected', /執行器拒絕了這條指令/],
    ['agent_dir_not_in_executor', /代理人的資料夾不在執行器裡/],
    ['project_not_in_executor', /專案資料夾沒有掛進執行器/],
    ['block_ports_incomplete', /約半分鐘內會自動恢復/],
    ['block_ports_overflow', /超過 1024 個/],
    ['interpreter_untrusted', /啟動隔離的 Python 不可信[\s\S]*docker compose up -d --build[\s\S]*Python 的安裝位置/],
  ];

  it.each(NEW_REASONS)('reason %s has its own explanation in both languages', (reason, text) => {
    notice({ level: 'none', fallback: 'direct', features: [], reason });
    expect(screen.getByText(text).textContent).toMatch(/^原因：/);
    const zhReasons = zhTW.chat.tool_approval_card.isolation.reasons as Record<string, string>;
    const enReasons = en.chat.tool_approval_card.isolation.reasons as Record<string, string>;
    expect(zhReasons[reason]).toBeTruthy();
    expect(enReasons[reason]).toBeTruthy();
  });
});

describe('ToolApprovalCard — executor fallback (C2-2)', () => {
  const step: ToolStep = {
    id: 'c1', tool: 'execute_command', args: { command: 'pytest' }, status: 'awaiting_approval',
    approval: {
      execId: 'ex-9', risk: 'dangerous', timeout: 180, humanOnly: true,
      isolation: { level: 'none', fallback: 'executor', features: NO_TCP, reason: 'net_mptcp', expected: 'executor' },
    },
  };

  it('approving an executor card sends back the executor fallback it showed', async () => {
    render(<ToolApprovalCard step={step} />);
    fireEvent.click(screen.getByRole('button', { name: /批准/ }));
    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenCalledWith('http://proxy.test', '/v1/tools/approve/ex-9', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payload: { isolation_ack: 'executor' } }),
      });
    });
  });
});
