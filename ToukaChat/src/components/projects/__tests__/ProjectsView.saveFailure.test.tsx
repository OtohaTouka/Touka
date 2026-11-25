/**
 * 專案頁存檔被拒絕時（ADR-0332：專用空間的登記規則），提示說出原因、表單保持開啟；
 * 成功時表單收起。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { toast } from 'sonner';
import i18n from '@/lib/i18n';
import { ProjectsView } from '../ProjectsView';

const saveProject = vi.fn();

vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock('@/lib/providers/ChatProvider', () => ({ useChat: () => ({ handleNewSession: vi.fn() }) }));
vi.mock('@/lib/providers/ModelProvider', () => ({ useModels: () => ({ availableModels: [] }) }));
vi.mock('@/hooks/useConfirmToast', () => ({ useConfirmToast: () => ({ confirmToast: vi.fn() }) }));
vi.mock('@/hooks/useProjects', () => ({
  useProjects: () => ({
    projects: [],
    isLoading: false,
    fetchProjects: vi.fn(),
    saveProject,
    deleteProject: vi.fn(),
  }),
}));

function fillAndSave() {
  render(<ProjectsView />);
  fireEvent.click(screen.getByText(i18n.t('projects.projects_view.add_project_button')));
  fireEvent.change(screen.getByPlaceholderText(i18n.t('projects.projects_view.field_name_placeholder')), {
    target: { value: 'repo' },
  });
  fireEvent.change(screen.getByPlaceholderText(i18n.t('projects.projects_view.field_root_path_placeholder')), {
    target: { value: '/app/workspace/touka-projects/repo/src' },
  });
  fireEvent.click(screen.getByText(i18n.t('projects.projects_view.save_button')));
}

describe('ProjectsView 存檔失敗的提示', () => {
  beforeEach(() => {
    saveProject.mockReset();
    vi.mocked(toast.error).mockReset();
  });

  it('專用空間拒絕 → 提示原因，表單保持開啟', async () => {
    saveProject.mockResolvedValueOnce({ ok: false, code: 'VALIDATION_ERROR', reason: 'project_not_top_level' });
    fillAndSave();
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(i18n.t('projects.projects_view.space_reasons.project_not_top_level')),
    );
    expect(screen.getByText(i18n.t('projects.projects_view.create_form_title'))).toBeInTheDocument();
  });

  it('沒有原因 → 一般的「存不了」', async () => {
    saveProject.mockResolvedValueOnce({ ok: false });
    fillAndSave();
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(i18n.t('projects.projects_view.save_failed')));
  });

  it('成功 → 不提示，表單收起', async () => {
    saveProject.mockResolvedValueOnce({ ok: true });
    fillAndSave();
    await waitFor(() =>
      expect(screen.queryByText(i18n.t('projects.projects_view.create_form_title'))).not.toBeInTheDocument(),
    );
    expect(toast.error).not.toHaveBeenCalled();
  });
});
