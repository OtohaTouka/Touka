import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MySkillsView } from '../MySkillsView';
import { Skill } from '@/lib/types';

const skills: Skill[] = [
  { id: 's1', name: 'Python Expert', description: 'py', system_prompt: '', required_tools: [], mcp_servers: [] },
  { id: 's2', name: 'Writer', description: 'writing helper', system_prompt: '', required_tools: [], mcp_servers: [] },
];

describe('MySkillsView B3：搜尋', () => {
  it('依名稱/描述過濾我的技能', () => {
    render(
      <MySkillsView
        skills={skills}
        onEdit={vi.fn()} onDelete={vi.fn()} onExport={vi.fn()}
        onAddNew={vi.fn()} onInstallFromUrl={vi.fn()} onPreview={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/搜尋我的技能/), { target: { value: 'python' } });
    expect(screen.getByText('Python Expert')).toBeTruthy();
    expect(screen.queryByText('Writer')).toBeNull();
  });
});
