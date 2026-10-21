"use client";

import { Sparkles } from 'lucide-react';
import { useTranslation } from "react-i18next";

/**
 * HAX G1（講清楚系統能做什麼）：對話空狀態的首屏。
 *
 * 範例提示為單一事實來源常數，可自由增減/改寫——它們是新使用者的第一印象，
 * 刻意挑選「整理 / 視覺化 / 規劃」三個跨領域、非工程背景也好上手的入口。
 */
export const WELCOME_PROMPT_KEYS = [
  'chat.chat_welcome.prompts.organize_requirements_todo',
  'chat.chat_welcome.prompts.explain_concept_flowchart',
  'chat.chat_welcome.prompts.plan_learning_path',
] as const;

interface ChatWelcomeProps {
  isIDE: boolean;
  /** 點擊範例提示時，將該提示文字填入輸入框。 */
  onPickPrompt: (prompt: string) => void;
}

export function ChatWelcome({ isIDE, onPickPrompt }: ChatWelcomeProps) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col items-center justify-center gap-5 py-10 text-center select-none">
      <Sparkles size={isIDE ? 40 : 56} className="opacity-30" aria-hidden="true" />
      <div className="max-w-md space-y-1.5">
        <p className="text-base font-bold text-foreground">{t('chat.chat_welcome.start_conversation_title')}</p>
        <p className="text-sm leading-relaxed text-muted-foreground">
          {t('chat.chat_welcome.capabilities_description')}
        </p>
      </div>
      <ul className="flex w-full max-w-md flex-col gap-2">
        {WELCOME_PROMPT_KEYS.map((promptKey) => {
          const prompt = t(promptKey);
          return (
            <li key={promptKey}>
              <button
                type="button"
                onClick={() => onPickPrompt(prompt)}
                className="w-full rounded-xl border border-input bg-muted/30 px-4 py-2.5 text-left text-sm text-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              >
                {prompt}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
