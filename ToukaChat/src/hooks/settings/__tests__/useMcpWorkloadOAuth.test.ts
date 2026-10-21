import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/connections", () => ({
  fetchTouka: vi.fn(),
  getActiveProxyUrl: () => "http://proxy",
}));
vi.mock("@/lib/error_handler", () => ({ logSSOTError: vi.fn() }));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import { fetchTouka } from "@/lib/connections";
import { useMcpWorkloadOAuth } from "../useMcpWorkloadOAuth";

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;

describe("useMcpWorkloadOAuth", () => {
  beforeEach(() => vi.clearAllMocks());

  it("credential secret 只出現在單次 PUT body，不進 hook state", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        profile_id: "profile-a",
        credential_id: "credential-a",
        generation: 2,
        status: "active",
      }),
    });
    const { result } = renderHook(() => useMcpWorkloadOAuth("Alice"));

    await act(async () => {
      await result.current.setCredential(
        "profile-a",
        "client-canary",
        "secret-canary",
      );
    });

    const [, path, options] = mockFetch.mock.calls[0];
    expect(path).toBe(
      "/v1/admin/mcp/oauth/workload/profiles/profile-a/credential",
    );
    expect(JSON.parse(options.body)).toEqual({
      client_id: "client-canary",
      client_secret: "secret-canary",
    });
    expect(JSON.stringify(result.current.credentialStatuses)).not.toContain(
      "canary",
    );
    expect(result.current.credentialStatuses["profile-a"]).toEqual({
      profile_id: "profile-a",
      credential_id: "credential-a",
      generation: 2,
      status: "active",
    });
  });

  it("owner delegation payload 不接受 source grant 識別字且路徑逐段編碼", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ delegation_id: "delegation/a", status: "active" }),
    });
    const { result } = renderHook(() => useMcpWorkloadOAuth("Alice/A"));

    await act(async () => {
      await result.current.putDelegation("delegation/a", {
        installation_id: "calendar",
        installation_generation: 4,
        raw_tool_name: "calendar.read",
        required_scopes: ["calendar.read"],
        expires_at: 200,
      });
    });

    const [, path, options] = mockFetch.mock.calls[0];
    expect(path).toBe(
      "/v1/mcp/agents/Alice%2FA/oauth/delegations/delegation%2Fa",
    );
    expect(JSON.parse(options.body)).toEqual({
      installation_id: "calendar",
      installation_generation: 4,
      raw_tool_name: "calendar.read",
      required_scopes: ["calendar.read"],
      expires_at: 200,
    });
  });

  it("recovery 只經明示 POST retry 並以回應覆蓋 blocking state", async () => {
    mockFetch.mockResolvedValue({ ok: true, json: async () => [] });
    const { result } = renderHook(() => useMcpWorkloadOAuth("Alice"));

    let completed = false;
    await act(async () => {
      completed = await result.current.retryRecovery();
    });

    const [, path, options] = mockFetch.mock.calls[0];
    expect(path).toBe("/v1/admin/mcp/oauth/workload/recovery/retry");
    expect(options).toEqual({ method: "POST" });
    expect(completed).toBe(true);
    expect(result.current.recovery).toEqual([]);
  });
});
