"use client";

/**
 * hitl-unification H3：聊天內嵌工具審批卡。
 *
 * Gate-2 對 CONFIRM 決策把 `tool_approval_request` 事件推進聊天 SSE（H2 統一
 * broker）並阻塞等待——本卡渲染於對應工具步驟下方：工具名／參數預覽／風險章／
 * 倒數，批准或拒絕經 `POST /v1/tools/approve|reject/{execId}` 解算（is_human 硬閘
 * 在後端）。解算結果（含逾時自動拒）由 `tool_approval_resolved` 事件收斂
 * （stream_tree 寫進 step.approval.decision），多筆並行各卡獨立。
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Check, ShieldQuestion, UserCheck, X } from "lucide-react";
import { fetchTouka, getActiveProxyUrl } from "@/lib/connections";
import { errorMsg, getErrorMessage, logSSOTError } from "@/lib/error_handler";
import type { ToolStep } from "@/lib/tree_history";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { IsolationNotice } from "./IsolationNotice";

const RISK_STYLES: Record<string, string> = {
  read_only: "bg-muted text-muted-foreground",
  safe: "bg-status-warning/15 text-status-warning",
  dangerous: "bg-status-error/15 text-status-error",
};

export function ToolApprovalCard({ step }: { step: ToolStep }) {
  const { t } = useTranslation();
  const [submitting, setSubmitting] = useState<"approve" | "reject" | null>(null);
  const [secondsLeft, setSecondsLeft] = useState<number | null>(
    typeof step.approval?.timeout === "number" ? Math.round(step.approval.timeout) : null,
  );

  // 本地倒數（純顯示）：逾時的權威判定在後端（自動拒＋resolved 事件）
  useEffect(() => {
    if (secondsLeft === null || step.approval?.decision) return;
    const timer = setInterval(() => {
      setSecondsLeft(prev => (prev === null || prev <= 0 ? 0 : prev - 1));
    }, 1000);
    return () => clearInterval(timer);
  }, [secondsLeft === null, step.approval?.decision]); // eslint-disable-line react-hooks/exhaustive-deps

  const argsPreview = useMemo(() => {
    if (!step.args || typeof step.args !== "object") return null;
    return Object.entries(step.args as Record<string, unknown>).map(([key, value]) => (
      <div key={key} className="flex gap-1.5 min-w-0">
        <span className="shrink-0 font-bold opacity-60">{key}:</span>
        {/* A7：完整換行顯示（含整條指令），不再單行省略 */}
        <span className="min-w-0 whitespace-pre-wrap break-all font-mono opacity-80">
          {typeof value === "string" ? value : JSON.stringify(value)}
        </span>
      </div>
    ));
  }, [step.args]);

  const resolve = async (action: "approve" | "reject") => {
    const execId = step.approval?.execId;
    if (!execId || submitting) return;
    setSubmitting(action);
    try {
      // 路徑分寫為字面常量段（Guard #2 API parity 以字面路徑對後端路由表）
      const path =
        action === "approve"
          ? `/v1/tools/approve/${execId}`
          : `/v1/tools/reject/${execId}`;
      // iso-c2：核准 none 卡時送回卡片上顯示的去處，後端對得上才記同意
      const isolation = step.approval?.isolation;
      const ack = action === "approve" && isolation?.level === "none" ? isolation.fallback : null;
      const init: RequestInit = ack
        ? {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ payload: { isolation_ack: ack } }),
          }
        : { method: "POST" };
      const res = await fetchTouka(getActiveProxyUrl(), path, init);
      if (!res.ok) {
        toast.error(await getErrorMessage(res));
        setSubmitting(null); // 解算失敗（404 過期/403）→ 還原按鈕，卡片終態靠 resolved 事件
      }
      // 成功：不還原 submitting——等 tool_approval_resolved 事件把 decision 寫進 step
    } catch (err) {
      logSSOTError(err, `ToolApprovalCard:${action}:${execId}`);
      toast.error(errorMsg("NETWORK_CONNECTION_ERROR"));
      setSubmitting(null);
    }
  };

  const decision = step.approval?.decision;
  if (decision) {
    // 已解算：收斂為狀態徽章（行狀態本身會轉 running/denied/error）
    return (
      <div className="mt-1 flex items-center gap-1.5 text-xs opacity-70">
        <ShieldQuestion size={12} />
        <span>{t(`chat.tool_approval_card.decision.${decision}`)}</span>
      </div>
    );
  }

  const risk = step.approval?.risk || "dangerous";
  const isolation = step.approval?.isolation;
  const isolationUnreadable = isolation?.level === "none" && isolation.fallback === null;
  // C2-3：未隔離卡沒有完整顯示要執行的內容（後端同樣不算核准）
  const isolationIncomplete = isolation?.complete === false;

  return (
    <div className="mt-1.5 rounded-xl border border-status-warning/40 bg-status-warning/5 p-3 flex flex-col gap-2">
      <div className="flex items-center gap-2 text-xs font-bold">
        <ShieldQuestion size={14} className="text-status-warning shrink-0" />
        <span className="uppercase tracking-wider">
          {t("chat.tool_approval_card.title", { tool: step.tool })}
        </span>
        <span
          className={cn(
            "ml-auto shrink-0 rounded px-1.5 py-0.5 text-xs font-black uppercase",
            RISK_STYLES[risk] || RISK_STYLES.dangerous,
          )}
        >
          {t(`chat.tool_approval_card.risk.${risk}`, { defaultValue: risk })}
        </span>
      </div>

      {step.approval?.humanOnly && (
        <div className="flex items-start gap-1.5 rounded-lg bg-status-error/10 p-2 text-xs text-status-error">
          <UserCheck size={12} className="mt-0.5 shrink-0" />
          <span>
            <span className="font-bold">{t("chat.tool_approval_card.human_only")}</span>
            {" — "}
            {t("chat.tool_approval_card.human_only_desc")}
          </span>
        </div>
      )}

      {isolation && <IsolationNotice isolation={isolation} />}

      {step.approval?.reviewReason && (
        <p className="text-xs whitespace-pre-wrap break-words opacity-80">
          {t("chat.tool_approval_card.review_reason", { reason: step.approval.reviewReason })}
        </p>
      )}

      {step.approval?.hookReason !== undefined && (
        <p className="text-xs whitespace-pre-wrap break-words opacity-80">
          {step.approval.hookReason
            ? t("chat.tool_approval_card.hook_reason", { reason: step.approval.hookReason })
            : t("chat.tool_approval_card.hook_ask")}
        </p>
      )}

      {argsPreview && (
        <div className="rounded-lg bg-background/60 p-2 text-xs flex flex-col gap-0.5 max-h-64 overflow-y-auto custom-scrollbar">
          {argsPreview}
        </div>
      )}

      <div className="flex items-center gap-2">
        <Button
          size="sm"
          className="h-7 px-3 text-xs font-bold gap-1"
          disabled={!!submitting || isolationUnreadable || isolationIncomplete}
          onClick={() => resolve("approve")}
        >
          <Check size={12} />
          {t("chat.tool_approval_card.approve")}
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="h-7 px-3 text-xs font-bold gap-1 text-status-error border-status-error/40 hover:bg-status-error/10"
          disabled={!!submitting}
          onClick={() => resolve("reject")}
        >
          <X size={12} />
          {t("chat.tool_approval_card.reject")}
        </Button>
        {secondsLeft !== null && (
          <span className="ml-auto text-xs opacity-50 tabular-nums">
            {t("chat.tool_approval_card.countdown", { seconds: secondsLeft })}
          </span>
        )}
      </div>
    </div>
  );
}
