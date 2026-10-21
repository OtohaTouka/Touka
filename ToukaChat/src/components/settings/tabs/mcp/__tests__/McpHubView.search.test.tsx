import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { McpHubView } from '../McpHubView';
import { McpHubServerView, InstalledMcpConfig } from '@/lib/mcp_version';

const SERVERS: McpHubServerView[] = [
  { id: 'a', name: 'Fetch', description: 'Fetch web pages', category: 'Browser', command: 'uvx', args: [] },
  { id: 'b', name: 'Git', description: 'Git operations', category: 'Dev', command: 'uvx', args: [] },
];

const baseProps = {
  mcpConfig: {} as InstalledMcpConfig,
  mcpHubServers: SERVERS,
  syncMcpHub: vi.fn(() => Promise.resolve(true)),
  setSelectedHubServer: vi.fn(),
};

describe('McpHubView B2：文字搜尋', () => {
  it('依名稱/描述過濾', () => {
    render(<McpHubView {...baseProps} />);
    fireEvent.change(screen.getByLabelText(/搜尋 MCP 工具/), { target: { value: 'git' } });
    expect(screen.getByText('Git')).toBeTruthy();
    expect(screen.queryByText('Fetch')).toBeNull();
  });

  it('搜尋與分類組合（分類 Dev + 搜尋 fetch → 無結果）', () => {
    render(<McpHubView {...baseProps} />);
    fireEvent.click(screen.getByRole('button', { name: 'Dev' }));
    fireEvent.change(screen.getByLabelText(/搜尋 MCP 工具/), { target: { value: 'fetch' } });
    expect(screen.getByText('找不到符合搜尋的 MCP 工具')).toBeTruthy();
  });

  it('搜尋無結果顯示 empty_search', () => {
    render(<McpHubView {...baseProps} />);
    fireEvent.change(screen.getByLabelText(/搜尋 MCP 工具/), { target: { value: 'zzz-none' } });
    expect(screen.getByText('找不到符合搜尋的 MCP 工具')).toBeTruthy();
  });

  it('0 個伺服器：只顯示 hub_unreachable，不重複顯示 empty_category', () => {
    render(<McpHubView {...baseProps} mcpHubServers={[]} />);
    expect(screen.getByText(/無法連線到 MCP Hub/)).toBeTruthy();
    expect(screen.queryByText('此分類下目前沒有工具')).toBeNull();
  });
});
