'use client';

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CONNECTIONS_STORAGE_KEY, scrubStoredConnectionKeys } from '@/lib/connections';

// 舊版把第三方金鑰真值存在瀏覽器的連線清單裡；載入時換成遮罩（比照 ADR-0224）。
export function StoredConnectionKeyCleanup() {
  const { t } = useTranslation();
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const scrub = () => {
      setFailed(!scrubStoredConnectionKeys());
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === CONNECTIONS_STORAGE_KEY) scrub();
    };
    scrub();
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  return failed ? (
    <div role="alert" className="bg-destructive/10 p-3 text-sm text-destructive">
      {t('settings.connections_tab.storage_cleanup_failed')}
    </div>
  ) : null;
}
