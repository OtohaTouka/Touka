'use client';

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { SETUP_WIZARD_STEPS } from './setupWizardConfig';
import { SetupStatus } from '@/hooks/useSetupStatus';
import { Step1SystemHealth } from './steps/Step1SystemHealth';
import { Step2AdminPassword } from './steps/Step2AdminPassword';
import { Step3ApiKeys } from './steps/Step3ApiKeys';
import { Step4Secrets } from './steps/Step4Secrets';
import { Step5DataPath } from './steps/Step5DataPath';
import { Step6CreateUser } from './steps/Step6CreateUser';
import { Step7Complete } from './steps/Step7Complete';

interface Props {
  status: SetupStatus | null;
  onClose: () => void;
}

export function SetupWizard({ status, onClose }: Props) {
  const { t } = useTranslation();
  const [stepIndex, setStepIndex] = useState(0);

  const step = SETUP_WIZARD_STEPS[stepIndex];
  const totalSteps = SETUP_WIZARD_STEPS.length;

  function next() {
    if (stepIndex < totalSteps - 1) setStepIndex((i) => i + 1);
  }

  function back() {
    if (stepIndex > 0) setStepIndex((i) => i - 1);
  }

  // 各套用步驟（Step1/Step3/Step5）皆自帶 fetch＋錯誤顯示，僅在成功時呼叫 onNext 前進；
  // 父層不再代理 apply，避免默默吞掉失敗（root-cause-C：修正前 apply* 一律 next()）。
  function renderStep() {
    switch (step.id) {
      case "system_health":
        return <Step1SystemHealth status={status} onNext={next} />;
      case "admin_password":
        return <Step2AdminPassword onNext={next} />;
      case "api_keys":
        return <Step3ApiKeys status={status} onNext={next} />;
      case "secrets":
        return <Step4Secrets status={status} onNext={next} />;
      case "data_path":
        return <Step5DataPath status={status} onNext={next} />;
      case "create_user":
        return <Step6CreateUser onNext={next} />;
      case "complete":
        return <Step7Complete status={status} onClose={onClose} />;
      default:
        return null;
    }
  }

  return (
    // 可捲動遮罩＋ m-auto 卡片：內容矮於視窗＝置中；高於視窗（如金鑰步驟 7 個供應商輸入）
    // ＝可完整捲動到底部按鈕。嚴禁改回 items-center justify-center 置中——flex 置中在
    // 內容溢出時會同時裁掉上下兩端且無法捲動，導致「下一步」按鈕永遠點不到（實際事故）。
    <div className="fixed inset-0 z-50 flex overflow-y-auto p-4 bg-black/50 backdrop-blur-sm">
      <div className="bg-card rounded-xl shadow-xl w-full max-w-lg m-auto p-4 sm:p-6">
        {/* Header */}
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className="font-bold text-lg">{t('setup.wizard.title')}</h2>
            <p className="text-sm text-muted-foreground">
              {t('setup.wizard.step_progress', {
                current: stepIndex + 1,
                total: totalSteps,
                title: t(`setup.wizard.steps.${step.id}`),
              })}
              {!step.isRequired && <span className="ml-2 text-xs text-muted-foreground">{t('setup.wizard.optional')}</span>}
            </p>
          </div>
          {step.id !== "complete" && (
            <button
              onClick={onClose}
              className="text-muted-foreground hover:text-foreground text-xl leading-none"
              aria-label={t('setup.wizard.close_aria')}
            >
              ×
            </button>
          )}
        </div>

        {/* Progress bar */}
        <div
          className="w-full bg-muted rounded-full h-1.5 mb-6"
          role="progressbar"
          aria-valuenow={stepIndex + 1}
          aria-valuemin={1}
          aria-valuemax={totalSteps}
        >
          <div
            className="bg-primary h-1.5 rounded-full transition-all"
            style={{ width: `${((stepIndex + 1) / totalSteps) * 100}%` }}
          />
        </div>

        {/* Step content */}
        {renderStep()}

        {stepIndex > 0 && (
          <div className="mt-4 pt-3 border-t border-border">
            <button
              onClick={back}
              className="text-sm text-muted-foreground hover:text-foreground"
            >
              ← {t('setup.wizard.back')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
