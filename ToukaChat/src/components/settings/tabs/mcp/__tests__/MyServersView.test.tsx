import { beforeEach, describe, it, expect, vi } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";
import i18n from "@/lib/i18n";

const confirmToast = vi.fn();
vi.mock("@/hooks/useConfirmToast", () => ({
  useConfirmToast: () => ({ confirmToast }),
}));

vi.mock("../McpServerForm", () => ({ McpServerForm: () => null }));
vi.mock("../McpIsolationSection", () => ({
  McpIsolationSection: ({ serverKey }: { serverKey: string }) => (
    <div>isolation-{serverKey}</div>
  ),
}));

import { MyServersView } from "../MyServersView";
import type { McpServerStatus } from "@/lib/types";
import { fetchTouka } from "@/lib/connections";

vi.mock("@/lib/connections", () => ({
  fetchTouka: vi.fn(),
  getActiveProxyUrl: () => "http://x",
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchTouka).mockReset();
});

describe("MyServersView 手動啟動確認", () => {
  const bound = "b".repeat(64);
  const view = {
    server: "files",
    class: "self",
    origin: "agent",
    record_state: "ok",
    bound,
    sandbox_paths: [],
    can_mark_agent: false,
  };
  const isolation: NonNullable<McpServerStatus["isolation"]> = {
    class: "self",
    origin: "agent",
    record_state: "ok",
    state: "waiting_isolation",
    level: "none",
    reason: "executor_unreachable",
    features: [],
    manual_start: {
      fallback: "executor",
      features: ["fs", "signal"],
      generation: 7,
      bound,
    },
  };
  const props = (change: Partial<typeof isolation> = {}) => ({
    mcpServers: [
      {
        name: "files",
        status: "not_started" as const,
        url: "",
        tools_count: 0,
        generation: 7,
        isolation: { ...isolation, ...change },
      },
    ],
    mcpConfig: { files: { command: "x", args: [], enabled: true } },
    saveMcpConfig: vi.fn(async () => true),
    restartMcpServer: vi.fn(async () => true),
    restartingKeys: new Set<string>(),
    isSystemRefreshing: false,
    onRefreshList: vi.fn(),
    editingMcpKey: null,
    setEditingMcpKey: vi.fn(),
    fetchServerTools: vi.fn(async () => null),
    fetchServerResources: vi.fn(async () => null),
    fetchServerResourceTemplates: vi.fn(async () => null),
    readServerResource: vi.fn(async () => null),
    fetchServerPrompts: vi.fn(async () => null),
    getServerPrompt: vi.fn(async () => null),
  });
  const open = () => fireEvent.click(screen.getByRole("button", { name: "啟動…" }));

  it("啟動入口常駐徽章旁，按下才開確認框", async () => {
    vi.mocked(fetchTouka).mockResolvedValue(new Response(JSON.stringify(view)));
    const p = props();
    render(<MyServersView {...p} />);
    const start = screen.getByRole("button", { name: "啟動…" });
    expect(start).toBeVisible();
    expect(start.closest('.opacity-0')).toBeNull();
    expect(start.parentElement).toHaveTextContent("等待隔離");
    expect(screen.queryByRole("dialog")).toBeNull();
    await act(async () => fireEvent.click(start));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(p.restartMcpServer).not.toHaveBeenCalled();
  });

  it("重啟中不能再次按啟動", () => {
    const p = props();
    p.restartingKeys.add("files");
    render(<MyServersView {...p} />);
    const start = screen.getByRole("button", { name: "啟動…" });
    expect(start).toBeDisabled();
    fireEvent.click(start);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(p.restartMcpServer).not.toHaveBeenCalled();
  });

  it.each([
    ["executor", /在執行器容器裡/],
    ["landlock", /在 Touka 容器內.*Landlock/],
    ["direct", /這是代理人寫的程式.*Touka 自己的權限.*全部金鑰/],
  ] as const)(
    "%s 顯示去處與能力，確認後才送完整同意",
    async (fallback, wording) => {
      vi.mocked(fetchTouka).mockResolvedValue(
        new Response(JSON.stringify(view)),
      );
      const plan = {
        ...isolation.manual_start!,
        fallback,
        features: fallback === "direct" ? [] : ["fs", "signal"],
      };
      const p = props({ manual_start: plan });
      render(<MyServersView {...p} />);
      open();
      expect(screen.getByRole("dialog")).toHaveTextContent(wording);
      expect(screen.getByRole("dialog")).toHaveTextContent("這次網路埠沒有擋住。");
      expect(p.restartMcpServer).not.toHaveBeenCalled();
      expect(confirmToast).not.toHaveBeenCalled();
      const confirm = screen.getByRole("button", { name: "同意這次啟動" });
      await waitFor(() => expect(confirm).toBeEnabled());
      if (fallback === "direct")
        expect(screen.getByRole("dialog")).toHaveTextContent("沒有隔離保護");
      else
        expect(screen.getByRole("dialog")).toHaveTextContent(
          /檔案存取限制.*行程信號限制/,
        );
      fireEvent.click(confirm);
      await waitFor(() =>
        expect(p.restartMcpServer).toHaveBeenCalledExactlyOnceWith("files", {
          isolation_ack: fallback,
          features: plan.features,
          generation: 7,
          bound,
        }),
      );
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    },
  );

  it("有 TCP 保護時不顯示埠未封鎖警告", async () => {
    vi.mocked(fetchTouka).mockResolvedValue(new Response(JSON.stringify(view)));
    const p = props({ manual_start: { ...isolation.manual_start!, features: ["fs", "tcp"] } });
    render(<MyServersView {...p} />);
    await act(async () => open());
    expect(screen.getByRole("dialog")).toHaveTextContent("TCP 埠限制");
    expect(screen.getByRole("dialog")).not.toHaveTextContent("這次網路埠沒有擋住");
  });

  it("英文入口、恢復說明與未隔離風險一致", async () => {
    await i18n.changeLanguage("en");
    try {
      vi.mocked(fetchTouka).mockResolvedValue(new Response(JSON.stringify(view)));
      const p = props({ manual_start: { ...isolation.manual_start!, fallback: "direct", features: [] } });
      render(<MyServersView {...p} />);
      expect(screen.getByText(/starts automatically when isolation is restored/)).toBeInTheDocument();
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Start…" })));
      expect(screen.getByRole("dialog")).toHaveTextContent(/This is code the agent wrote.*Touka’s own permissions/);
      expect(screen.getByRole("dialog")).toHaveTextContent("Network ports are not blocked this time.");
    } finally {
      await act(async () => { await i18n.changeLanguage("zh-TW"); });
    }
  });

  it.each([
    { ...view, bound: "c".repeat(64) },
    { ...view, class: "third_party" },
  ])("最新來源或 bound 不符時不能沿用清單上的同意", async (latest) => {
    vi.mocked(fetchTouka).mockResolvedValue(
      new Response(JSON.stringify(latest)),
    );
    const p = props();
    render(<MyServersView {...p} />);
    open();
    expect(await screen.findByRole("alert")).toHaveTextContent(/設定剛被改過/);
    expect(screen.getByRole("button", { name: "同意這次啟動" })).toBeDisabled();
    expect(p.restartMcpServer).not.toHaveBeenCalled();
  });

  it("讀取未完成、失敗與取消都不啟動，重新打開會重新讀取", async () => {
    vi.mocked(fetchTouka).mockImplementation(() => new Promise(() => {}));
    const p = props();
    render(<MyServersView {...p} />);
    open();
    expect(screen.getByRole("button", { name: "同意這次啟動" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    vi.mocked(fetchTouka).mockRejectedValue(new Error("offline"));
    open();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "同意這次啟動" })).toBeDisabled();
    expect(p.restartMcpServer).not.toHaveBeenCalled();
    expect(fetchTouka).toHaveBeenCalledTimes(2);
  });

  it("不認得的保護能力不能核准", async () => {
    vi.mocked(fetchTouka).mockResolvedValue(new Response(JSON.stringify(view)));
    const p = props({
      manual_start: {
        ...isolation.manual_start!,
        features: ["future_feature"],
      },
    });
    render(<MyServersView {...p} />);
    await act(async () => open());
    expect(screen.getByRole("dialog")).toHaveTextContent("無法辨識的保護能力");
    expect(screen.getByRole("button", { name: "同意這次啟動" })).toBeDisabled();
    expect(p.restartMcpServer).not.toHaveBeenCalled();
  });

  it("送出期間不能重複，拒絕後關閉，重新開啟使用刷新後的去處", async () => {
    vi.mocked(fetchTouka).mockImplementation(
      async () => new Response(JSON.stringify(view)),
    );
    let finish!: (value: boolean) => void;
    const p = props();
    p.restartMcpServer.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const mounted = render(<MyServersView {...p} />);
    open();
    const confirm = screen.getByRole("button", { name: "同意這次啟動" });
    await waitFor(() => expect(confirm).toBeEnabled());
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    expect(p.restartMcpServer).toHaveBeenCalledTimes(1);
    expect(confirm).toBeDisabled();
    mounted.rerender(
      <MyServersView
        {...p}
        mcpServers={
          props({
            manual_start: {
              ...isolation.manual_start!,
              fallback: "direct",
              features: [],
            },
          }).mcpServers
        }
      />,
    );
    finish(false);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    open();
    expect(screen.getByRole("dialog")).toHaveTextContent(/Touka 自己的權限/);
  });

  it.each([
    { class: "third_party" as const },
    { class: "remote" as const },
    { state: "suspended" as const },
    { state: "running" as const },
    { manual_start: undefined },
  ])("非自建等待狀態沒有手動降級入口：%s", (change) => {
    const p = props(change);
    render(<MyServersView {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /重啟 MCP 伺服器 files/ }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(confirmToast).toHaveBeenCalledTimes(1);
    expect(fetchTouka).not.toHaveBeenCalled();
    expect(p.restartMcpServer).not.toHaveBeenCalled();
  });
});

describe("MyServersView 刪除二次確認", () => {
  it("刪除前先呼叫 confirmToast，不直接 saveMcpConfig", () => {
    const saveMcpConfig = vi.fn(() => Promise.resolve(true));
    render(
      <MyServersView
        mcpServers={[]}
        mcpConfig={{ "srv-a": { command: "npx", args: [] } }}
        saveMcpConfig={saveMcpConfig}
        restartMcpServer={vi.fn(() => Promise.resolve(true))}
        restartingKeys={new Set()}
        isSystemRefreshing={false}
        onRefreshList={vi.fn()}
        editingMcpKey={null}
        setEditingMcpKey={vi.fn()}
        fetchServerTools={vi.fn(() => Promise.resolve(null))}
        fetchServerResources={vi.fn(() => Promise.resolve(null))}
        fetchServerResourceTemplates={vi.fn(() => Promise.resolve(null))}
        readServerResource={vi.fn(() => Promise.resolve(null))}
        fetchServerPrompts={vi.fn(() => Promise.resolve(null))}
        getServerPrompt={vi.fn(() => Promise.resolve(null))}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /刪除 MCP 伺服器 srv-a/ }));
    expect(confirmToast).toHaveBeenCalledTimes(1);
    expect(confirmToast.mock.calls[0][0]).toContain("srv-a");
    expect(saveMcpConfig).not.toHaveBeenCalled(); // 只有確認後才會刪
  });
});

describe("MyServersView 隔離設定區塊", () => {
  const renderEditing = (editingMcpKey: string | null) =>
    render(
      <MyServersView
        mcpServers={[]}
        mcpConfig={{ "srv-a": { command: "npx", args: [] } }}
        saveMcpConfig={vi.fn(() => Promise.resolve(true))}
        restartMcpServer={vi.fn(() => Promise.resolve(true))}
        restartingKeys={new Set()}
        isSystemRefreshing={false}
        onRefreshList={vi.fn()}
        editingMcpKey={editingMcpKey}
        setEditingMcpKey={vi.fn()}
        fetchServerTools={vi.fn(() => Promise.resolve(null))}
        fetchServerResources={vi.fn(() => Promise.resolve(null))}
        fetchServerResourceTemplates={vi.fn(() => Promise.resolve(null))}
        readServerResource={vi.fn(() => Promise.resolve(null))}
        fetchServerPrompts={vi.fn(() => Promise.resolve(null))}
        getServerPrompt={vi.fn(() => Promise.resolve(null))}
      />,
    );

  it("編輯已存的伺服器時顯示它的來源與額外允許的資料夾", () => {
    renderEditing("srv-a");
    expect(screen.getByText("isolation-srv-a")).toBeInTheDocument();
  });

  it("新增中（沒有已存的條目）不顯示", () => {
    renderEditing(null);
    expect(screen.queryByText(/^isolation-/)).toBeNull();
  });
});
