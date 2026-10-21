import { useCallback } from 'react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';

export function useConfirmToast() {
  const { t } = useTranslation();
  const confirmToast = useCallback(
    (
      message: string,
      onConfirm: () => unknown,
      confirmLabel = t('hooks.use_confirm_toast.confirm_label'),
      onCancel?: () => unknown,
    ) => {
      toast.warning(message, {
        action: {
          label: confirmLabel,
          onClick: () => { void onConfirm(); },
        },
        cancel: {
          label: t('hooks.use_confirm_toast.cancel_label'),
          onClick: () => { void onCancel?.(); },
        },
        duration: Infinity,
      });
    },
    [t],
  );

  return { confirmToast };
}
