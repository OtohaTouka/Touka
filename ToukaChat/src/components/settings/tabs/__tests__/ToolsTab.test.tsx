import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ToolsTab } from '../ToolsTab';
import { Tool } from '@/lib/types';

const tools: Tool[] = [
  { name: 'web_search', description: 'Search the web', category: 'Web' },
  { name: 'file_read', description: 'Read a file', category: 'IO' },
  { name: 'mcp__x__do', description: 'an mcp tool', category: 'MCP-x' },
];

describe('ToolsTab B1：搜尋', () => {
  it('預設隱藏 MCP 工具、顯示其餘', () => {
    render(<ToolsTab systemTools={tools} />);
    expect(screen.getByText('web_search')).toBeTruthy();
    expect(screen.getByText('file_read')).toBeTruthy();
  });

  it('輸入關鍵字只顯示匹配工具', () => {
    render(<ToolsTab systemTools={tools} />);
    fireEvent.change(screen.getByLabelText(/搜尋工具/), { target: { value: 'web' } });
    expect(screen.getByText('web_search')).toBeTruthy();
    expect(screen.queryByText('file_read')).toBeNull();
  });

  it('無匹配顯示 no_results', () => {
    render(<ToolsTab systemTools={tools} />);
    fireEvent.change(screen.getByLabelText(/搜尋工具/), { target: { value: 'zzz-none' } });
    expect(screen.getByText('找不到符合搜尋的工具')).toBeTruthy();
  });
});
