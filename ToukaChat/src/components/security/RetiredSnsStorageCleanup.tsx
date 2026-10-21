'use client';

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { removeLocalStorageItem } from '@/hooks/useLocalStorage';

// ADR-0224: retired secret storage is removed without ever reading its contents.
const RETIRED_SNS_KEY = 'touka_sns_bots';

export function RetiredSnsStorageCleanup() {
  const { t } = useTranslation();
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const clearRetiredStorage = () => {
      setFailed(!removeLocalStorageItem(RETIRED_SNS_KEY));
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === RETIRED_SNS_KEY) clearRetiredStorage();
    };
    clearRetiredStorage();
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  return failed ? (
    <div role="alert" className="bg-destructive/10 p-3 text-sm text-destructive">
      {t('settings.sns_bots_tab.storage_cleanup_failed')}
    </div>
  ) : null;
}
