import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import "@/lib/i18n";

import { McpOAuthStatus } from "../McpOAuthStatus";

describe("McpOAuthStatus", () => {
  it("auth_required 顯示連接操作", () => {
    const onConnect = vi.fn();
    render(
      <McpOAuthStatus
        status="auth_required"
        busy={false}
        error={null}
        onConnect={onConnect}
        onDisconnect={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "連接帳號" }));
    expect(onConnect).toHaveBeenCalledTimes(1);
  });

  it("connected 顯示已授權並提供斷開操作", () => {
    const onDisconnect = vi.fn();
    render(
      <McpOAuthStatus
        status="connected"
        busy={false}
        error={null}
        onConnect={vi.fn()}
        onDisconnect={onDisconnect}
      />,
    );

    expect(screen.getByText("已授權")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "斷開帳號" }));
    expect(onDisconnect).toHaveBeenCalledTimes(1);
  });

  it("popup blocker 以可見錯誤呈現", () => {
    render(
      <McpOAuthStatus
        status="auth_required"
        busy={false}
        error="popup_blocked"
        onConnect={vi.fn()}
        onDisconnect={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert").textContent).toContain("彈出視窗");
  });
});
