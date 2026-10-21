import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSoulSave } from "../useSoulSave";
import { clearAccessToken, setAccessToken } from "@/lib/connections";
import { getSystemEpoch } from "@/lib/time_utils";

const input = {
  persona: "synthetic draft",
  config: { temperature: 0.7, top_p: 1, top_k: 40, is_toukasoul: true },
  admin_password: "synthetic password",
};
const proxyUrl = "http://synthetic.invalid";

function configure(requestTimeoutMs = 0) {
  window.localStorage.setItem(
    "touka_connections",
    JSON.stringify([
      {
        id: "proxy",
        provider: "proxy",
        enabled: true,
        url: proxyUrl,
        requestTimeoutMs,
      },
    ]),
  );
}

function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("useSoulSave actual transport boundary", () => {
  beforeEach(() => {
    clearAccessToken();
    configure();
    vi.useFakeTimers();
  });
  afterEach(() => {
    clearAccessToken();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("auth preflight exceeds UI deadline: later refresh cannot send an aborted POST", async () => {
    const refresh = pending<Response>();
    const sent: string[] = [];
    setAccessToken(`x.${btoa(JSON.stringify({ exp: 0 }))}.x`);
    const transport = vi.fn((url: string, options: RequestInit = {}) => {
      if (url.endsWith("/auth/refresh")) return refresh.promise;
      // Browser fetch rejects an already aborted signal before network send.
      if (options.signal?.aborted) return Promise.reject(options.signal.reason);
      sent.push(url);
      return Promise.resolve(
        Response.json({ status: "success", soul_id: "MySoul" }),
      );
    });
    vi.stubGlobal("fetch", transport);
    const { result } = renderHook(() =>
      useSoulSave({ proxyUrl, soulId: "MySoul", readback: vi.fn() }),
    );
    let save!: Promise<string>;
    act(() => {
      save = result.current.save(input);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_001);
    });
    expect(await save).toBe("unknown");
    expect(result.current.busy).toBe(false);
    await act(async () => {
      refresh.resolve(
        Response.json({
          access_token: `x.${btoa(JSON.stringify({ exp: getSystemEpoch() / 1000 + 3600 }))}.x`,
        }),
      );
    });
    expect(transport).toHaveBeenCalledTimes(2);
    expect(sent).toEqual([]);
    expect(result.current.feedback.phase).toBe("unknown");
  });

  it("honours a shorter configured deadline and does not resend after transport failure", async () => {
    configure(75);
    const transport = vi.fn(() => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", transport);
    const { result } = renderHook(() =>
      useSoulSave({ proxyUrl, soulId: "MySoul", readback: vi.fn() }),
    );
    act(() => {
      void result.current.save(input);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(76);
    });
    expect(result.current.feedback.phase).toBe("unknown");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(transport).toHaveBeenCalledTimes(1);
  });

  it.each(["soul", "proxy", "unmount"] as const)(
    "invalidates pending save on %s without accepting its late response",
    async (kind) => {
      const response = pending<Response>();
      const transport = vi.fn(() => response.promise);
      vi.stubGlobal("fetch", transport);
      const readback = vi.fn();
      const { result, rerender, unmount } = renderHook(
        (props) => useSoulSave({ ...props, readback }),
        { initialProps: { proxyUrl, soulId: "MySoul" } },
      );
      let save!: Promise<string>;
      act(() => {
        save = result.current.save(input);
      });
      const signal = (
        transport.mock.calls[0] as unknown as [string, RequestInit]
      )[1].signal!;
      if (kind === "unmount") unmount();
      else
        rerender({
          proxyUrl: kind === "proxy" ? "http://other.invalid" : proxyUrl,
          soulId: kind === "soul" ? "OtherSoul" : "MySoul",
        });
      expect(signal.aborted).toBe(true);
      await act(async () => {
        response.resolve(
          Response.json({ status: "success", soul_id: "MySoul" }),
        );
      });
      expect(await save).toBe("stale");
      expect(readback).not.toHaveBeenCalled();
    },
  );

  it.each(["false", "throw", "timeout"] as const)(
    "confirmed save remains confirmed when readback returns %s",
    async (mode) => {
      const transport = vi.fn(() =>
        Promise.resolve(
          Response.json({ status: "success", soul_id: "MySoul" }),
        ),
      );
      vi.stubGlobal("fetch", transport);
      const readback = vi.fn(() =>
        mode === "false"
          ? Promise.resolve(false)
          : mode === "throw"
            ? Promise.reject(new Error("synthetic read failure"))
            : new Promise<boolean>(() => {}),
      );
      const { result } = renderHook(() =>
        useSoulSave({ proxyUrl, soulId: "MySoul", readback }),
      );
      await act(async () => {
        await result.current.save(input);
      });
      act(() => {
        void result.current.refresh();
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_001);
      });
      expect(result.current.feedback).toMatchObject({
        phase: "saved",
        refreshing: false,
        refreshFailed: true,
      });
      expect(transport).toHaveBeenCalledTimes(1);
    },
  );

  it("reset invalidates late readback and its current predicate", async () => {
    const response = pending<boolean>();
    const readback = vi.fn(() => response.promise);
    const { result } = renderHook(() =>
      useSoulSave({ proxyUrl, soulId: "MySoul", readback }),
    );
    act(() => {
      void result.current.refresh();
    });
    const current = (
      readback.mock.calls[0] as unknown as [AbortSignal, () => boolean]
    )[1];
    act(() => {
      result.current.reset();
    });
    expect(current()).toBe(false);
    await act(async () => {
      response.resolve(true);
    });
    expect(result.current.feedback).toMatchObject({
      phase: "idle",
      refreshing: false,
      refreshFailed: false,
    });
  });
});
