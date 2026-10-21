import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { SecurityTab } from '../SecurityTab';

// Radix Select 在 jsdom 不支援 fireEvent.change，替換為原生 <select>
vi.mock('@/components/ui/select', async () => {
  const { createElement, Fragment } = await import('react');
  const Select = ({ children, onValueChange, defaultValue }: { children: React.ReactNode; onValueChange?: (v: string) => void; defaultValue?: string }) =>
    createElement('select', { defaultValue, onChange: (e: React.ChangeEvent<HTMLSelectElement>) => onValueChange?.(e.target.value) }, children);
  const SelectTrigger = ({ children }: { children: React.ReactNode }) => createElement(Fragment, null, children);
  const SelectValue = ({ placeholder }: { placeholder?: string }) => createElement('option', { value: '' }, placeholder);
  const SelectContent = ({ children }: { children: React.ReactNode }) => createElement(Fragment, null, children);
  const SelectItem = ({ children, value }: { children: React.ReactNode; value: string }) =>
    createElement('option', { value }, children);
  return { Select, SelectTrigger, SelectValue, SelectContent, SelectItem };
});

// Radix Slider 在 jsdom 無法用 fireEvent.change（span 沒有 value setter），替換為原生 <input type="range">
vi.mock('@/components/ui/slider', async () => {
  const { createElement } = await import('react');
  const Slider = ({ onValueChange, value, min = 0, max = 100, step = 1 }: {
    onValueChange?: (v: number[]) => void;
    value?: number[];
    min?: number;
    max?: number;
    step?: number;
  }) =>
    createElement('input', {
      type: 'range',
      role: 'slider',
      value: value?.[0] ?? min,
      min,
      max,
      step,
      onChange: (e: React.ChangeEvent<HTMLInputElement>) => onValueChange?.([Number(e.target.value)]),
    });
  return { Slider };
});

// Mock connections.ts functions
vi.mock('../connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  getActiveConnection: vi.fn(() => ({ protocol: 'openai' })),
  fetchTouka: vi.fn(() => Promise.resolve({
    json: () => Promise.resolve({ data: [] })
  }))
}));

// 審查員區塊的模型清單：不連線（ADR-0246）
vi.mock('@/hooks/settings/useAutoReviewSettings', () => ({
  useAutoReviewSettings: () => ({ models: [], loadFailed: false, selectedModel: '', editable: false, setReviewerModel: vi.fn() }),
}));

// Mock ResizeObserver for Radix UI
global.ResizeObserver = vi.fn().mockImplementation(() => ({
    observe: vi.fn(),
    unobserve: vi.fn(),
    disconnect: vi.fn(),
}));
// Actually, using a class works better for 'is not a constructor' errors
global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};

const mockProps = {
  permissions: {
    roles: {
      // 身分對應的角色下拉選單只列可指派角色（Owner／Base／Debugger；ADR-0245），故用真實角色名。
      Owner: {
        desc: "Owner role",
        allowed_tools: ["*"],
        allowed_mcp_servers: ["*"],
        allowed_skills: ["*"],
        denied_tools: []
      },
      guest: {
        desc: "Guest role",
        allowed_tools: [],
        allowed_mcp_servers: [],
        allowed_skills: [],
        denied_tools: []
      }
    },
    agent_mappings: {},
    system_config: { default_role: "guest", allow_unregistered_agents: false }
  },
  setPermissions: vi.fn(),
  identityMap: {
    default_role: "guest",
    tag_rules: {},
    agent_overrides: {
      "Charlie": { role: "Owner", capabilities: ["heartbeat_awakening"] }
    }
  },
  setIdentityMap: vi.fn(),
  allAgents: [
    { id: "Charlie", name: "Charlie Agent", type: "agent", is_group: false },
    { id: "AgentAlpha", name: "AgentAlpha", type: "agent", is_group: false }
  ],
  autonomyConfig: {
    global_config: { max_pulses_per_session: 5 },
    // ADR-0146：工具輪保險絲（0＝不限）；nudge 舊鍵已隨機制移除
    react_loop: { max_tool_rounds: 10 },
    self_repair: {
      enabled: true,
      max_attempts: 3,
      repair_prompt: "repair"
    },
    sandbox_limits: {
      execution_timeout_sec: 30,
      max_storage_mb: 50,
      max_memory_mb: 256
    }
  },
  setAutonomyConfig: vi.fn(),
  isSavingPermissions: false,
  isSavingIdentity: false,
  isSavingAutonomy: false,
  savePermissions: vi.fn(() => Promise.resolve(true)),
  saveIdentity: vi.fn(() => Promise.resolve(true)),
  saveAutonomy: vi.fn(() => Promise.resolve(true)),
  systemTools: [
    { name: "read_file", description: "Read file contents", category: "Builtin" }
  ],
  mcpServers: [
    { name: "google_search", tools_count: 5, status: 'connected' as const, url: 'http://localhost:3001' }
  ],
  skills: [
    { id: "python_expert", name: "Python Expert", description: "", system_prompt: "", required_tools: [], mcp_servers: [] }
  ]
};

describe('SecurityTab (Baseline)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders RBAC roles and permissions', () => {
    render(<SecurityTab {...mockProps} />);
    expect(screen.getByText(/角色權限矩陣/i)).toBeDefined();
    expect(screen.getAllByText(/OWNER/i)).not.toHaveLength(0);
    expect(screen.getAllByText(/GUEST/i)).not.toHaveLength(0);
  });

  // 「changing auth level」測試已隨 default_auth_level 旋鈕拆除（hitl-unification H1）
  // ——後端從未消費該欄位；HITL 等級改由聊天列 AuthSwitcher 逐 session 設定並持久化。

  it('renders identity mappings table', () => {
    render(<SecurityTab {...mockProps} />);
    expect(screen.getByText(/身分與能力管理/i)).toBeDefined();
    expect(screen.getByText(/Charlie Agent/i)).toBeDefined();
    expect(screen.getAllByText(/AgentAlpha/i)[0]).toBeDefined();
  });

  it('triggers setIdentityMap when changing agent role and does so immutably', () => {
    render(<SecurityTab {...mockProps} />);
    const agentRow = screen.getAllByText(/AgentAlpha/i)[0].closest('tr');
    const select = agentRow?.querySelector('select');
    if (select) {
        fireEvent.change(select, { target: { value: 'Owner' } });
        expect(mockProps.setIdentityMap).toHaveBeenCalled();
        const callArg = mockProps.setIdentityMap.mock.calls[0][0];
        expect(callArg).not.toBe(mockProps.identityMap);
        expect(callArg.agent_overrides).not.toBe(mockProps.identityMap.agent_overrides);
        expect(callArg.agent_overrides.AgentAlpha.role).toBe('Owner');
    }
  });

  it('renders autonomy budget section', () => {
    render(<SecurityTab {...mockProps} />);
    expect(screen.getByText(/自主行為預算/i)).toBeDefined();
    expect(screen.getByText(/10/)).toBeDefined(); // react_loop.max_tool_rounds
    expect(screen.getByText('3')).toBeDefined();  // self_repair.max_attempts（審查員說明也含「3 則」，改精確比對）
  });

  it('triggers setAutonomyConfig when moving sliders and updates nested properties', () => {
    render(<SecurityTab {...mockProps} />);
    const sliders = screen.getAllByRole('slider'); // range inputs
    fireEvent.change(sliders[0], { target: { value: '20' } });
    expect(mockProps.setAutonomyConfig).toHaveBeenCalled();
    const callArg = mockProps.setAutonomyConfig.mock.calls[0][0];
    // ADR-0146：只寫 nested 新鍵（root 遺留雙寫已拆）
    expect(callArg.react_loop.max_tool_rounds).toBe(20);
    expect('max_react_slots' in callArg).toBe(false);
  });

  it('calls save functions when clicking save buttons', async () => {
    render(<SecurityTab {...mockProps} />);
    const saveBtn = screen.getByText(/儲存權限配置/i);
    fireEvent.click(saveBtn);
    expect(mockProps.savePermissions).toHaveBeenCalled();
  });

  it('renders the Auto reviewer section after the autonomy budget', () => {
    render(<SecurityTab {...mockProps} />);
    const budget = screen.getByText(/自主行為預算/);
    const reviewer = screen.getByText('Auto 審查員');
    expect(budget.compareDocumentPosition(reviewer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
