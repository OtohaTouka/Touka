import { useState } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, RotateCw } from "lucide-react";
import { toast } from "sonner";
import { InterruptedRun } from "@/hooks/useSystemMonitoring";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { logSSOTError, errorMsg } from "@/lib/error_handler";

interface InterruptedRunsNoticeProps {
  runs: InterruptedRun[];
}

const runKey = (r: InterruptedRun) => `${r.agent ?? ""}:${r.session_id ?? ""}`;

/**
 * 當機復原可見化（P5c）：上次程序當機/被強制關閉時，有背景心跳迴圈未跑完。
 * 顯示「⚠️ 上次有 N 個未完成的背景任務」，每筆可「一鍵重新觸發」（P5c bonus）：
 * 對既有會話派發延續性背景脈衝（後端強制 Auto、僅擁有者可觸發）。
 * 無未完成任務時整個元件不顯示。
 */
export function InterruptedRunsNotice({ runs }: InterruptedRunsNoticeProps) {
  const { t } = useTranslation();
  const [acted, setActed] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);

  const items = (runs || []).filter((r) => !acted.has(runKey(r)));
  if (items.length === 0) return null;

  const retrigger = async (run: InterruptedRun) => {
    if (!run.session_id || !run.agent) return;
    const key = runKey(run);
    setBusy(key);
    try {
      const resp = await fetchTouka(getActiveProxyUrl(), "/v1/heartbeat/retrigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_id: run.agent, session_id: run.session_id }),
      });
      if (resp.ok) {
        toast.success(t("sidebar.interrupted_runs_notice.retrigger_success", { agent: run.agent }));
        setActed((prev) => new Set(prev).add(key));
      } else {
        toast.error(errorMsg("NETWORK_CONNECTION_ERROR"));
      }
    } catch (e) {
      logSSOTError(e, "InterruptedRunsNotice:retrigger");
      toast.error(errorMsg("NETWORK_CONNECTION_ERROR"));
    } finally {
      setBusy(null);
    }
  };

  return (
    // WCAG 4.1.3：「上次有 N 個未完成」通知出現時須讓螢幕閱讀器聽見（非緊急 → status 而非 alert）
    <div
      role="status"
      className="bg-status-warning/10 p-3 rounded-xl border border-status-warning/30 space-y-2"
      data-testid="interrupted-runs-notice"
    >
      <div className="flex items-center gap-2 text-xs font-bold text-status-warning uppercase">
        <AlertTriangle size={12} />
        {t("sidebar.interrupted_runs_notice.heading", { count: items.length })}
      </div>
      <div className="space-y-1">
        {items.map((run) => {
          const key = runKey(run);
          const canRetrigger = !!(run.session_id && run.agent);
          return (
            <div
              key={key}
              className="flex items-center justify-between gap-2 text-xs pl-1"
            >
              <span className="text-muted-foreground truncate min-w-0">
                {run.agent || run.session_id || t("sidebar.interrupted_runs_notice.unknown")}
              </span>
              {canRetrigger && (
                <button
                  type="button"
                  onClick={() => retrigger(run)}
                  disabled={busy === key}
                  className="flex items-center gap-1 text-status-warning font-bold hover:opacity-80 disabled:opacity-50 whitespace-nowrap"
                >
                  <RotateCw size={11} className={busy === key ? "animate-spin" : ""} />
                  {t("sidebar.interrupted_runs_notice.retrigger")}
                </button>
              )}
            </div>
          );
        })}
      </div>
      <p className="text-xs text-muted-foreground pl-1">{t("sidebar.interrupted_runs_notice.aborted_hint")}</p>
    </div>
  );
}
