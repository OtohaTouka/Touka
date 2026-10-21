/**
 * ADR-0149（ADR-0148 對抗式審查揪出的既有缺口）— 演化部署審批
 * （skill_creation_approval_request）接進 EvolutionModal。
 *
 * 部署審批事件僅帶 source_code＋is_creation，**無** args／env_requirements（launch
 * command 於核准後才組出）。舊 EvolutionModal 對 `installRequest.args.join(" ")` 無
 * 防護 → 渲染部署事件即 crash。本檔釘死：
 *  - 呈現 source_code 供審、不因缺 args 崩潰（Step 2 指令預覽僅安裝時渲染）；
 *  - 確認 → confirmInstall(exec_id, {}) → 對應 /terminal/approve 解算端點。
 */
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

const SOURCE = 'from server import mcp\n@mcp.tool()\ndef ping():\n    return "pong"';

// 演化部署事件形狀（request_mcp_creation → request_approval extra_data）：
// 有 command（"Create Capability: …"）但**無** args；有 source_code＋is_creation。
const creationRequest = {
  id: 'create_myskill_123',
  skill_id: 'myskill',
  name: 'MySkill',
  description: 'A freshly evolved capability',
  command: 'Create Capability: MySkill',
  is_creation: true,
  source_code: SOURCE,
} as unknown as SkillInstallRequest;

describe('EvolutionModal — 演化部署（is_creation／source_code／無 args）', () => {
  it('渲染 source_code 供審，且不因缺少 args 崩潰', () => {
    render(<EvolutionModal installRequest={creationRequest} onClose={onClose} />);
    expect(screen.getByText(/def ping/)).toBeDefined();
  });

  it('確認 → confirmInstall(exec_id, {}) 解算（對應 /terminal/approve 端點）', async () => {
    render(<EvolutionModal installRequest={creationRequest} onClose={onClose} />);
    fireEvent.click(screen.getByRole('button', { name: /確認授權並安裝/ }));
    await waitFor(() =>
      expect(h.confirmInstall).toHaveBeenCalledWith('create_myskill_123', {}),
    );
    expect(onClose).toHaveBeenCalled();
  });
});
