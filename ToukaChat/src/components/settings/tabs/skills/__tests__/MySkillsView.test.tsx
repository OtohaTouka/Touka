import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MySkillsView } from '../MySkillsView';

describe('MySkillsView a11y（P3）', () => {
  it('「新增自定義技能」是可鍵盤操作的 button（非 div）', () => {
    render(
      <MySkillsView
        skills={[]}
        onEdit={vi.fn()}
        onDelete={vi.fn()}
        onExport={vi.fn()}
        onAddNew={vi.fn()}
        onInstallFromUrl={vi.fn()}
        onPreview={vi.fn()}
      />,
    );
    const btn = screen.getByRole('button', { name: /新增自定義技能/ });
    expect(btn.tagName).toBe('BUTTON');
  });
});
