import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { McpInteractionCard } from "../McpInteractionCard";
import type { ToolStep } from "@/lib/tree_history";

const fetchToukaMock = vi.fn();
vi.mock("@/lib/connections", () => ({
  fetchTouka: (...args: unknown[]) => fetchToukaMock(...args),
  getActiveProxyUrl: () => "http://proxy.test",
}));

const step: ToolStep = {
  id: "mcpint_local",
  tool: "srv__demo",
  args: null,
  status: "awaiting_approval",
  interaction: {
    execId: "mcpint_local",
    kind: "elicitation_form",
    message: "Choose a value",
    timeout: 120,
    schema: {
      type: "object",
      properties: {
        answer: { type: "string", title: "Answer" },
        count: { type: "integer", title: "Count" },
      },
      required: ["answer"],
    },
  },
};

beforeEach(() => {
  fetchToukaMock.mockReset();
  fetchToukaMock.mockResolvedValue({ ok: true });
});

describe("McpInteractionCard", () => {
  it("renders schema fields without any upstream request state", () => {
    render(<McpInteractionCard step={step} />);
    expect(screen.getByText("Choose a value")).toBeInTheDocument();
    expect(screen.getByLabelText("Answer")).toBeInTheDocument();
    expect(screen.getByLabelText("Count")).toBeInTheDocument();
    expect(screen.queryByText(/requestState/i)).not.toBeInTheDocument();
  });

  it("accept posts typed payload to the local approval id", async () => {
    render(<McpInteractionCard step={step} />);
    fireEvent.change(screen.getByLabelText("Answer"), {
      target: { value: "yes" },
    });
    fireEvent.change(screen.getByLabelText("Count"), {
      target: { value: "2" },
    });
    fireEvent.click(screen.getByRole("button", { name: /送出並繼續/ }));

    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenCalledWith(
        "http://proxy.test",
        "/v1/tools/approve/mcpint_local",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ payload: { answer: "yes", count: 2 } }),
        },
      );
    });
    expect(JSON.stringify(fetchToukaMock.mock.calls)).not.toContain(
      "requestState",
    );
  });

  it("preserves the primitive type of enum values", async () => {
    const enumStep: ToolStep = {
      ...step,
      interaction: {
        ...step.interaction!,
        schema: {
          type: "object",
          properties: {
            priority: {
              type: "integer",
              title: "Priority",
              enum: [1, 2],
            },
          },
          required: ["priority"],
        },
      },
    };
    render(<McpInteractionCard step={enumStep} />);
    fireEvent.change(screen.getByLabelText("Priority"), {
      target: { value: "1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /送出並繼續/ }));

    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenCalledWith(
        "http://proxy.test",
        "/v1/tools/approve/mcpint_local",
        expect.objectContaining({
          body: JSON.stringify({ payload: { priority: 2 } }),
        }),
      );
    });
  });

  it.each([
    ["拒絕", "/v1/tools/reject/mcpint_local"],
    ["取消", "/v1/tools/cancel/mcpint_local"],
  ])("%s uses its distinct interaction action", async (label, path) => {
    render(<McpInteractionCard step={step} />);
    fireEvent.click(screen.getByRole("button", { name: label }));
    await waitFor(() => {
      expect(fetchToukaMock).toHaveBeenCalledWith(
        "http://proxy.test",
        path,
        { method: "POST" },
      );
    });
  });
});
