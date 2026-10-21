"use client";

import { useTranslation } from "react-i18next";
import { ShieldAlert, ShieldOff } from "lucide-react";
import type { ToolStepIsolation } from "@/lib/tree_history";
import { isolationClause, isolationReasonCodes, isolationTcpTails } from "@/lib/isolation_wording";
import { ISOLATION_KEY, useIsolationReasonText } from "@/hooks/useIsolationReasonText";
import { cn } from "@/lib/utils";

/**
 * iso-c2：層級 none 時標出「隔離降級／未隔離」與原因。後端以卡片標示記同意的降級去處，
 * 所以 none 卻沒有可顯示的去處時，卡片改顯示「看不懂」並不讓核准。
 * C2-2：開頭＝原因子句＋主句（看降級去處），結尾看 features，原因那幾行都寫補救方式。
 * C2-3：未隔離卡沒有完整顯示要執行的內容（`complete` 為 false）時多一句，卡片不讓核准。
 */
export function IsolationNotice({ isolation }: { isolation: ToolStepIsolation }) {
  const { t } = useTranslation();
  const reasonText = useIsolationReasonText();
  if (isolation.level !== "none") return null;
  const { fallback, features } = isolation;
  const lines = isolationReasonCodes(isolation).map(code => ({
    code, text: t(`${ISOLATION_KEY}.reason_line`, { text: reasonText(code, isolation) }),
  }));

  const degraded = fallback === "landlock" || fallback === "executor";
  const Icon = degraded ? ShieldAlert : ShieldOff;
  const clause = t(`${ISOLATION_KEY}.clauses.${isolationClause(isolation)}`);
  return (
    <div
      className={cn(
        "flex items-start gap-1.5 rounded-lg p-2 text-xs",
        degraded ? "bg-status-warning/10 text-status-warning" : "bg-status-error/10 text-status-error",
      )}
    >
      <Icon size={12} className="mt-0.5 shrink-0" />
      <div className="flex min-w-0 flex-col gap-1">
        {degraded ? (
          <>
            <span className="font-bold">{t(`${ISOLATION_KEY}.degraded_title`)}</span>
            <p data-testid="isolation-lead" className="whitespace-pre-wrap break-words">
              {t(`${ISOLATION_KEY}.${fallback === "executor" ? "executor_head" : "degraded_head"}`, { clause })}
              {isolationTcpTails(isolation).map(tail => t(`${ISOLATION_KEY}.${tail}`)).join("")}
              {/* 執行器是另一個容器：信號碰不到 Touka */}
              {fallback === "landlock" && !features.includes("signal") && t(`${ISOLATION_KEY}.degraded_signal_open`)}
              {t(`${ISOLATION_KEY}.${fallback === "executor" ? "executor_tail" : "degraded_tail"}`)}
            </p>
          </>
        ) : (
          <>
            <span className="font-bold">
              {t(`${ISOLATION_KEY}.${fallback === "direct" ? "direct_title" : "unknown_title"}`)}
            </span>
            <p className="whitespace-pre-wrap break-words">
              {t(`${ISOLATION_KEY}.${fallback === "direct" ? "direct_desc" : "unknown_desc"}`)}
            </p>
          </>
        )}
        {isolation.complete === false && (
          <p className="whitespace-pre-wrap break-words font-bold">{t(`${ISOLATION_KEY}.incomplete`)}</p>
        )}
        {lines.map(line => (
          <p key={line.code} className="whitespace-pre-wrap break-words opacity-80">{line.text}</p>
        ))}
      </div>
    </div>
  );
}
