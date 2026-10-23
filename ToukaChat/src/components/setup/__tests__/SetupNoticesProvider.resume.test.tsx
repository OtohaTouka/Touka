/**
 * 精靈改完密碼、重新登入之後的接續（ADR-0053 補記 2026-10-23）：
 * 同一位管理員、接續點還有效、而且還有沒設定完的項目，才自動打開精靈到記下的步驟。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SetupNoticesProvider, useSetupNotices } from '../SetupNoticesProvider';
import { useAuth } from '@/contexts/AuthContext';
import { useSetupStatus, type SetupStatus } from '@/hooks/useSetupStatus';
import { saveSetupResume } from '@/hooks/useSetupResume';

vi.mock('@/contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('@/hooks/useSetupStatus', () => ({ useSetupStatus: vi.fn() }));

const RESUME_KEY = 'touka_setup_resume';
const STEP3 = '步驟 3 / 7：API 金鑰';

const unfinished: SetupStatus = {
  dispatch_token_is_default: true,
  auth_secret_persisted: true,
  api_keys_configured: [],
  is_admin_temp_password: false,
  data_path_is_default: true,
  workspace_root_is_default: true,
  embedding_model: 'all-MiniLM-L6-v2',
};
const allSet: SetupStatus = {
  ...unfinished,
  dispatch_token_is_default: false,
  api_keys_configured: ['anthropic'],
};

const refetch = vi.fn();

function signIn(user: { id: string; role: string } | null, status: SetupStatus | null) {
  vi.mocked(useAuth).mockReturnValue({ user, logout: vi.fn() } as unknown as ReturnType<typeof useAuth>);
  vi.mocked(useSetupStatus).mockReturnValue({ status, isLoading: false, refetch });
}

const app = () => <SetupNoticesProvider><p>app</p></SetupNoticesProvider>;

describe('重新登入後接續精靈', () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.clearAllMocks();
  });

  it('同一位管理員、還有沒設定完的項目：精靈開在記下的步驟，接續點用掉', () => {
    saveSetupResume('admin-1', 'api_keys');
    signIn({ id: 'admin-1', role: 'admin' }, unfinished);
    render(app());
    expect(screen.getByText(STEP3)).toBeInTheDocument();
    expect(window.localStorage.getItem(RESUME_KEY)).toBeNull();
  });

  it('都設定好了：不打開精靈，接續點丟掉', () => {
    saveSetupResume('admin-1', 'api_keys');
    signIn({ id: 'admin-1', role: 'admin' }, allSet);
    render(app());
    expect(screen.queryByText(STEP3)).toBeNull();
    expect(window.localStorage.getItem(RESUME_KEY)).toBeNull();
  });

  it('別人的接續點：不打開，也不刪', () => {
    saveSetupResume('admin-1', 'api_keys');
    signIn({ id: 'admin-2', role: 'admin' }, unfinished);
    render(app());
    expect(screen.queryByText(STEP3)).toBeNull();
    expect(window.localStorage.getItem(RESUME_KEY)).not.toBeNull();
  });

  it('不是管理員：不取接續點', () => {
    saveSetupResume('admin-1', 'api_keys');
    signIn({ id: 'admin-1', role: 'normal' }, unfinished);
    render(app());
    expect(screen.queryByText(STEP3)).toBeNull();
    expect(window.localStorage.getItem(RESUME_KEY)).not.toBeNull();
  });

  it('登入頁還沒有使用者時不取；登入後狀態讀到才打開', () => {
    saveSetupResume('admin-1', 'api_keys');
    signIn(null, null);
    const { rerender } = render(app());
    expect(window.localStorage.getItem(RESUME_KEY)).not.toBeNull();

    signIn({ id: 'admin-1', role: 'admin' }, null);
    rerender(app());
    expect(screen.queryByText(STEP3)).toBeNull();

    signIn({ id: 'admin-1', role: 'admin' }, unfinished);
    rerender(app());
    expect(screen.getByText(STEP3)).toBeInTheDocument();
  });

  it('關掉接續打開的精靈後不會再自己打開', () => {
    saveSetupResume('admin-1', 'api_keys');
    signIn({ id: 'admin-1', role: 'admin' }, unfinished);
    const { rerender } = render(app());
    fireEvent.click(screen.getByLabelText('關閉設定精靈'));
    rerender(app());
    expect(screen.queryByText(STEP3)).toBeNull();
  });
});

function OpenCreateUserStep() {
  const { openWizard } = useSetupNotices();
  return <button type="button" onClick={() => openWizard('create_user')}>open</button>;
}

describe('第 7 步顯示最新狀態', () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.clearAllMocks();
  });

  it('前進到完成時重讀狀態，第 7 步顯示重讀後的結果', () => {
    signIn({ id: 'admin-1', role: 'admin' }, unfinished);
    const tree = () => <SetupNoticesProvider><OpenCreateUserStep /></SetupNoticesProvider>;
    const { rerender } = render(tree());
    fireEvent.click(screen.getByRole('button', { name: 'open' }));
    fireEvent.click(screen.getByRole('button', { name: '略過 — 結束設定' }));
    expect(refetch).toHaveBeenCalledOnce();
    expect(screen.getByText('尚未設定任何 API 金鑰')).toBeInTheDocument();

    signIn({ id: 'admin-1', role: 'admin' }, allSet);
    rerender(tree());
    expect(screen.getByText(/步驟 7/)).toBeInTheDocument();
    expect(screen.queryByText('尚未設定任何 API 金鑰')).toBeNull();
  });
});
