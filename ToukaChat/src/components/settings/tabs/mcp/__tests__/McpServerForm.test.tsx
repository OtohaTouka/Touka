import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

const mockConfirm = vi.fn();
vi.mock('@/hooks/useConfirmToast', () => ({ useConfirmToast: () => ({ confirmToast: mockConfirm }) }));
vi.mock('sonner', () => ({ toast: { warning: vi.fn(), error: vi.fn(), success: vi.fn() } }));

import { toast } from 'sonner';
import { McpServerForm } from '../McpServerForm';

function fillNameCommand() {
  const boxes = screen.getAllByRole('textbox');
  fireEvent.change(boxes[0], { target: { value: 'srv' } }); // name
  fireEvent.change(boxes[1], { target: { value: 'npx' } }); // command
  return boxes;
}

describe('McpServerForm 信任警告 + 二次確認（P2）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('顯示 ShieldAlert 信任警告橫幅', () => {
    render(<McpServerForm mcpConfig={{}} editingMcpKey={null} onSave={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByText(/會在伺服器/)).toBeTruthy();
  });

  it('按手動加入 → 先跳二次確認，確認後才呼叫 onSave', () => {
    const onSave = vi.fn().mockResolvedValue(true);
    render(<McpServerForm mcpConfig={{}} editingMcpKey={null} onSave={onSave} onCancel={vi.fn()} />);
    fillNameCommand();
    fireEvent.click(screen.getByRole('button', { name: /手動加入/ }));
    expect(mockConfirm).toHaveBeenCalledTimes(1);
    expect(onSave).not.toHaveBeenCalled();
    // 模擬使用者點「確認」action
    const onConfirm = mockConfirm.mock.calls[0][1] as () => void;
    onConfirm();
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it('無效 env JSON → toast.error，不進確認、不存檔', () => {
    const onSave = vi.fn();
    render(<McpServerForm mcpConfig={{}} editingMcpKey={null} onSave={onSave} onCancel={vi.fn()} />);
    const boxes = fillNameCommand();
    fireEvent.change(boxes[3], { target: { value: 'not-json' } }); // env（非 JSON）
    fireEvent.click(screen.getByRole('button', { name: /手動加入/ }));
    expect(toast.error).toHaveBeenCalled();
    expect(mockConfirm).not.toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe('McpServerForm remote 形狀（ADR-0194）', () => {
  beforeEach(() => vi.clearAllMocks());

  const REMOTE_CFG = {
    'acme-hosted': {
      url: 'https://acme.example/mcp',
      headers: { Authorization: 'Bearer ${MCP_AUTHORIZATION}' },
      env: { MCP_AUTHORIZATION: 'Bearer secret' },
      enabled: true,
    },
  };

  it('編輯 remote 條目時載入 url/headers，而不是把它當成空 command 的 stdio', () => {
    // 批次 2 的缺口：表單只認 command，remote 條目一進編輯態就顯示空指令，
    // 使用者連補憑證都做不到（存檔鈕因 command 空而永遠 disabled）。
    render(
      <McpServerForm
        mcpConfig={REMOTE_CFG}
        editingMcpKey="acme-hosted"
        onSave={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByDisplayValue('https://acme.example/mcp')).toBeTruthy();
    // headers 與 env 都含 MCP_AUTHORIZATION，故以各自的完整 JSON 精確比對。
    expect(
      screen.getByDisplayValue(JSON.stringify(REMOTE_CFG['acme-hosted'].headers)),
    ).toBeTruthy();
    expect(
      screen.getByDisplayValue(JSON.stringify(REMOTE_CFG['acme-hosted'].env)),
    ).toBeTruthy();
  });

  it('編輯 remote 條目存檔後仍是 remote（url/headers 不被 command 覆蓋）', () => {
    const onSave = vi.fn().mockResolvedValue(true);
    render(
      <McpServerForm
        mcpConfig={REMOTE_CFG}
        editingMcpKey="acme-hosted"
        onSave={onSave}
        onCancel={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /完成編輯|手動加入/ }));
    const onConfirm = mockConfirm.mock.calls[0][1] as () => void;
    onConfirm();
    const saved = onSave.mock.calls[0][0]['acme-hosted'];
    expect(saved.url).toBe('https://acme.example/mcp');
    expect(saved.headers).toEqual({ Authorization: 'Bearer ${MCP_AUTHORIZATION}' });
    expect(saved.command).toBeUndefined();
  });

  it('切到遠端模式時，新增鈕以 url 而非 command 作為必填', () => {
    const onSave = vi.fn().mockResolvedValue(true);
    render(<McpServerForm mcpConfig={{}} editingMcpKey={null} onSave={onSave} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /遠端/ }));
    const boxes = screen.getAllByRole('textbox');
    fireEvent.change(boxes[0], { target: { value: 'zapier' } });
    fireEvent.change(boxes[1], { target: { value: 'https://mcp.example.com/s/abc/mcp' } });
    fireEvent.click(screen.getByRole('button', { name: /手動加入/ }));
    const onConfirm = mockConfirm.mock.calls[0][1] as () => void;
    onConfirm();
    expect(onSave.mock.calls[0][0]['zapier'].url).toBe('https://mcp.example.com/s/abc/mcp');
  });

  it('無效 headers JSON → toast.error，不進確認、不存檔', () => {
    const onSave = vi.fn();
    render(<McpServerForm mcpConfig={{}} editingMcpKey={null} onSave={onSave} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /遠端/ }));
    const boxes = screen.getAllByRole('textbox');
    fireEvent.change(boxes[0], { target: { value: 'srv' } });
    fireEvent.change(boxes[1], { target: { value: 'https://x.example/mcp' } });
    fireEvent.change(boxes[2], { target: { value: 'not-json' } }); // headers
    fireEvent.click(screen.getByRole('button', { name: /手動加入/ }));
    expect(toast.error).toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
  });
});

describe('McpServerForm 工具治理欄位保全（ADR-0195 軸 A）', () => {
  beforeEach(() => vi.clearAllMocks());

  const GOVERNED = {
    stripe: {
      command: 'npx',
      args: ['-y', '@stripe/mcp@0.3.3'],
      enabled: true,
      enabled_tools: ['list_customers'],
      disabled_tools: ['create_refund'],
      cache_enabled: false,
      cache_max_entries: 41,
      cache_default_ttl_ms: 1234,
      subscriptions_enabled: false,
      subscription_max_backoff_seconds: 17,
      approved_header_tools: ['list_customers'],
      icons_enabled: false,
      telemetry_enabled: false,
      auth_mode: 'oauth_client_credentials' as const,
      auth_profile_id: 'stripe-workload',
      workload_authority_kind: 'direct' as const,
      workload_delegation_id: null,
    },
  };

  it('編輯既有條目存檔後，enabled_tools/disabled_tools 不被抹除', () => {
    // 本表單以字面量重建整個條目，未列名的欄位一律消失（vault_jail_arg 那次
    // 「政策因例行操作 fail open」的同型缺陷）。改一次端點就把 create_refund
    // 重新打開，且畫面顯示存檔成功。
    const onSave = vi.fn().mockResolvedValue(true);
    render(
      <McpServerForm
        mcpConfig={GOVERNED}
        editingMcpKey="stripe"
        onSave={onSave}
        onCancel={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /完成編輯|手動加入/ }));
    (mockConfirm.mock.calls[0][1] as () => void)();
    const saved = onSave.mock.calls[0][0]['stripe'];
    expect(saved.enabled_tools).toEqual(['list_customers']);
    expect(saved.disabled_tools).toEqual(['create_refund']);
  });

  it('編輯既有條目後保留全部未修改的認證、能力與安全欄位', () => {
    const onSave = vi.fn().mockResolvedValue(true);
    render(
      <McpServerForm
        mcpConfig={GOVERNED}
        editingMcpKey="stripe"
        onSave={onSave}
        onCancel={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /完成編輯|手動加入/ }));
    (mockConfirm.mock.calls[0][1] as () => void)();
    const saved = onSave.mock.calls[0][0]['stripe'];
    expect(saved).toMatchObject({
      cache_enabled: false,
      cache_max_entries: 41,
      cache_default_ttl_ms: 1234,
      subscriptions_enabled: false,
      subscription_max_backoff_seconds: 17,
      approved_header_tools: ['list_customers'],
      icons_enabled: false,
      telemetry_enabled: false,
      auth_mode: 'oauth_client_credentials',
      auth_profile_id: 'stripe-workload',
      workload_authority_kind: 'direct',
      workload_delegation_id: null,
    });
  });

  it('新增條目不得送出 enabled_tools: []（那是「明示全關」，比丟失更糟）', () => {
    // 空陣列會穿過後端的自磁碟還原分支直接落盤＝新裝的伺服器一個工具都不能用。
    const onSave = vi.fn().mockResolvedValue(true);
    render(<McpServerForm mcpConfig={{}} editingMcpKey={null} onSave={onSave} onCancel={vi.fn()} />);
    fillNameCommand();
    fireEvent.click(screen.getByRole('button', { name: /手動加入/ }));
    (mockConfirm.mock.calls[0][1] as () => void)();
    expect(onSave.mock.calls[0][0]['srv'].enabled_tools).toBeUndefined();
  });
});
