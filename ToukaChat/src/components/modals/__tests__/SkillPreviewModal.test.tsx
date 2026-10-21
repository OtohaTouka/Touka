import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import SkillPreviewModal from '../SkillPreviewModal';
import { Skill, CatalogSkill } from '@/lib/types';

const skill: Skill = {
  id: 's1', name: 'Python Expert', description: 'A python helper',
  system_prompt: 'BE-A-PYTHON-EXPERT-BODY', required_tools: ['shell'], mcp_servers: [], is_system: false,
};

const catalog: CatalogSkill = {
  id: 'c1', name: 'Skill Creator', category: 'Dev', description: 'Build skills',
  repo: 'anthropics/skills', commit: 'abc1234'.padEnd(40, '0'), base_path: 'p',
  files: [{ path: 'SKILL.md', sha256: 'a'.repeat(64) }], author: 'Anthropic', license: 'Apache-2.0',
};

describe('SkillPreviewModal B4', () => {
  it('已安裝技能：顯示指令 body 與需要的工具', () => {
    render(<SkillPreviewModal skill={skill} onClose={vi.fn()} />);
    expect(screen.getByText('指令內容（System Prompt）')).toBeTruthy();
    expect(screen.getByText(/BE-A-PYTHON-EXPERT-BODY/)).toBeTruthy();
    expect(screen.getByText('shell')).toBeTruthy();
  });

  it('已安裝技能 body 載入中：顯示載入字樣', () => {
    render(<SkillPreviewModal skill={skill} bodyLoading onClose={vi.fn()} />);
    expect(screen.getByText('載入指令中…')).toBeTruthy();
  });

  it('目錄技能：顯示安裝提示與授權，不顯示 body 區', () => {
    render(<SkillPreviewModal catalogSkill={catalog} onClose={vi.fn()} />);
    expect(screen.getByText('安裝後即可檢視完整指令內容。')).toBeTruthy();
    expect(screen.getByText(/Apache-2.0/)).toBeTruthy();
    expect(screen.queryByText('指令內容（System Prompt）')).toBeNull();
  });
});
