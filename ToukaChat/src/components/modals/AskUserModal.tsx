"use client";

import { useState } from 'react';
import { useTranslation } from "react-i18next";
import { cn } from '@/lib/utils';
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog';
import { AskUserRequest } from '@/lib/types';

interface AskUserModalProps {
  request: AskUserRequest;
  onSubmit: (answers: string[][]) => void;
  onCancel: () => void;
}

/**
 * HAX G10（ADR-0085）：AI 詢問使用者的問卷彈窗。
 * 一頁列出全部問題：單選＝radio 語意、複選＝checkbox 語意，每題可附「其他」自由填。
 * 送出回傳依問題順序的答案陣列（string[][]）。
 */
export function AskUserModal({ request, onSubmit, onCancel }: AskUserModalProps) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<Record<number, string[]>>({});
  const [other, setOther] = useState<Record<number, string>>({});

  const toggle = (qIdx: number, option: string, multi: boolean) => {
    setSelected((prev) => {
      const cur = prev[qIdx] || [];
      if (!multi) return { ...prev, [qIdx]: [option] };
      return {
        ...prev,
        [qIdx]: cur.includes(option) ? cur.filter((o) => o !== option) : [...cur, option],
      };
    });
  };

  const handleSubmit = () => {
    const answers = request.questions.map((q, i) => {
      const opts = selected[i] || [];
      const otherText = (other[i] || '').trim();
      return otherText ? [...opts, `其他：${otherText}`] : opts;
    });
    onSubmit(answers);
  };

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onCancel(); }}>
      <DialogContent
        overlayClassName="bg-black/60 backdrop-blur-sm"
        className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-lg p-6 flex flex-col gap-4 max-h-[80vh]"
      >
        <DialogTitle className="text-lg font-semibold text-foreground">{t('modals.ask_user_modal.title')}</DialogTitle>
        <DialogDescription className="text-sm text-muted-foreground">
          {t('modals.ask_user_modal.description')}
        </DialogDescription>

        <div className="flex flex-col gap-5 overflow-y-auto custom-scrollbar pr-1">
          {request.questions.map((q, qIdx) => (
            <fieldset key={qIdx} className="flex flex-col gap-2">
              <legend className="text-sm font-bold text-foreground">{q.question}</legend>
              <div className="flex flex-wrap gap-2">
                {q.options.map((option) => {
                  const isOn = (selected[qIdx] || []).includes(option);
                  return (
                    <button
                      key={option}
                      type="button"
                      aria-pressed={isOn}
                      onClick={() => toggle(qIdx, option, Boolean(q.multiSelect))}
                      className={cn(
                        "rounded-lg border px-3 py-1.5 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
                        isOn
                          ? "border-primary-button bg-primary-button text-primary-foreground font-bold"
                          : "border-input bg-muted/30 text-foreground hover:bg-accent"
                      )}
                    >
                      {option}
                    </button>
                  );
                })}
              </div>
              {q.allowOther && (
                <input
                  type="text"
                  value={other[qIdx] || ''}
                  onChange={(e) => setOther((prev) => ({ ...prev, [qIdx]: e.target.value }))}
                  maxLength={300}
                  aria-label={t('modals.ask_user_modal.other_aria_label', { question: q.question })}
                  placeholder={t('modals.ask_user_modal.other_placeholder')}
                  className="rounded-lg border border-input bg-muted px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                />
              )}
            </fieldset>
          ))}
        </div>

        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={onCancel}
            className="rounded-lg px-4 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted"
          >
            {t('modals.ask_user_modal.cancel')}
          </button>
          <button
            onClick={handleSubmit}
            className="rounded-lg bg-primary-button px-4 py-2 text-sm font-bold text-primary-foreground transition-colors hover:bg-primary-button/90"
          >
            {t('modals.ask_user_modal.submit')}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
