import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { SkillCatalogItem } from '../SkillCatalogItem';
import { CatalogSkill } from '@/lib/types';

const ENTRY: CatalogSkill = {
  id: 'mcp-builder',
  name: 'MCP Builder',
  category: 'Dev',
  description: 'Build MCP servers.',
  repo: 'anthropics/skills',
  commit: 'abcdef0'.padEnd(40, '0'),
  base_path: 'skills/mcp-builder',
  files: [{ path: 'SKILL.md', sha256: 'a'.repeat(64) }],
  author: 'Anthropic',
  license: 'Apache-2.0',
};

describe('SkillCatalogItem 版本狀態', () => {
  it('not_installed：顯示安裝鈕，點擊帶 isUpgrade=false', () => {
    const onInstall = vi.fn();
    render(<SkillCatalogItem catalogSkill={ENTRY} status="not_installed" onInstall={onInstall} onPreview={vi.fn()} />);
    fireEvent.click(screen.getByText(/安裝真技能/));
    expect(onInstall).toHaveBeenCalledWith(ENTRY, false);
  });

  it('up_to_date：顯示「已安裝（最新）」且停用', () => {
    const onInstall = vi.fn();
    render(<SkillCatalogItem catalogSkill={ENTRY} status="up_to_date" onInstall={onInstall} onPreview={vi.fn()} />);
    const btn = screen.getByText(/已安裝（最新）/);
    fireEvent.click(btn);
    expect(onInstall).not.toHaveBeenCalled();
  });

  it('update_available：顯示「可更新」徽章與升級鈕，點擊帶 isUpgrade=true', () => {
    const onInstall = vi.fn();
    render(
      <SkillCatalogItem catalogSkill={ENTRY} status="update_available" onInstall={onInstall} onPreview={vi.fn()} />,
    );
    expect(screen.getByText(/可更新/)).toBeDefined();
    fireEvent.click(screen.getByText(/升級至新版/));
    expect(onInstall).toHaveBeenCalledWith(ENTRY, true);
  });
});
