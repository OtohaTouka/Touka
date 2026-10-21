"use client";

import React from 'react';
import { useTranslation, Trans } from 'react-i18next';
import { Brain } from 'lucide-react';
import { MessageNode, AgentProgress } from '@/lib/tree_history';
import { countRounds, contextUsagePct, CONTEXT_ROUND_LIMIT } from '@/lib/context_usage';
import { cn } from '@/lib/utils';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogClose,
} from '@/components/ui/dialog';

interface ContextMeterProps {
  messages: MessageNode[];
}

// 分解類別 → i18n 標籤鍵（ADR-0092 Phase B）。
const CATEGORY_LABEL_KEYS: Record<string, string> = {
  persona: 'chat.context_meter.categories.persona',
  capabilities: 'chat.context_meter.categories.capabilities',
  tools: 'chat.context_meter.categories.tools',
  mission_awareness: 'chat.context_meter.categories.mission_awareness',
  project_context: 'chat.context_meter.categories.project_context',
  ide_context: 'chat.context_meter.categories.ide_context',
  conversation_summary: 'chat.context_meter.categories.conversation_summary',
  conversation: 'chat.context_meter.categories.conversation',
  system_other: 'chat.context_meter.categories.system_other',
};

/** 從最近一則帶 token_usage 進度的訊息取 context 用量（無則 undefined）。 */
function findLatestContextUsage(messages: MessageNode[]): AgentProgress | undefined {
  for (let i = messages.length - 1; i >= 0; i--) {
    const tu = messages[i]?.progress?.token_usage;
    if (tu && typeof tu.usage_pct === 'number') return tu;
  }
  return undefined;
}

function formatTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

/**
 * context 占用指示（ADR-0092 Phase B）：
 * - 有 token 用量（聊過至少一輪）→ 顯示真實 token 百分比 ＋ Claude Code /context 風格分類別分解。
 * - 尚無 token 用量（剛載入、未產生新回合的對話）→ fallback 以對話來回數估算（HAX G12 說明面板）。
 * 空對話不顯示；接近上限（≥85%）轉為警示色。
 */
export function ContextMeter({ messages }: ContextMeterProps) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const usage = findLatestContextUsage(messages);

  // ── 主路徑：真實 token 百分比 ＋ 分類別分解 ──
  if (usage && typeof usage.usage_pct === 'number') {
    const pct = Math.round(usage.usage_pct);
    const bd = usage.context_breakdown;
    const total = bd?.total_tokens ?? 0;
    const ctxWindow = bd?.window ?? 0;
    const entries = Object.entries(bd?.breakdown ?? {}).sort((a, b) => b[1] - a[1]);
    return (
      <>
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label={t('chat.context_meter.aria_label_breakdown', { pct })}
          className={cn(
            'inline-flex min-h-6 items-center gap-1 rounded px-1.5 text-xs transition-colors hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
            pct >= 85 ? 'text-status-error' : 'text-muted-foreground',
          )}
          data-testid="context-meter"
        >
          <Brain size={12} />
          context {pct}%
        </button>

        <Dialog open={open} onOpenChange={setOpen}>
          <DialogContent className="max-w-sm rounded-2xl border border-border bg-popover p-5 shadow-2xl">
            <DialogTitle className="mb-1 text-base font-black text-foreground">{t('chat.context_meter.dialog_title_usage')}</DialogTitle>
            <DialogDescription className="mb-3 text-sm leading-relaxed text-muted-foreground">
              <Trans
                i18nKey="chat.context_meter.usage_summary"
                values={{ pct }}
                components={{ pctval: <span className="font-bold text-foreground" /> }}
              />
              {ctxWindow > 0 && <>{t('chat.context_meter.usage_tokens', { total: formatTokens(total), window: formatTokens(ctxWindow) })}</>}{t('chat.context_meter.usage_summary_tail')}
            </DialogDescription>

            {entries.length > 0 ? (
              <div className="flex flex-col gap-2">
                {entries.map(([key, tok]) => {
                  const w = total > 0 ? Math.round((tok / total) * 100) : 0;
                  return (
                    <div key={key} className="text-xs">
                      <div className="mb-0.5 flex justify-between">
                        <span className="font-medium text-foreground">{CATEGORY_LABEL_KEYS[key] ? t(CATEGORY_LABEL_KEYS[key]) : key}</span>
                        <span className="text-muted-foreground">{formatTokens(tok)} · {w}%</span>
                      </div>
                      <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                        <div className="h-full rounded-full bg-primary" style={{ width: `${w}%` }} />
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">{t('chat.context_meter.no_breakdown')}</p>
            )}

            <div className="mt-4 flex justify-end">
              <DialogClose className="rounded-xl bg-primary-button px-4 py-2 text-sm font-bold text-white hover:bg-primary-button/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary">
                {t('chat.context_meter.got_it')}
              </DialogClose>
            </div>
          </DialogContent>
        </Dialog>
      </>
    );
  }

  // ── Fallback：尚無 token 用量 → 以來回數估算（HAX G12 說明面板）──
  const rounds = countRounds(messages);
  if (rounds === 0) return null;
  const pct = contextUsagePct(rounds);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={t('chat.context_meter.aria_label_loaded', { pct })}
        className={cn(
          'inline-flex min-h-6 items-center gap-1 rounded px-1.5 text-xs transition-colors hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
          pct >= 85 ? 'text-status-error' : 'text-muted-foreground',
        )}
        data-testid="context-meter"
      >
        <Brain size={12} />
        context {pct}%
      </button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-xs rounded-2xl border border-border bg-popover p-5 shadow-2xl">
          <DialogTitle className="mb-2 text-base font-black text-foreground">{t('chat.context_meter.dialog_title_loaded')}</DialogTitle>
          <DialogDescription className="text-sm leading-relaxed text-muted-foreground">
            <Trans
              i18nKey="chat.context_meter.loaded_summary"
              values={{ rounds, limit: CONTEXT_ROUND_LIMIT, pct }}
              components={{ roundsval: <span className="font-bold text-foreground" /> }}
            />
            <br />
            <br />
            {t('chat.context_meter.loaded_detail')}
          </DialogDescription>
          <div className="mt-4 flex justify-end">
            <DialogClose className="rounded-xl bg-primary-button px-4 py-2 text-sm font-bold text-white hover:bg-primary-button/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary">
              {t('chat.context_meter.got_it')}
            </DialogClose>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
