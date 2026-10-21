"use client";

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Brain, ChevronDown, ChevronRight, Loader2 } from 'lucide-react';

interface ThinkingFoldProps {
  /** 累積的思考內容（SSE delta.reasoning_content，ADR-0152 思考分頻）。 */
  thinking: string;
  /** 串流中且正文尚未開始 → 標題顯示「思考中…」＋轉圈。 */
  isThinking?: boolean;
}

/**
 * 思考摺疊區塊（ADR-0152 T2）：預設收合、點開展開；不混入正文的 Markdown 排版
 * （思考是過程輔助資訊，以低視覺權重的等寬純文字呈現）。
 */
export const ThinkingFold = ({ thinking, isThinking = false }: ThinkingFoldProps) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  return (
    <div className="border border-border/50 rounded-lg bg-muted/30 max-w-full">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="flex items-center gap-2 w-full px-3 py-1.5 text-xs font-bold text-muted-foreground hover:text-foreground transition-colors"
      >
        {isThinking ? (
          <Loader2 className="w-3.5 h-3.5 animate-spin shrink-0" />
        ) : (
          <Brain className="w-3.5 h-3.5 shrink-0" />
        )}
        <span>{isThinking ? t('chat.thinking.in_progress') : t('chat.thinking.title')}</span>
        {open ? (
          <ChevronDown className="w-3.5 h-3.5 ml-auto shrink-0" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5 ml-auto shrink-0" />
        )}
      </button>
      {open && (
        <div className="px-3 pb-2 pt-2 text-xs text-muted-foreground whitespace-pre-wrap break-words border-t border-border/50">
          {thinking}
        </div>
      )}
    </div>
  );
};
