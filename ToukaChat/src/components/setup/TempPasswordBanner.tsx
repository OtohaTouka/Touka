'use client';

import { useTranslation } from 'react-i18next';
import { useSetupStatus } from '@/hooks/useSetupStatus';
import { useAuth } from '@/contexts/AuthContext';

interface TempPasswordBannerProps {
  onChangePassword?: () => void;
}

export function TempPasswordBanner({ onChangePassword }: TempPasswordBannerProps) {
  const { t } = useTranslation();
  const { user } = useAuth();
  const { status } = useSetupStatus();

  if (user?.role !== 'admin' || !status?.is_admin_temp_password) {
    return null;
  }

  return (
    <div
      role="alert"
      className="w-full bg-destructive text-destructive-foreground text-sm px-4 py-2 flex items-center justify-between z-50"
    >
      <span>
        {t('setup.banner.temp_password')}
      </span>
      {onChangePassword && (
        <button
          onClick={onChangePassword}
          className="ml-4 underline font-semibold whitespace-nowrap hover:opacity-80"
        >
          {t('setup.banner.change_password')}
        </button>
      )}
    </div>
  );
}
