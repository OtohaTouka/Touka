import { useTranslation } from "react-i18next";
import type { ToolStepIsolation } from "@/lib/tree_history";
import { netServicesKey } from "@/lib/isolation_wording";

/** 隔離說明的翻譯根（審批卡與側欄橫幅共用原因說明）。 */
export const ISOLATION_KEY = "chat.tool_approval_card.isolation";

type ReasonContext = Pick<ToolStepIsolation, "expected" | "reason" | "reasonDetail">;

/** 一個原因代碼的說明（細節專屬說明優先，其次原因說明；都沒有就是代碼本身）。 */
export function useIsolationReasonText() {
  const { t, i18n } = useTranslation();
  return (code: string, context: ReasonContext) => {
    const keys = [
      ...(context.reasonDetail && code === context.reason
        ? [`${ISOLATION_KEY}.reason_details.${code}.${context.reasonDetail}`]
        : []),
      `${ISOLATION_KEY}.reasons.${code}`,
    ];
    const key = keys.find(k => i18n.exists(k));
    const service = t(`${ISOLATION_KEY}.net_services.${netServicesKey(context.expected)}`);
    return key ? t(key, { service }) : code;
  };
}
