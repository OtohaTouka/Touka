/**
 * ADR-0149（ADR-0148 對抗式審查揪出的既有缺口）— 演化部署審批
 * （skill_creation_approval_request）接進 EvolutionModal。
 *
 * 部署審批事件僅帶 source_code＋is_creation，**無** args／env_requirements（launch
 * command 於核准後才組出）。舊 EvolutionModal 對 `installRequest.args.join(" ")` 無
 * 防護 → 渲染部署事件即 crash。本檔釘死：
 *  - 呈現 source_code 供審、不因缺 args 崩潰（Step 2 指令預覽僅安裝時渲染）；
 *  - 確認 → confirmInstall(exec_id, {}) → 對應 /terminal/approve 解算端點；
 *  - 確認失敗時說出原因（不是管理員、申請過期、其他錯誤碼），視窗不關。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { SkillInstallRequest } from '@/lib/types';
import { errorMsg, ToukaApiError } from '@/lib/error_handler';
import i18n from '@/lib/i18n';

const h = vi.hoisted(() => ({
  confirmInstall: vi.fn().mockResolvedValue(true),
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));
const onClose = vi.fn();

vi.mock('@/hooks/useEvolution', () => ({
  useEvolution: () => ({
    isInstalling: false,
    confirmInstall: h.confirmInstall,
  }),
}));
vi.mock('sonner', () => ({ toast: { success: h.toastSuccess, error: h.toastError } }));

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

beforeEach(() => vi.clearAllMocks());

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

describe('EvolutionModal — 確認失敗時說出原因', () => {
  async function clickConfirmAndGetToast(error: unknown): Promise<unknown> {
    h.confirmInstall.mockRejectedValueOnce(error);
    render(<EvolutionModal installRequest={creationRequest} onClose={onClose} />);
    fireEvent.click(screen.getByRole('button', { name: /確認授權並安裝/ }));
    await waitFor(() => expect(h.toastError).toHaveBeenCalledTimes(1));
    expect(onClose).not.toHaveBeenCalled();
    expect(h.toastSuccess).not.toHaveBeenCalled();
    return h.toastError.mock.calls[0][0];
  }

  it.each([
    [
      'AUTH_ADMIN_REQUIRED',
      403,
      'modals.evolution_modal.admin_required',
      '只有管理員帳號能核准安裝。如果你是管理員，請重新登入後再請 AI 申請一次。',
    ],
    [
      'TERMINAL_APPROVAL_NOT_FOUND',
      404,
      'modals.evolution_modal.request_expired',
      '這筆申請已過期，請再請 AI 申請一次。',
    ],
  ])('%s → 專用提示', async (code, status, key, text) => {
    expect(i18n.exists(key)).toBe(true);
    const shown = await clickConfirmAndGetToast(new ToukaApiError('x', status, code));
    expect(shown).toBe(text);
  });

  it.each([
    ['AUTH_PERMISSION_DENIED', 403],
    ['NETWORK_CONNECTION_ERROR', 0],
    ['CONFIG_SAVE_FAILED', 500],
  ] as const)('其他認得的錯誤碼 %s → 顯示那個錯誤碼的訊息', async (code, status) => {
    const shown = await clickConfirmAndGetToast(new ToukaApiError('x', status, code));
    expect(shown).toBe(errorMsg(code));
  });

  it('沒有錯誤碼的意外錯誤 → 系統內部錯誤', async () => {
    const shown = await clickConfirmAndGetToast(new Error('boom'));
    expect(shown).toBe(errorMsg('SYSTEM_ERROR'));
  });
});
