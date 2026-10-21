import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { SkillsTab } from '../SkillsTab';
import { Skill } from '@/lib/types';

// Radix Tabs 在 jsdom 不支援 fireEvent.click 切換，替換為原生 state-based 實作
vi.mock('@/components/ui/tabs', async () => {
  const { createElement, useState, createContext, useContext } = await import('react');
  const TabsCtx = createContext({ value: '', setValue: (_: string) => {} });

  const Tabs = ({ children, defaultValue }: { children: React.ReactNode; defaultValue?: string }) => {
    const [value, setValue] = useState(defaultValue ?? '');
    return createElement(TabsCtx.Provider, { value: { value, setValue } }, children);
  };

  const TabsList = ({ children, className }: { children: React.ReactNode; className?: string }) =>
    createElement('div', { role: 'tablist', className }, children);

  const TabsTrigger = ({ children, value, className }: { children: React.ReactNode; value: string; className?: string }) => {
    const ctx = useContext(TabsCtx);
    return createElement('button', {
      role: 'tab',
      'aria-selected': ctx.value === value,
      className,
      onClick: () => ctx.setValue(value),
    }, children);
  };

  const TabsContent = ({ children, value, className }: { children: React.ReactNode; value: string; className?: string }) => {
    const ctx = useContext(TabsCtx);
    return ctx.value === value ? createElement('div', { role: 'tabpanel', className }, children) : null;
  };

  return { Tabs, TabsList, TabsTrigger, TabsContent };
});

// confirmToast 直接執行回呼，便於測試「二次確認後安裝」流程
vi.mock('@/hooks/useConfirmToast', () => ({
  useConfirmToast: () => ({ confirmToast: (_msg: string, cb: () => void) => cb() }),
}));

const mockProps = {
    skills: [
        { id: "skill_1", name: "Python Expert", description: "Expert in Python", system_prompt: "Be a python expert", required_tools: ["shell"], mcp_servers: [], is_system: false },
        { id: "skill_system", name: "Core Guard", description: "System security", system_prompt: "Secure the system", required_tools: [], mcp_servers: [], is_system: true }
    ],
    editingSkill: null,
    isSavingSkill: false,
    setEditingSkill: vi.fn(),
    saveSkill: vi.fn(() => Promise.resolve(true)),
    deleteSkill: vi.fn(() => Promise.resolve(true)),
    exportSkill: vi.fn(() => Promise.resolve(true)),
    installSkillFromUrl: vi.fn(() => Promise.resolve(true)),
    catalogItems: [
        { id: "cat_1", name: "Skill Creator", category: "Dev", description: "Build skills", repo: "anthropics/skills", commit: "abc1234def567890abc1234def567890abc12345", base_path: "skills/skill-creator", files: [{ path: "SKILL.md", sha256: "a".repeat(64) }], author: "Anthropic", license: "Apache-2.0" }
    ],
    installCatalogSkill: vi.fn(() => Promise.resolve(true)),
    getSkill: vi.fn(() => Promise.resolve(null)),
    systemTools: [{ name: "shell", description: "Run shell commands" }, { name: "browser", description: "Browse web pages" }]
};

describe('SkillsTab (Baseline)', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('renders my skills list', () => {
        render(<SkillsTab {...mockProps} />);
        expect(screen.getByText(/Python Expert/i)).toBeDefined();
        expect(screen.getByText(/Core Guard/i)).toBeDefined();
        expect(screen.getByText(/新增自定義技能/i)).toBeDefined();
    });

    it('displays System Protected for system skills', () => {
        render(<SkillsTab {...mockProps} />);
        expect(screen.getByText(/系統保護/)).toBeDefined();
    });

    it('triggers setEditingSkill when clicking add new', () => {
        render(<SkillsTab {...mockProps} />);
        fireEvent.click(screen.getByText(/新增自定義技能/i));
        expect(mockProps.setEditingSkill).toHaveBeenCalled();
    });

    it('renders edit form when editingSkill is provided', () => {
        const editingSkill = mockProps.skills[0];
        render(<SkillsTab {...mockProps} editingSkill={editingSkill} />);
        
        const nameInput = screen.getByPlaceholderText<HTMLInputElement>(/例如：Python 專家/i);
        expect(nameInput.value).toBe('Python Expert');
        expect(screen.getByText(/儲存技能/i)).toBeDefined();
    });

    it('shows executable-script warning and installs catalog skill after confirm', () => {
        render(<SkillsTab {...mockProps} />);
        fireEvent.click(screen.getByText(/技能中心/));
        expect(screen.getByText(/含可執行腳本/i)).toBeDefined();
        expect(screen.getByText(/Skill Creator/i)).toBeDefined();
        fireEvent.click(screen.getByText(/安裝真技能/i));
        expect(mockProps.installCatalogSkill).toHaveBeenCalledWith("cat_1");
    });

    it('opens install modal when clicking install from URL', () => {
        render(<SkillsTab {...mockProps} />);
        fireEvent.click(screen.getByText('從 URL 安裝'));
        expect(screen.getByPlaceholderText(/\.skill/i)).toBeDefined();
    });

    it('triggers exportSkill when clicking export button', () => {
        render(<SkillsTab {...mockProps} />);
        const exportBtns = screen.getAllByLabelText('導出 .skill');
        fireEvent.click(exportBtns[0]);
        expect(mockProps.exportSkill).toHaveBeenCalled();
    });

    it('clicking preview Eye opens the skill preview modal (B4)', async () => {
        render(<SkillsTab {...mockProps} />);
        fireEvent.click(screen.getByRole('button', { name: '預覽技能 Python Expert' }));
        expect(await screen.findByText('指令內容（System Prompt）')).toBeDefined();
    });

    it('race: 關閉預覽後，過期的 getSkill 結果不讓 modal 重現 (B4)', async () => {
        let resolveGet!: (s: Skill | null) => void;
        const deferred = new Promise<Skill | null>((r) => { resolveGet = r; });
        const props = { ...mockProps, getSkill: vi.fn(() => deferred) };
        render(<SkillsTab {...props} />);
        fireEvent.click(screen.getByRole('button', { name: '預覽技能 Python Expert' }));
        expect(await screen.findByText('指令內容（System Prompt）')).toBeDefined();
        // 關閉預覽（reqRef 遞增，作廢進行中的 getSkill）
        fireEvent.click(screen.getAllByRole('button', { name: '關閉' })[0]);
        // 過期的 body 抓取此刻才回來
        resolveGet({ id: 'skill_1', name: 'Python Expert', description: 'x', system_prompt: 'STALE-BODY', required_tools: [], mcp_servers: [], is_system: false });
        await Promise.resolve();
        await Promise.resolve();
        // modal 不得重現、過期 body 不得出現
        expect(screen.queryByText('指令內容（System Prompt）')).toBeNull();
        expect(screen.queryByText(/STALE-BODY/)).toBeNull();
    });
});
