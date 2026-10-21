"use client";

import { useState } from 'react';
import { useTranslation } from "react-i18next";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog';

interface FeedbackReasonDialogProps {
  open: boolean;
  onSubmit: (reason: string) => void;
  onCancel: () => void;
}

/**
 * HAX G15/G6 — 倒讚時的選填原因彈窗（重用 Radix Dialog）。
 * 原因欄同時兼作有害/偏見內容回報管道；明示回饋僅供擁有者檢視、不自動改變 AI。
 */
export function FeedbackReasonDialog({ open, onSubmit, onCancel }: FeedbackReasonDialogProps) {
  const { t } = useTranslation();
  const [reason, setReason] = useState('');

  // 於關閉/送出時清空，下次開啟即為空白（避免 set-state-in-effect）。
  const handleSubmit = () => { onSubmit(reason.trim()); setReason(''); };
  const handleCancel = () => { onCancel(); setReason(''); };

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) handleCancel(); }}>
      <DialogContent
        overlayClassName="bg-black/60 backdrop-blur-sm"
        className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-md p-6 flex flex-col gap-4"
      >
        <DialogTitle className="text-lg font-semibold text-foreground">{t('chat.feedback_reason_dialog.title')}</DialogTitle>
        <DialogDescription className="text-sm text-muted-foreground">
          {t('chat.feedback_reason_dialog.description')}
        </DialogDescription>
        <textarea
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          maxLength={500}
          rows={4}
          aria-label={t('chat.feedback_reason_dialog.textarea_aria_label')}
          placeholder={t('chat.feedback_reason_dialog.textarea_placeholder')}
          className="w-full resize-none rounded-lg border border-input bg-muted px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
        />
        <div className="flex justify-end gap-2">
          <button
            onClick={handleCancel}
            className="rounded-lg px-4 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted"
          >
            {t('chat.feedback_reason_dialog.cancel')}
          </button>
          <button
            onClick={handleSubmit}
            className="rounded-lg bg-primary-button px-4 py-2 text-sm font-bold text-primary-foreground transition-colors hover:bg-primary-button/90"
          >
            {t('chat.feedback_reason_dialog.submit')}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
