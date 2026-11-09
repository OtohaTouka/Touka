// src/components/chat/GenerationFailedNotice.tsx
"use client";

import { useTranslation } from 'react-i18next';
import type { AgentProgress } from '@/lib/tree_history';

/** 後端 retry.py 的失敗類型；其他代碼（含舊對話沒有原因）顯示原本那一句。 */
const REASON_KEYS = new Map<unknown, string>([
  ['authentication_failed', 'chat.message_item.generation_failed_reason.authentication_failed'],
  ['rate_limit', 'chat.message_item.generation_failed_reason.rate_limit'],
  ['billing_error', 'chat.message_item.generation_failed_reason.billing_error'],
  ['overloaded', 'chat.message_item.generation_failed_reason.overloaded'],
  ['server_error', 'chat.message_item.generation_failed_reason.server_error'],
  ['model_not_found', 'chat.message_item.generation_failed_reason.model_not_found'],
  ['invalid_request', 'chat.message_item.generation_failed_reason.invalid_request'],
]);

interface GenerationFailedNoticeProps {
  failure?: AgentProgress;
}

/** 回覆失敗的標示。廠商原文是外部內容，只當純文字顯示，不經 Markdown。 */
export function GenerationFailedNotice({ failure }: GenerationFailedNoticeProps) {
  const { t } = useTranslation();
  const detail = failure?.detail;
  return (
    <div role="alert" className="text-sm text-status-error">
      {t(REASON_KEYS.get(failure?.error_type) ?? 'chat.message_item.generation_failed')}
      {typeof detail === 'string' && detail && (
        <p className="mt-1 break-words whitespace-pre-wrap">
          {t('chat.message_item.generation_failed_vendor_said')}
          {detail}
        </p>
      )}
    </div>
  );
}
