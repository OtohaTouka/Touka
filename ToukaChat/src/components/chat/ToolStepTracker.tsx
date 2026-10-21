// src/components/chat/ToolStepTracker.tsx
"use client";

import { useState, useMemo } from 'react';
import { useTranslation } from "react-i18next";
import { ToolStep } from '@/lib/tree_history';
import { parseToolResult } from '@/lib/tool_result';
import { ToolApprovalCard } from './ToolApprovalCard';
import { McpInteractionCard } from './McpInteractionCard';
import { cn } from '@/lib/utils';
import {
  CheckCircle2,
  AlertCircle,
  ShieldAlert,
  ShieldQuestion,
  Loader2,
  ChevronDown,
  ChevronRight,
  Terminal,
  Database,
  Wrench,
  FileText,
  Globe,
  Brain,
  Code
} from 'lucide-react';

interface ToolStepTrackerProps {
  steps: ToolStep[];
}

// HAX G11（H3）：對記憶／檔案庫工具，從既有 args 萃取「使用了哪些記憶」的簡述（純前端、唯讀解析）。
const MEMORY_TOOL = /memory|vault|recall|記憶/i;
function describeMemoryAccess(step: ToolStep): string | null {
  if (!MEMORY_TOOL.test(step.tool)) return null;
  const a = step.args;
  if (!a || typeof a !== 'object') return null;
  const rec = a as Record<string, unknown>;
  const str = (k: string) => (typeof rec[k] === 'string' ? (rec[k] as string).trim() : '');
  const tags = Array.isArray(rec.tags) ? (rec.tags as unknown[]).filter((t) => typeof t === 'string') : [];
  const desc =
    str('query') ||
    str('content') ||
    str('path') ||
    (tags.length ? (tags as string[]).map((t) => `#${t}`).join(' ') : '') ||
    str('action');
  // 截斷至 40 字：足以辨識「用了哪些記憶」（透明化），同時限制敏感內容在 UI 的曝光。
  return desc ? desc.slice(0, 40) : null;
}

export function ToolStepTracker({ steps }: ToolStepTrackerProps) {
  if (!steps || steps.length === 0) return null;

  return (
    <div className="flex flex-col gap-1.5 my-3 w-full max-w-full overflow-hidden border-l-2 border-primary/20 pl-3">
      {steps.map((step, idx) => (
        <ToolStepItem key={step.id || idx} step={step} />
      ))}
    </div>
  );
}

function ToolStepItem({ step }: { step: ToolStep }) {
  const { t } = useTranslation();
  const [isOpen, setIsOpen] = useState(false);

  // P3：解析結構化封套（P1/P2）→決定顯示狀態；denied 由 error_class=permission_denied 判定。
  // H3：awaiting_approval＝統一審批卡待決（黃）——批准後 tool_start 就地升級 running。
  const parsed = useMemo(() => parseToolResult(step.result), [step.result]);
  const displayStatus =
    step.status === 'awaiting_approval'
      ? 'awaiting'
      : step.status === 'running'
        ? 'running'
        : parsed.kind === 'denied'
          ? 'denied'
          : parsed.kind === 'error' || step.status === 'error'
            ? 'error'
            : 'success';

  const getIcon = () => {
    if (displayStatus === 'awaiting') return <ShieldQuestion className="text-status-warning animate-pulse" size={14} />;
    if (displayStatus === 'running') return <Loader2 className="animate-spin text-primary" size={14} />;
    if (displayStatus === 'denied') return <ShieldAlert className="text-status-warning" size={14} />;
    if (displayStatus === 'error') return <AlertCircle className="text-status-error" size={14} />;
    return <CheckCircle2 className="text-status-success" size={14} />;
  };

  const containerColorClass =
    displayStatus === 'awaiting' || displayStatus === 'denied'
      ? 'text-status-warning'
      : displayStatus === 'error'
        ? 'text-status-error'
        : 'text-muted-foreground';

  const getToolIcon = (name: string) => {
    const n = name.toLowerCase();
    if (n.includes('bash') || n.includes('terminal')) return <Terminal size={12} />;
    if (n.includes('search') || n.includes('google')) return <Globe size={12} />;
    if (n.includes('memory') || n.includes('database') || n.includes('vault')) return <Database size={12} />;
    if (n.includes('read') || n.includes('file') || n.includes('list')) return <FileText size={12} />;
    if (n.includes('investigator') || n.includes('speculate')) return <Brain size={12} />;
    if (n.includes('edit') || n.includes('replace') || n.includes('write')) return <Code size={12} />;
    return <Wrench size={12} />;
  };

  // P2b：執行中若已有 live 輸出（streamed result）也允許展開即時觀看
  const canExpand = !!step.result;
  const memoryAccess = describeMemoryAccess(step);

  return (
    <div className={cn(
      "overflow-hidden transition-all duration-200",
      containerColorClass
    )}>
      {/* WCAG 2.1.1/4.1.2（A2）：展開列改 button 語意（鍵盤可達＋aria-expanded）；
          不可展開時 disabled（不提供假互動） */}
      <button
        type="button"
        disabled={!canExpand}
        aria-expanded={canExpand ? isOpen : undefined}
        className={cn(
          "flex items-center gap-2 py-1 px-0 w-full text-left bg-transparent select-none text-xs font-medium",
          canExpand
            ? "cursor-pointer hover:text-primary transition-colors rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
            : "cursor-default"
        )}
        onClick={() => canExpand && setIsOpen(!isOpen)}
      >
        {getIcon()}
        <div className="flex items-center gap-1.5 flex-1 min-w-0">
          {getToolIcon(step.tool)}
          <span className="truncate uppercase tracking-wider font-black opacity-80">
            {step.status === 'awaiting_approval'
              ? t('chat.tool_step_tracker.status.awaiting_approval')
              : step.status === 'running'
                ? t('chat.tool_step_tracker.status.calling')
                : t('chat.tool_step_tracker.status.called')}{t('chat.tool_step_tracker.label_tool_separator')}{step.tool}
          </span>
          {memoryAccess && (
            <span
              className="inline-flex shrink-0 items-center gap-1 rounded bg-primary/10 px-1.5 py-0.5 text-xs font-bold text-primary"
              title={t('chat.tool_step_tracker.memory_used_title', { access: memoryAccess })}
            >
              <Database size={10} />
              {t('chat.tool_step_tracker.memory_badge', { access: memoryAccess })}
            </span>
          )}
          {!!step.args && (
            <span className="text-xs opacity-40 truncate font-mono hidden sm:inline">
              {JSON.stringify(step.args).slice(0, 100)}
            </span>
          )}
        </div>
        {canExpand && (
          <div className="opacity-40">
            {isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          </div>
        )}
      </button>

      {/* H3：待決審批 → 內嵌審批卡（批准/拒絕/倒數）；解算後收斂為狀態徽章 */}
      {step.approval && (step.status === 'awaiting_approval' || step.approval.decision) && (
        <ToolApprovalCard step={step} />
      )}
      {step.interaction && <McpInteractionCard step={step} />}

      {isOpen && !!step.result && (
        <div className="mt-1 border rounded-lg border-border bg-muted/50 max-h-64 overflow-y-auto custom-scrollbar">
          {parsed.kind === 'raw' ? (
            // 純文字（bash markdown／legacy）：維持原樣顯示
            <div className="p-2.5 font-mono text-xs leading-tight break-all whitespace-pre-wrap opacity-80">
              {parsed.raw}
            </div>
          ) : (
            // P3：結構化封套→渲染人話（message／建議／變更片段），不外顯原始 JSON
            <div className="p-2.5 text-xs leading-snug opacity-90 flex flex-col gap-1.5">
              {parsed.message && <div className="break-words font-medium">{parsed.message}</div>}
              {parsed.suggestion && (
                <div className="break-words text-muted-foreground">
                  <span className="font-bold">{t('chat.tool_step_tracker.suggestion_label')}</span>
                  {parsed.suggestion}
                </div>
              )}
              {parsed.preview && (
                <pre className="m-0 rounded bg-black/10 p-1.5 font-mono break-all whitespace-pre-wrap opacity-80">
                  {parsed.preview}
                </pre>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
