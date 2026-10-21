import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { McpToolPolicy } from "../McpToolPolicy";
import type { McpServerToolsView } from "@/lib/mcp_version";

const CFG = {
  command: "npx",
  args: ["-y", "@stripe/mcp@0.3.3"],
  enabled: true,
};

function view(over: Partial<McpServerToolsView> = {}): McpServerToolsView {
  return {
    server: "stripe",
    source_protocol_version: "2026-07-28",
    supported: true,
    tools: [
      {
        source_protocol_version: "2026-07-28",
        name: "list_customers",
        description: "read",
        enabled: true,
      },
      {
        source_protocol_version: "2026-07-28",
        name: "create_refund",
        description: "moves money",
        enabled: false,
      },
    ],
    enabled_tools: ["list_customers"],
    disabled_tools: [],
    ...over,
  };
}

function renderPanel(data: McpServerToolsView | null, onSave = vi.fn()) {
  const fetchServerTools = vi.fn().mockResolvedValue(data);
  render(
    <McpToolPolicy
      serverKey="stripe"
      serverCfg={CFG}
      fetchServerTools={fetchServerTools}
      onSave={onSave}
      onClose={vi.fn()}
    />,
  );
  return { onSave, fetchServerTools };
}

describe("McpToolPolicy（ADR-0195 軸 A 治理面板）", () => {
  beforeEach(() => vi.clearAllMocks());

  it("列出**全部**工具，被關掉的仍在清單裡（只是未勾選）", async () => {
    // 端點存在的理由：複用廣告面的 get_all_tools 會讓取消勾選的工具直接消失、
    // 使用者再也勾不回來。
    renderPanel(view());
    await waitFor(() => expect(screen.getByText("create_refund")).toBeTruthy());
    expect(screen.getByText("list_customers")).toBeTruthy();
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes[0].getAttribute("data-state")).toBe("checked");
    expect(boxes[1].getAttribute("data-state")).toBe("unchecked");
  });

  it('全部勾選時送出 ["*"]，而非逐項列舉', async () => {
    // 逐項列舉會讓上游日後新增的工具預設關閉；"*" 才等同「不設限」。
    const { onSave } = renderPanel(view());
    await waitFor(() => expect(screen.getByText("create_refund")).toBeTruthy());
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: /儲存|Save/ }));
    expect(onSave).toHaveBeenCalledWith(["*"]);
  });

  it("取消勾選後送出剩餘工具的明確清單", async () => {
    const { onSave } = renderPanel(view());
    await waitFor(() =>
      expect(screen.getByText("list_customers")).toBeTruthy(),
    );
    fireEvent.click(screen.getAllByRole("checkbox")[0]); // 取消 list_customers
    fireEvent.click(screen.getByRole("button", { name: /儲存|Save/ }));
    expect(onSave).toHaveBeenCalledWith([]);
  });

  it("被 disabled_tools 硬性封鎖的工具無法在此勾選", async () => {
    renderPanel(view({ disabled_tools: ["create_refund"] }));
    await waitFor(() => expect(screen.getByText("create_refund")).toBeTruthy());
    expect(screen.getAllByRole("checkbox")[1].hasAttribute("disabled")).toBe(
      true,
    );
  });

  it("伺服器未連線（工具清單空）→ 顯示提示且存檔鈕停用，不得誤存成全關", async () => {
    const { onSave } = renderPanel(view({ tools: [] }));
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: /儲存|Save/ })
          .hasAttribute("disabled"),
      ).toBe(true),
    );
    expect(onSave).not.toHaveBeenCalled();
  });

  it("抓取失敗 → 顯示錯誤且存檔鈕停用（不得把失敗當成「沒有工具」而存成全關）", async () => {
    const { onSave } = renderPanel(null);
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: /儲存|Save/ })
          .hasAttribute("disabled"),
      ).toBe(true),
    );
    expect(onSave).not.toHaveBeenCalled();
  });
});
