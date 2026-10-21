import { MessageNode } from './tree_history';

// 與後端 sliding_window `upper_limit_rounds` 預設一致（150 來回 = user+assistant）。
export const CONTEXT_ROUND_LIMIT = 150;

/**
 * 計算完整來回數（一個 user 後接一個 assistant = 1 round），對齊後端
 * `history_manager.count_rounds_from_linear` 的 SSOT 定義（中間的 system 等訊息跳過）。
 */
export function countRounds(messages: MessageNode[]): number {
  let rounds = 0;
  let i = 0;
  while (i < messages.length) {
    if (messages[i].role === 'user') {
      let j = i + 1;
      while (
        j < messages.length &&
        messages[j].role !== 'user' &&
        messages[j].role !== 'assistant'
      ) {
        j++;
      }
      if (j < messages.length && messages[j].role === 'assistant') {
        rounds++;
        i = j + 1;
        continue;
      }
    }
    i++;
  }
  return rounds;
}

/** context 佔用百分比（0..100），以 round 數 / 上限估算（封頂 100）。 */
export function contextUsagePct(rounds: number, limit: number = CONTEXT_ROUND_LIMIT): number {
  if (limit <= 0) return 0;
  return Math.min(100, Math.round((rounds / limit) * 100));
}
