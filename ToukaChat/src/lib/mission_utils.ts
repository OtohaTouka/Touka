/**
 * 從 mission metadata 取出「已分派給外部 AI（等待回呼）」的 task id 集合。
 *
 * 後端 mission metadata 的 dispatch_callbacks 對映 task_id -> callback_token；有 token =
 * 該任務由外部 AI（如 claude-code）執行、等待 fire-and-forget 回呼（ADR-0051/0067）。
 */
export function getDispatchedTaskIds(
  metadata: { dispatch_callbacks?: Record<string, string> } | undefined | null,
): Set<string> {
  return new Set(Object.keys(metadata?.dispatch_callbacks || {}));
}
