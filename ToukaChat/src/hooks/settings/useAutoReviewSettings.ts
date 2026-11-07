import { useCallback } from 'react';
import type { AutonomyConfig } from '@/lib/types';
import { useKeyedModels } from './useKeyedModels';

/** 審查員等待秒數上限；對齊後端 AutoReviewConfig.timeout_s 的 le=120（下限 gt=0）。 */
export const REVIEWER_TIMEOUT_MAX_S = 120;

/**
 * Auto 審查員（ADR-0246）：選項只來自後端「已設定金鑰的模型」清單；選到的模型寫進
 * 安全設定頁共用的 autonomy 設定，經 /v1/autonomy 一起儲存。不自動帶建議值。
 */
export function useAutoReviewSettings(
  autonomyConfig: AutonomyConfig | null,
  setAutonomyConfig: (config: AutonomyConfig | null) => void,
) {
  const { models, loadFailed } = useKeyedModels();

  const setReviewerModel = useCallback((model: string) => {
    if (!autonomyConfig?.auto_review) return;
    setAutonomyConfig({ ...autonomyConfig, auto_review: { ...autonomyConfig.auto_review, model } });
  }, [autonomyConfig, setAutonomyConfig]);

  // r55：只改 timeout_s，其他欄位原樣（逾時＝轉人／拒絕，不是放行條件）
  const setReviewerTimeout = useCallback((seconds: number) => {
    if (!autonomyConfig?.auto_review) return;
    setAutonomyConfig({ ...autonomyConfig, auto_review: { ...autonomyConfig.auto_review, timeout_s: seconds } });
  }, [autonomyConfig, setAutonomyConfig]);

  const timeoutS = autonomyConfig?.auto_review?.timeout_s;
  const timeoutValid = typeof timeoutS === 'number' && Number.isFinite(timeoutS) && timeoutS > 0
    && timeoutS <= REVIEWER_TIMEOUT_MAX_S;
  return {
    models,
    loadFailed,
    selectedModel: autonomyConfig?.auto_review?.model ?? '',
    editable: !!autonomyConfig?.auto_review,
    setReviewerModel,
    timeoutS,
    timeoutMaxS: REVIEWER_TIMEOUT_MAX_S,
    timeoutValid,
    setReviewerTimeout,
  };
}
