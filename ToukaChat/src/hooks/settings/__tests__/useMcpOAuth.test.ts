import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/connections", () => ({
  fetchTouka: vi.fn(),
  getActiveProxyUrl: () => "https://proxy.example.test",
}));

import { fetchTouka } from "@/lib/connections";
import { useMcpOAuth } from "../useMcpOAuth";

const mockFetch = vi.mocked(fetchTouka);

function jsonResponse(payload: unknown, ok = true): Response {
  return {
    ok,
    json: vi.fn().mockResolvedValue(payload),
  } as unknown as Response;
}

describe("useMcpOAuth popup 與 polling 邊界", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("只在 oauth_user 啟用時讀取 exact server status", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse({
        status: "auth_required",
        operation_id: null,
        expires_at: null,
        authorization_servers: [],
      }),
    );

    const { result } = renderHook(() => useMcpOAuth("remote/a", true));

    await waitFor(() => expect(result.current.status).toBe("auth_required"));
    expect(mockFetch).toHaveBeenCalledWith(
      "https://proxy.example.test",
      "/v1/mcp/servers/remote%2Fa/oauth/status",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("exact origin、source、shape 與 operation 才能啟動 polling", async () => {
    const popup = {
      close: vi.fn(),
      closed: false,
    } as unknown as Window;
    vi.spyOn(window, "open").mockReturnValue(popup);
    mockFetch
      .mockResolvedValueOnce(
        jsonResponse({
          status: "auth_required",
          operation_id: null,
          expires_at: null,
          authorization_servers: [],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          operation_id: "operation-safe",
          authorization_url: "https://issuer.example/authorize?opaque=yes",
          expires_at: 1234,
          reused: false,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          status: "connected",
          operation_id: null,
          expires_at: 5678,
          authorization_servers: [],
        }),
      );

    const { result } = renderHook(() => useMcpOAuth("remote-a", true));
    await waitFor(() => expect(result.current.status).toBe("auth_required"));
    await act(async () => {
      await result.current.connect();
    });

    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: "https://attacker.example",
          source: popup,
          data: {
            type: "touka:mcp-oauth",
            operationId: "operation-safe",
            status: "connected",
          },
        }),
      );
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: "https://proxy.example.test",
          source: window,
          data: {
            type: "touka:mcp-oauth",
            operationId: "operation-safe",
            status: "connected",
          },
        }),
      );
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: "https://proxy.example.test",
          source: popup,
          data: {
            type: "touka:mcp-oauth",
            operationId: "wrong-operation",
            status: "connected",
          },
        }),
      );
    });
    expect(mockFetch).toHaveBeenCalledTimes(2);

    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: "https://proxy.example.test",
          source: popup,
          data: {
            type: "touka:mcp-oauth",
            operationId: "operation-safe",
            status: "connected",
          },
        }),
      );
    });

    await waitFor(() => expect(result.current.status).toBe("connected"));
    expect(mockFetch).toHaveBeenCalledTimes(3);
    expect(popup.close).toHaveBeenCalledTimes(1);
  });

  it("popup 被阻擋時 fail-loud，且不把 opaque operation 寫入 storage", async () => {
    vi.spyOn(window, "open").mockReturnValue(null);
    const localSet = vi.spyOn(Storage.prototype, "setItem");
    mockFetch
      .mockResolvedValueOnce(
        jsonResponse({
          status: "auth_required",
          operation_id: null,
          expires_at: null,
          authorization_servers: [],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          operation_id: "operation-safe",
          authorization_url: "https://issuer.example/authorize?opaque=yes",
          expires_at: 1234,
          reused: false,
        }),
      );

    const { result } = renderHook(() => useMcpOAuth("remote-a", true));
    await waitFor(() => expect(result.current.status).toBe("auth_required"));
    await act(async () => {
      await result.current.connect();
    });

    expect(result.current.error).toBe("popup_blocked");
    expect(localSet).not.toHaveBeenCalled();
  });

  it("重複 connect 會關閉前一個 popup 並使舊 message 失效", async () => {
    const firstPopup = { close: vi.fn(), closed: false } as unknown as Window;
    const secondPopup = { close: vi.fn(), closed: false } as unknown as Window;
    vi.spyOn(window, "open")
      .mockReturnValueOnce(firstPopup)
      .mockReturnValueOnce(secondPopup);
    mockFetch
      .mockResolvedValueOnce(
        jsonResponse({
          status: "auth_required",
          operation_id: null,
          expires_at: null,
          authorization_servers: [],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          operation_id: "operation-one",
          authorization_url: "https://issuer.example/authorize?one",
          expires_at: 1234,
          reused: false,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          operation_id: "operation-two",
          authorization_url: "https://issuer.example/authorize?two",
          expires_at: 1235,
          reused: false,
        }),
      );

    const { result } = renderHook(() => useMcpOAuth("remote-a", true));
    await waitFor(() => expect(result.current.status).toBe("auth_required"));
    await act(async () => {
      await result.current.connect();
      await result.current.connect();
    });

    expect(firstPopup.close).toHaveBeenCalledTimes(1);
    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: "https://proxy.example.test",
          source: firstPopup,
          data: {
            type: "touka:mcp-oauth",
            operationId: "operation-one",
            status: "connected",
          },
        }),
      );
    });
    expect(mockFetch).toHaveBeenCalledTimes(3);
  });
});
