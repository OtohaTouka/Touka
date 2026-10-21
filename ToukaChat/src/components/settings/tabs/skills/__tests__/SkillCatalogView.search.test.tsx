import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SkillCatalogView } from '../SkillCatalogView';
import { CatalogSkill } from '@/lib/types';

const mk = (id: string, name: string, category: string, description: string): CatalogSkill => ({
  id, name, category, description,
  repo: 'r', commit: 'c'.repeat(40), base_path: 'p', files: [], author: 'A', license: 'MIT',
});

const items = [
  mk('a', 'Python Helper', 'Dev', 'python coding stuff'),
  mk('b', 'Office Doc', 'Office', 'docx writing'),
];

describe('SkillCatalogView B3：搜尋', () => {
  it('依名稱/描述過濾', () => {
    render(<SkillCatalogView skills={[]} catalogItems={items} onInstall={vi.fn()} onPreview={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/搜尋技能/), { target: { value: 'python' } });
    expect(screen.getByText('Python Helper')).toBeTruthy();
    expect(screen.queryByText('Office Doc')).toBeNull();
  });

  it('搜尋無結果顯示 no_results', () => {
    render(<SkillCatalogView skills={[]} catalogItems={items} onInstall={vi.fn()} onPreview={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/搜尋技能/), { target: { value: 'zzz-none' } });
    expect(screen.getByText('找不到符合搜尋的技能')).toBeTruthy();
  });
});
