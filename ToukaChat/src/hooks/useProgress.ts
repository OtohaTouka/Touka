// src/hooks/useProgress.ts
import type { TFunction } from 'i18next';
import { AgentProgress, ProgressMap } from '@/lib/tree_history';

/**
 * 將一筆 touka_progress SSE 事件併入「依 kind 收斂」的進度表。
 *
 * 純函式（可單測）：同一 kind 以最新事件覆蓋（顯示當下狀態），
 * 非 touka_progress 事件或缺 kind 者原樣回傳，不影響既有狀態。
 */
export function reduceProgress(
  prev: ProgressMap | undefined,
  event: Record<string, unknown>
): ProgressMap {
  const base = prev || {};
  if (event.type !== 'touka_progress' || typeof event.kind !== 'string') return base;
  const rest: Record<string, unknown> = { ...event };
  delete rest.type; // 外層 type 不需存進進度節點，只保留 kind 與欄位
  return { ...base, [event.kind]: rest as unknown as AgentProgress };
}

/**
 * 將單筆進度轉為人類可讀的標籤（供「Agent 活動」strip 顯示）。
 * 純函式（可單測）：i18n 的 `t` 以參數注入（本檔非元件/hook，無法呼叫 useTranslation）。
 */
export function describeProgress(p: AgentProgress, t: TFunction): string {
  switch (p.kind) {
    case 'react_slots':
      return t('hooks.use_progress.react_slots', { current: p.current ?? '?', total: p.total ?? '?' });
    case 'repair':
      return t('hooks.use_progress.repair', { attempt: p.attempt ?? '?', max: p.max ?? '?' });
    case 'group':
      return `${t('hooks.use_progress.group', { index: p.index ?? '?', total: p.total ?? '?' })}${p.agent ? ` @${p.agent}` : ''}`;
    case 'creation_stage':
      return `${t('hooks.use_progress.creation_label')}${p.stage ?? ''}${p.status ? `（${p.status}）` : ''}`;
    case 'tool_retry':
      return `${p.tool || t('hooks.use_progress.tool_fallback')} ${t('hooks.use_progress.tool_retry', { attempt: p.attempt ?? '?', max: p.max ?? '?' })}`;
    case 'token_usage':
      return t('hooks.use_progress.token_usage', { total: new Intl.NumberFormat('en-US').format(p.total ?? 0), cost: (p.cost ?? 0).toFixed(4) });
    default:
      return String(p.kind);
  }
}
