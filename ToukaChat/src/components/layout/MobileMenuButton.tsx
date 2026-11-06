"use client";

import type { ComponentProps } from 'react';
import { Menu } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { SessionAttentionDot, useStatusLabel } from '@/components/sidebar/left/SessionStatusMark';
import { SetupNoticeDot } from '@/components/setup/SetupNoticeDot';
import { useHasSetupNotice } from '@/components/setup/SetupNoticesProvider';
import { useSessionActivity } from '@/lib/providers/SessionActivityProvider';

/** 手機頂列開抽屜的按鈕。抽屜關著也要看得到有對話在工作或等您處理；props 由 SheetTrigger 帶進來。 */
export function MobileMenuButton(props: ComponentProps<'button'>) {
  const { t } = useTranslation();
  const hasSetupNotice = useHasSetupNotice();
  const { attention } = useSessionActivity();
  const attentionLabel = useStatusLabel(attention);
  return (
    <button
      {...props}
      className="relative p-2 -ml-2 text-muted-foreground hover:text-primary transition-colors shrink-0"
      aria-label={[hasSetupNotice ? t('sidebar.setup_notice.open_menu_aria') : t('app.home_page.nav_menu_title'), attentionLabel].filter(Boolean).join(t('sidebar.session_tree.label_separator'))}
    >
      <Menu size={24} />
      <SetupNoticeDot />
      <SessionAttentionDot status={attention} />
    </button>
  );
}
