/**
 * 專案卡片的下載按鈕（ADR-0332 PR B）：一直看得到；先跳提醒（zip 裡是代理人寫的程式、VS Code
 * 選不信任），確認後才下載；被拒絕時說出原因。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { toast } from 'sonner';
import i18n from '@/lib/i18n';
import { ProjectsView } from '../ProjectsView';

const downloadProject = vi.fn();
const confirmToast = vi.fn();
const PROJECT = {
  id: 'proj-1',
  name: 'repo',
  root_path: '/app/workspace/touka-projects/repo',
  description: '',
  updatedAt: 0,
};

vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock('@/lib/providers/ChatProvider', () => ({ useChat: () => ({ handleNewSession: vi.fn() }) }));
vi.mock('@/lib/providers/ModelProvider', () => ({ useModels: () => ({ availableModels: [] }) }));
vi.mock('@/hooks/useConfirmToast', () => ({ useConfirmToast: () => ({ confirmToast }) }));
vi.mock('@/hooks/useProjects', () => ({
  useProjects: () => ({
    projects: [PROJECT],
    isLoading: false,
    fetchProjects: vi.fn(),
    saveProject: vi.fn(),
    deleteProject: vi.fn(),
    downloadProject,
  }),
}));

function clickDownload() {
  render(<ProjectsView />);
  const button = screen.getByRole('button', {
    name: i18n.t('projects.projects_view.download_aria', { name: PROJECT.name }),
  });
  expect(button.className).not.toContain('opacity-0'); // 不是滑過才出現
  fireEvent.click(button);
}

async function confirm() {
  const [message, onConfirm, label] = confirmToast.mock.calls[0];
  expect(message).toBe(i18n.t('projects.projects_view.download_confirm_message'));
  expect(label).toBe(i18n.t('projects.projects_view.download_confirm_button'));
  await onConfirm();
}

describe('ProjectsView 的下載按鈕', () => {
  beforeEach(() => {
    downloadProject.mockReset();
    confirmToast.mockReset();
    vi.mocked(toast.error).mockReset();
  });

  it('提醒的話兩語都有，且說到 VS Code 的不信任', () => {
    for (const lng of ['zh-TW', 'en']) {
      expect(i18n.exists('projects.projects_view.download_confirm_message', { lng })).toBe(true);
    }
    expect(i18n.t('projects.projects_view.download_confirm_message', { lng: 'en' })).toMatch(/VS Code/);
    expect(i18n.t('projects.projects_view.download_confirm_message', { lng: 'zh-TW' })).toMatch(/VS Code/);
  });

  it('按下去先跳提醒，還沒下載', () => {
    clickDownload();
    expect(confirmToast).toHaveBeenCalledTimes(1);
    expect(downloadProject).not.toHaveBeenCalled();
  });

  it('確認後下載這個專案，成功不提示錯誤', async () => {
    downloadProject.mockResolvedValueOnce({ ok: true });
    clickDownload();
    await confirm();
    expect(downloadProject).toHaveBeenCalledWith(PROJECT);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('被拒絕 → 說出原因', async () => {
    downloadProject.mockResolvedValueOnce({ ok: false, code: 'VALIDATION_ERROR', reason: 'no_git' });
    clickDownload();
    await confirm();
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(i18n.t('projects.projects_view.download_reasons.no_git')),
    );
  });
});
