'use client';

import { useTranslation } from 'react-i18next';
import { SetupStatus } from '@/hooks/useSetupStatus';

interface Props {
  status: SetupStatus | null;
  onClose: () => void;
}

export function Step7Complete({ status, onClose }: Props) {
  const { t } = useTranslation();
  const issues: string[] = [];
  if (status?.dispatch_token_is_default) issues.push(t('setup.step7.issues.dispatch_default'));
  if (!status?.auth_secret_persisted) issues.push(t('setup.step7.issues.auth_not_persisted'));
  if (!status?.api_keys_configured.length) issues.push(t('setup.step7.issues.no_api_keys'));
  if (status?.is_admin_temp_password) issues.push(t('setup.step7.issues.admin_temp_password'));

  return (
    <div className="space-y-4">
      {issues.length === 0 ? (
        <div className="text-center py-4">
          <div className="text-4xl mb-2">✅</div>
          <h3 className="font-semibold text-lg">{t('setup.step7.complete_title')}</h3>
          <p className="text-sm text-muted-foreground mt-1">{t('setup.step7.complete_subtitle')}</p>
        </div>
      ) : (
        <div>
          <div className="text-center py-2">
            <div className="text-4xl mb-2">⚠️</div>
            <h3 className="font-semibold text-lg">{t('setup.step7.partial_title')}</h3>
          </div>
          <p className="text-sm text-accent-foreground font-medium">{t('setup.step7.remaining')}</p>
          <ul className="text-sm list-disc ml-4 mt-1 text-accent-foreground">
            {issues.map((i) => <li key={i}>{i}</li>)}
          </ul>
          <p className="text-sm text-muted-foreground mt-2">
            {t('setup.step7.complete_later')}
          </p>
        </div>
      )}

      {status && (
        <div className="text-xs text-muted-foreground border rounded p-3 space-y-1">
          <p className="font-medium text-muted-foreground">{t('setup.step7.summary')}</p>
          <p>{t('setup.step7.embedding_model', { model: status.embedding_model })}</p>
          <p>{t('setup.step7.api_keys', { keys: status.api_keys_configured.join(", ") || t('setup.step7.none') })}</p>
        </div>
      )}

      <button
        onClick={onClose}
        className="w-full px-4 py-2 bg-primary-button text-primary-foreground rounded hover:opacity-90"
      >
        {t('setup.step7.start')}
      </button>
    </div>
  );
}
