import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import i18n from "@/lib/i18n";
import { McpTab } from "../McpTab";

// Radix Tabs 在 jsdom 不支援 fireEvent.click 切換，替換為原生 state-based 實作
vi.mock("@/components/ui/tabs", async () => {
  const { createElement, useState, createContext, useContext } =
    await import("react");
  const TabsCtx = createContext({ value: "", setValue: (_: string) => {} });

  const Tabs = ({
    children,
    defaultValue,
  }: {
    children: React.ReactNode;
    defaultValue?: string;
  }) => {
    const [value, setValue] = useState(defaultValue ?? "");
    return createElement(
      TabsCtx.Provider,
      { value: { value, setValue } },
      children,
    );
  };

  const TabsList = ({
    children,
    className,
  }: {
    children: React.ReactNode;
    className?: string;
  }) => createElement("div", { role: "tablist", className }, children);

  const TabsTrigger = ({
    children,
    value,
    className,
  }: {
    children: React.ReactNode;
    value: string;
    className?: string;
  }) => {
    const ctx = useContext(TabsCtx);
    return createElement(
      "button",
      {
        role: "tab",
        "aria-selected": ctx.value === value,
        className,
        onClick: () => ctx.setValue(value),
      },
      children,
    );
  };

  const TabsContent = ({
    children,
    value,
    className,
  }: {
    children: React.ReactNode;
    value: string;
    className?: string;
  }) => {
    const ctx = useContext(TabsCtx);
    return ctx.value === value
      ? createElement("div", { role: "tabpanel", className }, children)
      : null;
  };

  return { Tabs, TabsList, TabsTrigger, TabsContent };
});

// handleToggle 以淺拷貝 + 就地改寫 server.enabled，會污染共享的 mcpConfig 物件；
// 在 shuffle 次序下「切換」測試可能先跑 → 污染「列表渲染」測試。每個 case 還原乾淨副本。
const BASE_CONFIG = {
  memory: { command: "npx", args: ["server-memory"], enabled: true },
  google_search: { command: "python", args: ["search.py"], enabled: true },
  disabled_server: { command: "node", args: ["test.js"], enabled: false },
};

const mockProps = {
  mcpServers: [
    {
      name: "memory",
      tools_count: 5,
      status: "connected" as const,
      url: "http://localhost:3001",
    },
    {
      name: "google_search",
      tools_count: 3,
      status: "connected" as const,
      url: "http://localhost:3002",
    },
  ],
  mcpConfig: structuredClone(BASE_CONFIG),
  mcpHubServers: [
    {
      id: "fetch",
      name: "Fetch",
      description: "Fetch web pages",
      category: "Browser",
      icon: "Globe",
    },
  ],
  saveMcpConfig: vi.fn(() => Promise.resolve(true)),
  restartMcpServer: vi.fn(() => Promise.resolve(true)),
  restartingKeys: new Set<string>(),
  syncMcpHub: vi.fn(() => Promise.resolve(true)),
  setSelectedHubServer: vi.fn(),
  isSystemRefreshing: false,
  onRefreshList: vi.fn(),
  // ADR-0195：工具治理面板的資料源；預設回 null（抓取失敗態），本檔不驗該面板。
  fetchServerTools: vi.fn(() => Promise.resolve(null)),
  fetchServerResources: vi.fn(() => Promise.resolve(null)),
  fetchServerResourceTemplates: vi.fn(() => Promise.resolve(null)),
  readServerResource: vi.fn(() => Promise.resolve(null)),
  fetchServerPrompts: vi.fn(() => Promise.resolve(null)),
  getServerPrompt: vi.fn(() => Promise.resolve(null)),
};

describe("McpTab (Baseline)", () => {
  // 本檔斷言中文輸出（如「5 個工具」）；i18n 為跨檔共享單例且測試次序隨機，故明確固定 zh-TW。
  // 並還原 mcpConfig 乾淨副本（handleToggle 會就地污染共享物件）。
  beforeEach(async () => {
    vi.clearAllMocks();
    await i18n.changeLanguage("zh-TW");
    mockProps.mcpConfig = structuredClone(BASE_CONFIG);
  });

  it("renders existing MCP servers in list", () => {
    render(<McpTab {...mockProps} />);
    expect(screen.getAllByText(/memory/i)).not.toHaveLength(0);
    expect(screen.getAllByText(/google_search/i)).not.toHaveLength(0);
    expect(screen.getAllByText(/5 個工具/)).not.toHaveLength(0);
  });

  it("triggers toggle enabled state", () => {
    render(<McpTab {...mockProps} />);
    // Find toggle buttons (labeled "停用" or "啟用")
    const toggleBtn = screen.getAllByText(/停用/i)[0];
    fireEvent.click(toggleBtn);
    expect(mockProps.saveMcpConfig).toHaveBeenCalled();
  });

  it("fills form when clicking edit", () => {
    render(<McpTab {...mockProps} />);
    const editBtns = screen
      .getAllByRole("button")
      .filter((b) => b.querySelector("svg.lucide-settings2"));
    if (editBtns[0]) {
      fireEvent.click(editBtns[0]);
      // Check if input value changed
      const nameInput = screen.getByPlaceholderText(/例如: memory/i);
      expect((nameInput as HTMLInputElement).value).toBe("memory");
    }
  });

  it("can switch to Hub tab", () => {
    render(<McpTab {...mockProps} />);
    const hubTabBtn = screen.getByText(/探索中心/i);
    fireEvent.click(hubTabBtn);
    expect(screen.getByText(/推薦的官方與精選工具/i)).toBeDefined();
    expect(screen.getAllByText(/Fetch/i)).not.toHaveLength(0);
  });

  it("triggers syncMcpHub when clicking sync button in Hub", () => {
    render(<McpTab {...mockProps} />);
    fireEvent.click(screen.getByText(/探索中心/i));
    // Sync button is a icon button with RefreshCw
    const syncBtn = screen
      .getAllByRole("button")
      .find((b) => b.querySelector("svg.lucide-refresh-cw"));
    if (syncBtn) {
      fireEvent.click(syncBtn);
      expect(mockProps.syncMcpHub).toHaveBeenCalled();
    }
  });
});
