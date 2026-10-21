import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchTouka,
  getActiveConnection,
  getActiveProxyUrl,
} from "@/lib/connections";
import { ERRORS } from "@/lib/error_codes";
import { getSystemEpoch } from "@/lib/time_utils";
import type { SoulUpdateInput, SoulUpdateResult } from "@/lib/types";

const SAVE_WAIT_MS = 30_000;
type ErrorCode = keyof typeof ERRORS;
type Phase =
  | { phase: "idle" | "saving" | "saved" }
  | {
      phase: "error";
      code: ErrorCode;
      passwordError: boolean;
      fieldError: boolean;
    }
  | { phase: "unknown"; reconciled: boolean };
type Feedback = Phase & { refreshing: boolean; refreshFailed: boolean };
type Outcome = "saved" | "failed" | "unknown" | "stale" | "ignored";
type Options = {
  proxyUrl: string;
  soulId: string | null;
  readback: (signal: AbortSignal, current: () => boolean) => Promise<boolean>;
};
type Flight = {
  controller: AbortController;
  deadline: number;
  timer: ReturnType<typeof setTimeout>;
};
const idle = (): Feedback => ({
  phase: "idle",
  refreshing: false,
  refreshFailed: false,
});

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function saved(value: unknown, soulId: string): value is SoulUpdateResult {
  return (
    object(value) &&
    Object.values(value).every((item) => typeof item === "string") &&
    value.status === "success" &&
    value.soul_id === soulId
  );
}

function failure(value: unknown, status: number): Phase {
  if (
    status === 422 &&
    object(value) &&
    Array.isArray(value.detail) &&
    value.detail.length > 0 &&
    value.detail.every(
      (issue) =>
        object(issue) &&
        typeof issue.msg === "string" &&
        typeof issue.type === "string" &&
        Array.isArray(issue.loc) &&
        issue.loc.every(
          (part) => typeof part === "string" || typeof part === "number",
        ),
    )
  ) {
    return {
      phase: "error",
      code: "CONFIG_SAVE_FAILED",
      passwordError: false,
      fieldError: true,
    };
  }
  const detail = object(value) && object(value.detail) ? value.detail : null;
  const key = detail?.code;
  const code =
    typeof key === "string" &&
    Object.hasOwn(ERRORS, key) &&
    ERRORS[key as ErrorCode].id === detail?.id &&
    ERRORS[key as ErrorCode].status === status
      ? (key as ErrorCode)
      : null;
  if (!code) return { phase: "unknown", reconciled: false };
  return {
    phase: "error",
    code,
    passwordError: code === "AUTH_REAUTH_FAILED",
    fieldError: false,
  };
}

/** The deadline ends UI waiting; abort never claims to undo a server write. */
function within<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const aborted = () => reject(signal.reason);
    signal.addEventListener("abort", aborted, { once: true });
    if (signal.aborted) aborted();
    promise
      .then(resolve, reject)
      .finally(() => signal.removeEventListener("abort", aborted));
  });
}

export function useSoulSave({ proxyUrl, soulId, readback }: Options) {
  const [feedback, setFeedback] = useState<Feedback>(idle);
  const flight = useRef<Flight | null>(null);
  const mounted = useRef(false);

  const invalidate = useCallback(() => {
    const previous = flight.current;
    flight.current = null;
    if (previous) {
      clearTimeout(previous.timer);
      previous.controller.abort();
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    setFeedback(idle());
    return () => {
      mounted.current = false;
      invalidate();
    };
  }, [proxyUrl, soulId, invalidate]);

  const start = useCallback((): Flight => {
    const configured = getActiveConnection()?.requestTimeoutMs;
    const limit =
      typeof configured === "number" &&
      Number.isFinite(configured) &&
      configured > 0
        ? Math.min(configured, SAVE_WAIT_MS)
        : SAVE_WAIT_MS;
    const controller = new AbortController();
    const task = {
      controller,
      deadline: getSystemEpoch() + limit,
      timer: setTimeout(() => controller.abort(), limit),
    };
    flight.current = task;
    return task;
  }, []);

  const current = useCallback(
    (task: Flight) =>
      mounted.current &&
      flight.current === task &&
      getActiveProxyUrl() === proxyUrl,
    [proxyUrl],
  );
  const alive = useCallback(
    (task: Flight) => {
      if (getSystemEpoch() >= task.deadline) task.controller.abort();
      return current(task) && !task.controller.signal.aborted;
    },
    [current],
  );
  const finish = useCallback((task: Flight) => {
    clearTimeout(task.timer);
    if (flight.current === task) flight.current = null;
  }, []);

  const save = useCallback(
    async (input: SoulUpdateInput): Promise<Outcome> => {
      if (
        !soulId ||
        !mounted.current ||
        flight.current ||
        getActiveProxyUrl() !== proxyUrl
      )
        return "ignored";
      const task = start();
      setFeedback({ phase: "saving", refreshing: false, refreshFailed: false });
      try {
        const response = await within(
          fetchTouka(proxyUrl, `/v1/souls/${encodeURIComponent(soulId)}`, {
            method: "POST",
            body: JSON.stringify(input),
            signal: task.controller.signal,
          }),
          task.controller.signal,
        );
        if (!alive(task)) throw task.controller.signal.reason;
        const body: unknown = await within(
          response.json(),
          task.controller.signal,
        );
        if (!alive(task)) throw task.controller.signal.reason;
        if (!response.ok) {
          const rejected = failure(body, response.status);
          setFeedback({ ...rejected, refreshing: false, refreshFailed: false });
          return rejected.phase === "unknown" ? "unknown" : "failed";
        }
        if (!saved(body, soulId)) {
          setFeedback({
            phase: "unknown",
            reconciled: false,
            refreshing: false,
            refreshFailed: false,
          });
          return "unknown";
        }
        setFeedback({
          phase: "saved",
          refreshing: false,
          refreshFailed: false,
        });
        return "saved";
      } catch {
        if (!current(task)) return "stale";
        setFeedback({
          phase: "unknown",
          reconciled: false,
          refreshing: false,
          refreshFailed: false,
        });
        return "unknown";
      } finally {
        finish(task);
      }
    },
    [proxyUrl, soulId, start, alive, current, finish],
  );

  const refresh = useCallback(async () => {
    if (flight.current || !mounted.current || getActiveProxyUrl() !== proxyUrl)
      return;
    const task = start();
    setFeedback((previous) => ({
      ...previous,
      refreshing: true,
      refreshFailed: false,
    }));
    let ok = false;
    try {
      ok = await within(
        readback(task.controller.signal, () => alive(task)),
        task.controller.signal,
      );
      ok = ok === true && alive(task);
    } catch {
      /* Save acknowledgement remains authoritative when readback fails. */
    } finally {
      if (current(task)) {
        setFeedback((previous) => ({
          ...previous,
          refreshing: false,
          refreshFailed: !ok,
          ...(previous.phase === "unknown" ? { reconciled: ok } : {}),
        }));
      }
      finish(task);
    }
  }, [proxyUrl, start, readback, alive, current, finish]);

  const reset = useCallback(() => {
    invalidate();
    setFeedback(idle());
  }, [invalidate]);
  return { feedback, save, refresh, reset, busy: feedback.phase === "saving" };
}
