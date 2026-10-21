import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import { useIsolationReasonText } from "@/hooks/useIsolationReasonText";
import type { McpServerStatus } from "@/lib/types";

const KEY = "settings.mcp_isolation";
const SELFTEST_REASONS = new Set([
  "selftest_failed",
  "selftest_error",
  "fs_9p",
]);

export function McpIsolationBadge({
  isolation,
}: {
  isolation: McpServerStatus["isolation"];
}) {
  const { t, i18n } = useTranslation();
  const reasonText = useIsolationReasonText();
  if (!isolation || isolation.class === "remote") return null;
  const state = isolation.state;
  const badge =
    state === "suspended"
      ? "suspended"
      : state === "waiting_isolation" || state === "waiting_probe"
        ? "waiting"
        : state === "running" && isolation.level === "none"
          ? "unisolated"
          : null;
  if (!badge) return null;
  const reason = isolation.reason ?? "unknown";
  const ownKey = `${KEY}.reasons.${reason}`;
  const explanation = i18n.exists(ownKey)
    ? t(ownKey)
    : reasonText(reason, { reason });
  const recovery =
    isolation.class === "self" && state === "waiting_isolation"
      ? "waiting_self"
      : reason === "probe_pending"
        ? null
        : SELFTEST_REASONS.has(reason)
          ? "retry_selftest"
          : "check_environment";

  return (
    <span className="flex max-w-md flex-col items-start gap-1 text-xs font-normal">
      <Badge
        variant="outline"
        className="border-status-warning/40 text-status-warning whitespace-normal"
      >
        {t(`${KEY}.badges.${badge}`)}
      </Badge>
      <span className="text-muted-foreground break-words whitespace-pre-wrap">
        {explanation === reason ? t(`${KEY}.reasons.unknown`) : explanation}{" "}
        {badge !== "unisolated" && recovery && t(`${KEY}.${recovery}`)}
      </span>
    </span>
  );
}
