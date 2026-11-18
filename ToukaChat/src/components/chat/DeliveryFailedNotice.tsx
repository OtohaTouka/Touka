// src/components/chat/DeliveryFailedNotice.tsx
"use client";

import { useTranslation } from 'react-i18next';
import { knownPlatformName } from '@/lib/chat/turnNotes';

interface DeliveryFailedNoticeProps {
  platform?: string;
}

/** ADR-0322：SNS 回覆送不出去（後端在節點寫 deliveryFailed）的標示；平台只用固定名稱。 */
export function DeliveryFailedNotice({ platform }: DeliveryFailedNoticeProps) {
  const { t } = useTranslation();
  const name = knownPlatformName(platform);
  return (
    <p data-testid="delivery-failed-notice" className="text-sm text-status-warning">
      {name
        ? t('chat.message_item.delivery_failed', { platform: name })
        : t('chat.message_item.delivery_failed_other')}
    </p>
  );
}
