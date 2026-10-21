import { ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useIsolationStatus } from "@/hooks/useIsolationStatus";
import { ISOLATION_KEY, useIsolationReasonText } from "@/hooks/useIsolationReasonText";
import { isolationBannerView } from "@/lib/isolation_wording";

const BANNER_KEY = "sidebar.isolation_banner";

/**
 * 隔離橫幅（iso-c2 U2h）：層級低於部署預期、埠沒擋、或自癒額度用完時，在側欄常駐顯示，
 * 原因那幾行沿用審批卡的說明（含補救方式）。樣式與降級橫幅相同。
 */
export function IsolationBanner() {
  const { t } = useTranslation();
  const status = useIsolationStatus();
  const reasonText = useIsolationReasonText();
  if (!status) return null;
  const view = isolationBannerView(status);
  const mcp = status.mcp;
  const unisolated = status.expected !== "none" ? mcp.unisolated : 0;
  const mcpWarning = unisolated > 0 || mcp.waiting > 0 || mcp.suspended > 0;
  if (!view.below && !view.tcpMissing && !view.relock && !mcpWarning) return null;
  const context = {
    expected: status.expected, reason: status.reason ?? undefined, reasonDetail: status.detail ?? undefined,
  };
  return (
    <div
      role="alert"
      data-testid="isolation-banner"
      className="w-full bg-destructive text-destructive-foreground text-xs px-4 py-2 flex items-start gap-2 shrink-0"
    >
      <ShieldAlert size={14} className="shrink-0 mt-0.5" />
      <div className="min-w-0 flex flex-col gap-1 whitespace-pre-wrap break-words">
        {view.below && <p>{t(`${BANNER_KEY}.below`)}</p>}
        {view.reasons.map(code => (
          <p key={code}>{t(`${ISOLATION_KEY}.reason_line`, { text: reasonText(code, context) })}</p>
        ))}
        {view.controlChannel && <p>{t(`${BANNER_KEY}.control_channel`)}</p>}
        {view.tcpMissing && <p>{t(`${BANNER_KEY}.tcp_missing`)}</p>}
        {view.relock && <p>{t(`${BANNER_KEY}.relock`)}</p>}
        {mcpWarning && <p>{t(`${BANNER_KEY}.mcp`, {
          unisolated, waiting: mcp.waiting, suspended: mcp.suspended,
        })}</p>}
      </div>
    </div>
  );
}
