'use client';

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '@/contexts/AuthContext';
import { useSetupStatus, type SetupStatus } from '@/hooks/useSetupStatus';

interface SetupPromptBannerProps {
  onOpenWizard?: () => void;
}

function hasSetupIssues(status: SetupStatus | null): boolean {
  if (!status) return false;
  return (
    status.dispatch_token_is_default ||
    !status.auth_secret_persisted ||
    status.api_keys_configured.length === 0 ||
    status.is_admin_temp_password
  );
}

export function SetupPromptBanner({ onOpenWizard }: SetupPromptBannerProps) {
  const { t } = useTranslation();
  const { user } = useAuth();
  const { status } = useSetupStatus();
  const [dismissed, setDismissed] = useState(false);

  if (user?.role !== 'admin' || dismissed || !hasSetupIssues(status)) {
    return null;
  }

  return (
    <div
      role="status"
      className="w-full bg-accent text-accent-foreground text-sm px-4 py-2 flex items-center justify-between z-40"
    >
      <span>
        {t('setup.banner.setup_incomplete')}
      </span>
      <div className="flex items-center gap-3 ml-4">
        {onOpenWizard && (
          <button
            onClick={onOpenWizard}
            className="underline font-semibold whitespace-nowrap hover:text-accent-foreground"
          >
            {t('setup.banner.setup_now')}
          </button>
        )}
        <button
          onClick={() => setDismissed(true)}
          className="text-accent-foreground hover:text-accent-foreground font-medium whitespace-nowrap"
          aria-label={t('setup.banner.dismiss_aria')}
        >
          {t('setup.banner.later')}
        </button>
      </div>
    </div>
  );
}
