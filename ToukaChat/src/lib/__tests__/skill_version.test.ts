import { describe, it, expect } from 'vitest';
import { computeCatalogStatus } from '../skill_version';
import { CatalogSkill, Skill } from '@/lib/types';

const ENTRY: CatalogSkill = {
  id: 'mcp-builder',
  name: 'MCP Builder',
  category: 'Dev',
  description: 'd',
  repo: 'anthropics/skills',
  commit: 'a'.repeat(40),
  base_path: 'skills/mcp-builder',
  files: [{ path: 'SKILL.md', sha256: 'a'.repeat(64) }],
  author: 'Anthropic',
  license: 'Apache-2.0',
};

const skill = (over: Partial<Skill>): Skill => ({
  id: 'mcp-builder',
  name: 'x',
  description: '',
  system_prompt: '',
  required_tools: [],
  mcp_servers: [],
  ...over,
});

describe('computeCatalogStatus', () => {
  it('未安裝 → not_installed', () => {
    expect(computeCatalogStatus(ENTRY, [])).toBe('not_installed');
  });

  it('已安裝且 commit 一致 → up_to_date', () => {
    expect(computeCatalogStatus(ENTRY, [skill({ source_commit: 'a'.repeat(40) })])).toBe(
      'up_to_date',
    );
  });

  it('已安裝但 commit 不同 → update_available', () => {
    expect(computeCatalogStatus(ENTRY, [skill({ source_commit: 'b'.repeat(40) })])).toBe(
      'update_available',
    );
  });

  it('已安裝但無 provenance（舊版/URL）→ up_to_date（不催更新）', () => {
    expect(computeCatalogStatus(ENTRY, [skill({})])).toBe('up_to_date');
  });
});
