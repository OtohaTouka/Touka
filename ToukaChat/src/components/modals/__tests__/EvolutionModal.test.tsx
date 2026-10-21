import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { SkillInstallRequest } from '@/lib/types';

const h = vi.hoisted(() => ({
  confirmInstall: vi.fn().mockResolvedValue(true),
  toastSuccess: vi.fn(),
}));
const onClose = vi.fn();

vi.mock('@/hooks/useEvolution', () => ({
  useEvolution: () => ({
    isInstalling: false,
    confirmInstall: h.confirmInstall,
  }),
}));
vi.mock('sonner', () => ({ toast: { success: h.toastSuccess, error: vi.fn() } }));
vi.mock('@/lib/error_handler', () => ({ errorMsg: () => 'err' }));

import EvolutionModal from '../EvolutionModal';

const installRequest = {
  id: 'req1',
  name: 'TestTool',
  is_creation: false,
  command: 'python',
  args: ['-m', 'x'],
  env_requirements: [],
  source_code: null,
} as unknown as SkillInstallRequest;

describe('EvolutionModal A1：安裝成功 toast', () => {
  it('confirmInstall 成功 → 跳成功 toast 並關閉視窗', async () => {
    render(<EvolutionModal installRequest={installRequest} onClose={onClose} />);
    fireEvent.click(screen.getByRole('button', { name: /確認授權並安裝/ }));
    await waitFor(() => expect(h.confirmInstall).toHaveBeenCalled());
    expect(h.toastSuccess).toHaveBeenCalledWith('安裝成功！能力已掛載並立即生效。');
    expect(onClose).toHaveBeenCalled();
  });
});

describe('EvolutionModal：安裝指令審核', () => {
  it('顯示完整啟動指令，且不再提供安全掃描鈕', () => {
    render(<EvolutionModal installRequest={installRequest} onClose={onClose} />);
    expect(screen.getByText(/python -m x/)).toBeDefined();
    expect(screen.queryByRole('button', { name: /安全掃描/ })).toBeNull();
  });
});
