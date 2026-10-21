import { useEffect, useState } from "react";
import { captureRequestScope, fetchTouka, getActiveProxyUrl, subscribeCredentialScope } from "@/lib/connections";
import type { components } from "@/types/api";

export type IsolationStatus = components["schemas"]["IsolationStatusResponse"];

/** 執行器重啟、權限被改都是執行中才發生：側欄每 30 秒重問一次。 */
export const ISOLATION_POLL_MS = 30_000;

const LEVELS: readonly unknown[] = ["none", "landlock", "executor"];
const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const optionalText = (value: unknown) => value === null || typeof value === "string";
const safeCount = (value: unknown): number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0;

/** 看不懂的回應當成沒有（不顯示橫幅）；審批卡才是把關的地方。 */
function parseIsolationStatus(value: unknown): IsolationStatus | null {
  if (!isRecord(value) || !LEVELS.includes(value.level) || !LEVELS.includes(value.expected)) return null;
  const features = value.features;
  if (!Array.isArray(features) || !features.every(name => typeof name === "string")) return null;
  const fields = [value.reason, value.detail, value.local_issue, value.block_ports_issue];
  if (!fields.every(optionalText) || !isRecord(value.local) || !isRecord(value.mcp)) return null;
  if (value.executor !== null && !isRecord(value.executor)) return null;
  if (value.repairs !== null && !isRecord(value.repairs)) return null;
  const mcp = {
    ...value.mcp,
    unisolated: safeCount(value.mcp.unisolated),
    waiting: safeCount(value.mcp.waiting),
    suspended: safeCount(value.mcp.suspended),
  };
  return { ...value, mcp } as unknown as IsolationStatus;
}

/** GET /v1/isolation（登入後才問；憑證換了或每 30 秒重問）。沒有結果時回 null。 */
export function useIsolationStatus(): IsolationStatus | null {
  const [status, setStatus] = useState<IsolationStatus | null>(null);
  useEffect(() => {
    let controller: AbortController | undefined;
    const load = async () => {
      controller?.abort();
      const scope = captureRequestScope();
      if (!scope) {
        setStatus(null);
        return;
      }
      const current = new AbortController();
      controller = current;
      try {
        const res = await fetchTouka(getActiveProxyUrl(), "/v1/isolation", {
          cache: "no-store", signal: current.signal, nonStreaming: true, requestScope: scope,
        });
        const body: unknown = res.ok ? await res.json() : null;
        if (!current.signal.aborted) setStatus(parseIsolationStatus(body));
      } catch {
        if (!current.signal.aborted) setStatus(null);
      }
    };
    const timer = setTimeout(() => { void load(); }, 0);
    const poll = setInterval(() => { void load(); }, ISOLATION_POLL_MS);
    const unsubscribe = subscribeCredentialScope(() => { void load(); });
    return () => {
      clearTimeout(timer);
      clearInterval(poll);
      unsubscribe();
      controller?.abort();
    };
  }, []);
  return status;
}
