/**
 * 用真的 fetchTouka、只假造底層 fetch，核對確認安裝失敗時拿到的錯誤碼。
 *
 * /terminal 只掛根路徑（main.py 的 APP_ROUTERS），fetchTouka 遇 404 會改打 /v1，
 * 拿回的是通用 404，後端原本給的 TERMINAL_APPROVAL_NOT_FOUND 看不到。整個換掉
 * fetchTouka 的單元測試看不到這條重打，所以這裡走真的傳輸層。
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEvolution } from "../useEvolution";
import { clearAccessToken, setAccessToken } from "@/lib/connections";
import { getErrorCode } from "@/lib/error_handler";
import { getSystemEpoch } from "@/lib/time_utils";

const proxyUrl = "http://synthetic.invalid";

async function confirmAndCatch(): Promise<unknown> {
  const { result } = renderHook(() => useEvolution());
  let caught: unknown;
  await act(async () => {
    caught = await result.current.confirmInstall("install_x", {}).then(
      () => undefined,
      (err: unknown) => err,
    );
  });
  return caught;
}

describe("useEvolution.confirmInstall 走真的 fetchTouka", () => {
  beforeEach(() => {
    clearAccessToken();
    window.localStorage.setItem(
      "touka_connections",
      JSON.stringify([{ id: "proxy", provider: "proxy", enabled: true, url: proxyUrl }]),
    );
    setAccessToken(`x.${btoa(JSON.stringify({ exp: getSystemEpoch() / 1000 + 3600 }))}.x`);
  });
  afterEach(() => {
    clearAccessToken();
    vi.unstubAllGlobals();
    window.localStorage.removeItem("touka_connections");
  });

  it("申請過期：根路徑 404 帶錯誤碼、改打 /v1 拿回通用 404 → 仍是 TERMINAL_APPROVAL_NOT_FOUND", async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        urls.push(url);
        const detail = url.includes("/v1/") ? "Not Found" : { code: "TERMINAL_APPROVAL_NOT_FOUND" };
        return Promise.resolve(Response.json({ detail }, { status: 404 }));
      }),
    );

    const caught = await confirmAndCatch();

    expect(urls).toEqual([
      `${proxyUrl}/terminal/approve/install_x`,
      `${proxyUrl}/v1/terminal/approve/install_x`,
    ]);
    expect(getErrorCode(caught)).toBe("TERMINAL_APPROVAL_NOT_FOUND");
  });

  it("不是管理員：403 不會重打，錯誤碼照後端給的", async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        urls.push(url);
        return Promise.resolve(Response.json({ detail: { code: "AUTH_ADMIN_REQUIRED" } }, { status: 403 }));
      }),
    );

    const caught = await confirmAndCatch();

    expect(urls).toEqual([`${proxyUrl}/terminal/approve/install_x`]);
    expect(getErrorCode(caught)).toBe("AUTH_ADMIN_REQUIRED");
  });
});
