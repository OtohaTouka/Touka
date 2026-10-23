'use client';

import { useHasSetupNotice } from './SetupNoticesProvider';

/**
 * 側欄開關按鈕上的紅點：左側邊欄裡有首次設定警告時顯示，側欄收起來也看得到。
 * 純視覺標記；按鈕名稱由呼叫端依 useHasSetupNotice() 補上說明。按鈕需要 `relative`。
 */
export function SetupNoticeDot() {
  const show = useHasSetupNotice();
  if (!show) return null;
  return (
    <span
      aria-hidden="true"
      data-testid="setup-notice-dot"
      className="absolute top-1 right-1 h-2 w-2 rounded-full bg-destructive"
    />
  );
}
