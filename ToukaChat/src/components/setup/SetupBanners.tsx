'use client';

import { useState } from 'react';
import { TempPasswordBanner } from './TempPasswordBanner';
import { SetupPromptBanner } from './SetupPromptBanner';
import { SetupWizard } from './SetupWizard';
import { useSetupStatus } from '@/hooks/useSetupStatus';

export function SetupBanners() {
  const [wizardOpen, setWizardOpen] = useState(false);
  const { status } = useSetupStatus();

  return (
    <>
      <TempPasswordBanner onChangePassword={() => setWizardOpen(true)} />
      <SetupPromptBanner onOpenWizard={() => setWizardOpen(true)} />
      {wizardOpen && (
        <SetupWizard status={status} onClose={() => setWizardOpen(false)} />
      )}
    </>
  );
}
