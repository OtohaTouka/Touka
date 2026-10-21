import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { McpHubItem } from '../McpHubItem';

const HUB = {
  id: 'official-fetch',
  name: 'Fetch',
  description: 'Fetch web pages',
  category: 'Dev',
  command: 'npx',
  args: ['-y', '@modelcontextprotocol/server-fetch'],
  icon: 'Globe',
};

describe('McpHubItem 版本狀態', () => {
  it('not_installed：顯示「一鍵安裝」，點擊觸發 onInstall', () => {
    const onInstall = vi.fn();
    render(<McpHubItem hubSrv={HUB} status="not_installed" onInstall={onInstall} />);
    fireEvent.click(screen.getByText(/一鍵安裝/));
    expect(onInstall).toHaveBeenCalledWith(HUB);
  });

  it('up_to_date：顯示「已經安裝」且停用', () => {
    const onInstall = vi.fn();
    render(<McpHubItem hubSrv={HUB} status="up_to_date" onInstall={onInstall} />);
    fireEvent.click(screen.getByText(/已經安裝/));
    expect(onInstall).not.toHaveBeenCalled();
  });

  it('update_available：顯示「可更新」徽章與「更新設定」鈕，點擊觸發 onInstall', () => {
    const onInstall = vi.fn();
    render(<McpHubItem hubSrv={HUB} status="update_available" onInstall={onInstall} />);
    expect(screen.getByText(/可更新/)).toBeDefined();
    fireEvent.click(screen.getByText(/更新設定/));
    expect(onInstall).toHaveBeenCalledWith(HUB);
  });
});
