/**
 * 廠商的錯誤原文（只有人類 admin 的事件帶，路線圖第 8 項）。
 *
 * 只放記憶體：對話樹會存檔、會和存檔比對，靈魂和擁有者也讀得到存檔，所以原文不能進樹。
 * 重新整理後就沒有了，畫面只剩分類。
 */
import { create } from 'zustand';
import { GENERATION_FAILURE_KIND } from '@/hooks/useProgress';

/** 只給當下看，留最近幾則就夠。 */
export const MAX_FAILURE_DETAILS = 20;

interface FailureDetailState {
  /** 助理節點 id → 廠商原文 */
  details: Record<string, string>;
}

export const useFailureDetails = create<FailureDetailState>(() => ({ details: {} }));

export function rememberFailureDetail(assistantId: string, event: Record<string, unknown>): void {
  const { type, kind, detail } = event;
  if (type !== 'touka_progress' || kind !== GENERATION_FAILURE_KIND || typeof detail !== 'string' || !detail) return;
  useFailureDetails.setState(({ details }) => {
    const others = Object.entries(details).filter(([id]) => id !== assistantId);
    return { details: { ...Object.fromEntries(others.slice(1 - MAX_FAILURE_DETAILS)), [assistantId]: detail } };
  });
}
