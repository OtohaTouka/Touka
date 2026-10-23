'use client';

import { useState } from 'react';
import { useTranslation } from 'react-i18next';

interface Props {
  onRelogin?: () => void;
}

/**
 * 精靈第 2 步改完密碼後的畫面（ADR-0053 補記 2026-10-23）：後端已撤銷所有舊卡（ADR-0144），
 * 請使用者用新密碼重新登入。不用 toast：登出時 AuthGuard 會卸載整棵樹（含 Toaster），提示會消失。
 */
export function ReloginNotice({ onRelogin }: Props) {
  const { t } = useTranslation();
  const [leaving, setLeaving] = useState(false);

  return (
    <div className="space-y-4">
      <p role="status" className="text-sm">{t('setup.relogin.message')}</p>
      <button
        type="button"
        disabled={leaving}
        onClick={() => {
          setLeaving(true);
          onRelogin?.();
        }}
        className="px-4 py-2 bg-primary-button text-primary-foreground rounded hover:opacity-90 disabled:opacity-50"
      >
        {t('setup.relogin.go_to_login')}
      </button>
    </div>
  );
}
