// src/components/chat/AgentActivityStrip.tsx
"use client";

import { useTranslation } from 'react-i18next';
import { AgentProgress, ProgressMap } from '@/lib/tree_history';
import { describeProgress } from '@/hooks/useProgress';
import {
  Activity,
  RefreshCw,
  Users,
  Sparkles,
  RotateCcw,
  Coins,
  Loader2,
} from 'lucide-react';

interface AgentActivityStripProps {
  progress?: ProgressMap;
}

function kindIcon(kind: string) {
  switch (kind) {
    case 'react_slots':
      return <Activity size={12} />;
    case 'repair':
      return <RefreshCw size={12} />;
    case 'group':
      return <Users size={12} />;
    case 'creation_stage':
      return <Sparkles size={12} />;
    case 'tool_retry':
      return <RotateCcw size={12} />;
    case 'token_usage':
      return <Coins size={12} />;
    default:
      return <Loader2 size={12} className="animate-spin" />;
  }
}

/**
 * 「Agent 活動」strip：把結構化進度事件（ReAct 自主迴圈、終端自我修復、
 * 群聊 roster、創建流水線、工具重試）渲染成一排可掃視的狀態膠囊，
 * 讓使用者在 agent 進行複數行動時看得到「現在跑到哪、共幾步」。
 */
export function AgentActivityStrip({ progress }: AgentActivityStripProps) {
  const { t } = useTranslation();
  const items: AgentProgress[] = progress ? Object.values(progress) : [];
  if (items.length === 0) return null;

  return (
    // WCAG 4.1.3：agent 行動進度（ReAct 迴圈等）變化須讓螢幕閱讀器聽見
    <div
      role="status"
      className="flex flex-wrap items-center gap-1.5 my-2"
      data-testid="agent-activity-strip"
    >
      {items.map((p) => (
        <span
          key={p.kind}
          className="inline-flex items-center gap-1 rounded-full border border-primary/20 bg-primary/5 px-2 py-0.5 text-xs font-medium text-muted-foreground"
        >
          {kindIcon(p.kind)}
          {describeProgress(p, t)}
        </span>
      ))}
    </div>
  );
}
