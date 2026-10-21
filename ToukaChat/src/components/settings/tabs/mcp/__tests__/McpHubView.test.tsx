import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { McpHubView } from '../McpHubView';
import { McpHubServerView, InstalledMcpConfig } from '@/lib/mcp_version';

// 分類頁籤修復回歸測試：頁籤由 hub 資料衍生（非硬編），故只出現資料中實際存在的分類，
// 且點選後正確過濾。原 bug：前端硬編 Browser/Dev/File/... 但後端只給 Official/Community，
// 導致除「全部」外所有頁籤永遠空白。
const SERVERS: McpHubServerView[] = [
  {
    id: 'official-fetch', name: 'Fetch', server_key: 'fetch', description: 'Fetch web pages',
    category: 'Browser', command: 'uvx', args: ['mcp-server-fetch'],
  },
  {
    id: 'official-git', name: 'Git', server_key: 'git', description: 'Git operations',
    category: 'Dev', command: 'uvx', args: ['mcp-server-git'],
  },
];

const baseProps = {
  mcpConfig: {} as InstalledMcpConfig,
  mcpHubServers: SERVERS,
  syncMcpHub: vi.fn(() => Promise.resolve(true)),
  setSelectedHubServer: vi.fn(),
};

describe('McpHubView category tabs (derived from data)', () => {
  it('renders the All tab plus only the categories present in data', () => {
    render(<McpHubView {...baseProps} />);
    expect(screen.getByRole('button', { name: '全部 (All)' })).toBeDefined();
    expect(screen.getByRole('button', { name: 'Browser' })).toBeDefined();
    expect(screen.getByRole('button', { name: 'Dev' })).toBeDefined();
    // 資料裡沒有的分類不應出現頁籤（杜絕原本永遠空白的硬編頁籤）
    expect(screen.queryByRole('button', { name: 'Database' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Utils' })).toBeNull();
  });

  it('shows all servers under the default All tab', () => {
    render(<McpHubView {...baseProps} />);
    expect(screen.getByText('Fetch')).toBeDefined();
    expect(screen.getByText('Git')).toBeDefined();
  });

  it('filters to the selected category when a tab is clicked', () => {
    render(<McpHubView {...baseProps} />);
    fireEvent.click(screen.getByRole('button', { name: 'Dev' }));
    expect(screen.getByText('Git')).toBeDefined();
    expect(screen.queryByText('Fetch')).toBeNull();
  });
});

// F6 徽章回歸（ADR-0190）：server_config 鍵＝derive_server_key(name)（'git'），
// 非 hub.id（'official-git'）——徽章必須以 server_key 對得上真實安裝鍵。
describe('McpHubView installed badge (config 鍵＝衍生鍵，非 hub.id)', () => {
  it('config 以衍生鍵標記已裝且配方一致 → 該卡顯示「已安裝」', () => {
    const cfg: InstalledMcpConfig = {
      git: { command: 'uvx', args: ['mcp-server-git'] },
    };
    render(<McpHubView {...baseProps} mcpConfig={cfg} />);
    expect(screen.getByText('已安裝')).toBeDefined();
  });

  it('config 配方漂移 → 該卡顯示「可更新」', () => {
    const cfg: InstalledMcpConfig = {
      git: { command: 'uvx', args: ['mcp-server-git', '--repository', '/x'] },
    };
    render(<McpHubView {...baseProps} mcpConfig={cfg} />);
    expect(screen.getByText('可更新')).toBeDefined();
  });
});
