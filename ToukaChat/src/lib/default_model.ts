import type { ModelInfo } from "./providers/ModelProvider";

/**
 * 預設的一般模型：清單中第一個「啟用、沒隱藏、非代理、不離線」的模型（ADR-0250 A2）。
 * 不寫死模型名；找不到時回傳 undefined，由呼叫端決定不切換。
 */
export function pickDefaultModel(models: readonly ModelInfo[]): ModelInfo | undefined {
  return models.find(m =>
    m.enabled && (m.visibility ?? "visible") !== "hidden" && !m.is_agent && m.status !== "offline");
}

/**
 * 可當基礎／主要模型的一般模型（模型工坊與靈魂頁的下拉共用）：
 * 靈魂、自訂模型（is_agent）、human 與舊靈魂路由（owned_by touka）不列（ADR-0250）。
 */
export function isSelectableBaseModel(m: { is_agent?: boolean; owned_by?: string }): boolean {
  return !m.is_agent && m.owned_by !== "human" && m.owned_by !== "touka";
}
