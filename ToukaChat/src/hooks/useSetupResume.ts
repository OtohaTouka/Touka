import { useCallback, useEffect, useState } from 'react';
import { getLocalStorageItem, removeLocalStorageItem, setLocalStorageItem } from './useLocalStorage';
import { getSystemEpoch } from '@/lib/time_utils';
import { SETUP_WIZARD_STEPS, type WizardStepId } from '@/components/setup/setupWizardConfig';

/**
 * 精靈改完管理員密碼後的接續點（ADR-0053 補記 2026-10-23）。
 * 改密碼會撤銷這個人所有舊卡（ADR-0144），所以精靈請使用者重新登入；登入後由這裡記下的步驟接續。
 * 只記使用者編號、步驟與時間，不存密碼。
 */
const SETUP_RESUME_KEY = 'touka_setup_resume';
/** 改完密碼通常馬上重新登入；超過 1 小時就不再自動打開精靈。 */
export const SETUP_RESUME_TTL_MS = 60 * 60 * 1000;

interface SetupResume {
  userId: string;
  stepId: WizardStepId;
  savedAt: number;
}

const STEP_IDS = new Set<string>(SETUP_WIZARD_STEPS.map((s) => s.id));

function isValid(value: unknown, now: number): value is SetupResume {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const { userId, stepId, savedAt } = value as Record<string, unknown>;
  return typeof userId === 'string' && userId !== '' &&
    typeof stepId === 'string' && STEP_IDS.has(stepId) &&
    typeof savedAt === 'number' && Number.isFinite(savedAt) &&
    savedAt <= now && now - savedAt <= SETUP_RESUME_TTL_MS;
}

/** 讀出有效的接續點；壞掉、過期或時間在未來的整筆刪掉。沒有紀錄時不寫也不刪。 */
function readSetupResume(): SetupResume | null {
  const value = getLocalStorageItem<unknown>(SETUP_RESUME_KEY, null);
  if (value === null) return null;
  if (isValid(value, getSystemEpoch())) return value;
  removeLocalStorageItem(SETUP_RESUME_KEY);
  return null;
}

export function saveSetupResume(userId: string, stepId: WizardStepId): void {
  setLocalStorageItem<SetupResume>(SETUP_RESUME_KEY, { userId, stepId, savedAt: getSystemEpoch() });
}

/** 登入頁用：有沒有有效的接續點（只看不取）。 */
export function peekSetupResume(): boolean {
  return readSetupResume() !== null;
}

/** 取出這位使用者的接續步驟並刪掉紀錄；別人的紀錄不動。 */
export function takeSetupResume(userId: string): WizardStepId | null {
  const resume = readSetupResume();
  if (resume === null || resume.userId !== userId) return null;
  removeLocalStorageItem(SETUP_RESUME_KEY);
  return resume.stepId;
}

/** 登入後（userId 從 null 變成管理員的編號時）取一次接續步驟。 */
export function useSetupResumeAfterLogin(userId: string | null) {
  const [resumeStepId, setResumeStepId] = useState<WizardStepId | null>(null);
  useEffect(() => {
    if (!userId) return;
    const stepId = takeSetupResume(userId);
    // StrictMode 會跑兩次：第二次讀不到時保留第一次拿到的步驟，不設回 null。
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 登入後讀一次瀏覽器裡的接續點（外部儲存）
    if (stepId) setResumeStepId(stepId);
  }, [userId]);
  const clearResume = useCallback(() => setResumeStepId(null), []);
  return { resumeStepId, clearResume };
}
