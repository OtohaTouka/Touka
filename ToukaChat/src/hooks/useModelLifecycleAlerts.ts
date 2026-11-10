import { useEffect, useState } from "react";
import { captureRequestScope, fetchTouka, getActiveProxyUrl, subscribeCredentialScope } from "@/lib/connections";
import type { ModelLifecycleAlert } from "@/lib/types";

/** 分頁開很久也要跟上；只問自己的後端，廠商清單後端一天最多查一次。 */
export const MODEL_LIFECYCLE_POLL_MS = 3_600_000;
/** 後端還在背景查清單時，等一下再問一次。 */
export const MODEL_LIFECYCLE_RECHECK_MS = 5_000;

const ALERTS_PATH = "/v1/models/lifecycle-alerts";
const STATUSES: readonly unknown[] = ["retiring", "overdue", "date_unknown", "retired", "not_found"];
const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function parseAlert(value: unknown): ModelLifecycleAlert | null {
  if (!isRecord(value) || typeof value.model !== "string" || !STATUSES.includes(value.status)) return null;
  return {
    model: value.model,
    status: value.status as ModelLifecycleAlert["status"],
    retires_at: typeof value.retires_at === "number" ? value.retires_at : null,
    souls: Array.isArray(value.souls) ? value.souls.filter(soul => typeof soul === "string") : [],
    reviewer: value.reviewer === true,
    summarizer: value.summarizer === true,
  };
}

/** 看不懂的回應當成沒有（不顯示提示）；看不懂的一筆只略過那一筆。 */
function parseAlerts(body: unknown): { alerts: ModelLifecycleAlert[]; checking: boolean } {
  if (!isRecord(body) || !Array.isArray(body.alerts)) return { alerts: [], checking: false };
  return {
    alerts: body.alerts.map(parseAlert).filter(alert => alert !== null),
    checking: body.checking === true,
  };
}

/** GET /v1/models/lifecycle-alerts（登入後才問；憑證換了先清空再重問）。 */
export function useModelLifecycleAlerts(): ModelLifecycleAlert[] {
  const [alerts, setAlerts] = useState<ModelLifecycleAlert[]>([]);
  useEffect(() => {
    let controller: AbortController | undefined;
    let recheck: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      clearTimeout(recheck);
      controller?.abort();
      const scope = captureRequestScope();
      if (!scope) {
        setAlerts([]);
        return;
      }
      const current = new AbortController();
      controller = current;
      try {
        const res = await fetchTouka(getActiveProxyUrl(), ALERTS_PATH, {
          cache: "no-store", signal: current.signal, nonStreaming: true, requestScope: scope,
        });
        if (!res.ok) return; // 暫時失敗不清掉上一次的結果；換帳號、登出已經先清空
        const body: unknown = await res.json();
        if (current.signal.aborted) return;
        const parsed = parseAlerts(body);
        setAlerts(parsed.alerts);
        if (parsed.checking) recheck = setTimeout(() => { void load(); }, MODEL_LIFECYCLE_RECHECK_MS);
      } catch {
        // 同上：連不上時保留上一次的結果
      }
    };
    const timer = setTimeout(() => { void load(); }, 0);
    const poll = setInterval(() => { void load(); }, MODEL_LIFECYCLE_POLL_MS);
    const unsubscribe = subscribeCredentialScope(() => {
      setAlerts([]); // 上一個人的靈魂名字不能留到下一個人的畫面
      void load();
    });
    return () => {
      clearTimeout(timer);
      clearTimeout(recheck);
      clearInterval(poll);
      unsubscribe();
      controller?.abort();
    };
  }, []);
  return alerts;
}
