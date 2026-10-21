/**
 * MCP 隔離設定的資料層（GET／PUT /v1/mcp/{name}/isolation）：PUT 一律帶 GET 拿到的
 * bound、只送這次改的欄位；422 代碼轉成翻譯，條目改過時重新讀 bound。
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/connections", () => ({
  fetchTouka: vi.fn(),
  getActiveProxyUrl: () => "http://x",
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { fetchTouka } from "@/lib/connections";
import { toast } from "sonner";
import { useMcpIsolation } from "../useMcpIsolation";

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;
const BOUND = "a".repeat(64);
const VIEW = {
  server: "files",
  class: "third_party",
  origin: "manual",
  record_state: "ok",
  sandbox_paths: [],
  bound: BOUND,
  can_mark_agent: true,
};

const reply = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status });
const refused = (message: string) =>
  reply(422, { detail: { code: "VALIDATION_ERROR", id: 1001, message } });

async function loaded(serverKey = "files") {
  const hook = renderHook(() => useMcpIsolation(serverKey));
  await waitFor(() => expect(hook.result.current.view).not.toBeNull());
  return hook;
}

function putBody(call = 1) {
  const [, path, init] = mockFetch.mock.calls[call];
  expect(init.method).toBe("PUT");
  return { path, body: JSON.parse(init.body as string) };
}

describe("useMcpIsolation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockReset();
    mockFetch.mockImplementation(async () => reply(200, VIEW));
  });

  it("reads the server's isolation settings with an encoded name", async () => {
    const { result } = await loaded("my server");
    expect(mockFetch).toHaveBeenCalledWith(
      "http://x",
      "/v1/mcp/my%20server/isolation",
    );
    expect(result.current.view?.bound).toBe(BOUND);
  });

  it("reads again when the saved entry changes", async () => {
    const { rerender } = renderHook(
      ({ revision }) => useMcpIsolation("files", revision),
      { initialProps: { revision: { command: "npx" } } },
    );
    await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(1));
    rerender({ revision: { command: "uvx" } });
    await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(2));
  });

  it("marks as agent-built with the read bound and nothing else", async () => {
    const { result } = await loaded();
    mockFetch.mockImplementation(async () =>
      reply(200, {
        ...VIEW,
        class: "self",
        origin: "agent",
        can_mark_agent: false,
      }),
    );
    let ok = false;
    await act(async () => {
      ok = await result.current.markAgent();
    });
    expect(ok).toBe(true);
    expect(putBody()).toEqual({
      path: "/v1/mcp/files/isolation",
      body: { bound: BOUND, origin: "agent" },
    });
    expect(result.current.view?.origin).toBe("agent");
    expect(toast.success).toHaveBeenCalledWith(
      expect.stringContaining("下次重新啟動"),
    );
  });

  it("saves the extra folders with the read bound", async () => {
    const { result } = await loaded();
    const paths = [{ path: "/srv/repo", access: "rw" as const }];
    mockFetch.mockImplementation(async () =>
      reply(200, { ...VIEW, sandbox_paths: paths }),
    );
    await act(async () => {
      await result.current.saveSandboxPaths(paths);
    });
    expect(putBody().body).toEqual({ bound: BOUND, sandbox_paths: paths });
    expect(result.current.view?.sandbox_paths).toEqual(paths);
    expect(result.current.error).toBeNull();
  });

  it("names the refused folder by its row", async () => {
    const { result } = await loaded();
    mockFetch.mockImplementation(async () =>
      refused("mcp_sandbox_path_refused:1:protected_location"),
    );
    let ok = true;
    await act(async () => {
      ok = await result.current.saveSandboxPaths([]);
    });
    expect(ok).toBe(false);
    expect(result.current.error).toMatch(/^第 2 筆：.*受保護/);
    expect(result.current.view?.bound).toBe(BOUND);
  });

  it("re-reads the bound when the entry changed and keeps the message", async () => {
    const { result } = await loaded();
    const fresh = "b".repeat(64);
    mockFetch
      .mockResolvedValueOnce(refused("mcp_entry_changed"))
      .mockResolvedValueOnce(reply(200, { ...VIEW, bound: fresh }));
    await act(async () => {
      await result.current.markAgent();
    });
    expect(mockFetch).toHaveBeenCalledTimes(3);
    expect(result.current.view?.bound).toBe(fresh);
    expect(result.current.error).toMatch(/剛被改過/);
  });

  it("explains a request the contract rejects (detail is a list)", async () => {
    const { result } = await loaded();
    mockFetch.mockImplementation(async () =>
      reply(422, { detail: [{ loc: ["body", "sandbox_paths", 0, "path"] }] }),
    );
    await act(async () => {
      await result.current.saveSandboxPaths([{ path: "repo", access: "ro" }]);
    });
    expect(result.current.error).toMatch(/完整路徑/);
    expect(result.current.error).not.toContain("loc");
  });

  it("shows the error when the settings can't be read", async () => {
    mockFetch.mockImplementation(async () => refused("mcp_server_not_found"));
    const { result } = renderHook(() => useMcpIsolation("gone"));
    await waitFor(() => expect(result.current.error).toMatch(/找不到/));
    expect(result.current.view).toBeNull();
  });
});
