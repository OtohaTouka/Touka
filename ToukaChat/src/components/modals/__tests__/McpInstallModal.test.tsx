import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import { McpInstallModal } from '../McpInstallModal';

const SERVER = {
  id: 'official-fetch',
  name: 'Fetch',
  command: 'npx',
  args: ['-y', '@modelcontextprotocol/server-fetch'],
  env_requirements: [],
} as never;

const REMOTE_SERVER = {
  id: 'registry-acme/hosted',
  name: 'Acme Hosted',
  url: 'https://acme.example/mcp',
  headers: { Authorization: '${MCP_AUTHORIZATION}' },
  args: [],
  env_requirements: ['MCP_AUTHORIZATION'],
} as never;

function renderModal(server: never) {
  return render(
    <McpInstallModal
      isOpen
      server={server}
      onClose={vi.fn()}
      onConfirm={vi.fn(() => Promise.resolve())}
    />,
  );
}

describe('McpInstallModal 安全警示對等', () => {
  it('顯示「會在伺服器上執行的外部指令」紅色警示與「我了解風險，安裝」確認鈕', () => {
    renderModal(SERVER);
    expect(screen.getByText(/會在伺服器上執行的外部指令/)).toBeDefined();
    expect(screen.getByText(/我了解風險，安裝/)).toBeDefined();
  });

  it('不再提供安全掃描鈕，指令本身就是審核標的', () => {
    renderModal(SERVER);
    expect(screen.queryByRole('button', { name: /安全掃描/ })).toBeNull();
    expect(screen.getByText(/@modelcontextprotocol\/server-fetch/)).toBeDefined();
  });
});

describe('McpInstallModal remote 條目（ADR-0192）', () => {
  it('顯示端點 URL 而非本地指令，且不出現 undefined', () => {
    const { container } = renderModal(REMOTE_SERVER);
    expect(screen.getByText(/https:\/\/acme\.example\/mcp/)).toBeDefined();
    // 舊版會渲染出 `undefined `（command 為空）——這是使用者看得到的破綻。
    expect(container.textContent).not.toMatch(/undefined/);
  });

  it('風險文案講的是憑證外送，而不是本機執行指令', () => {
    renderModal(REMOTE_SERVER);
    // remote 不在本機起任何進程，沿用 stdio 文案等於對使用者說謊。
    expect(screen.queryByText(/會在伺服器上執行的外部指令/)).toBeNull();
    expect(screen.getByText(/把你填入的憑證送到下列外部端點/)).toBeDefined();
  });
});
