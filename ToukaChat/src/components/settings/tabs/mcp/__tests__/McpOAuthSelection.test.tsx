import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import "@/lib/i18n";

import { McpOAuthSelection } from "../McpOAuthSelection";

describe("McpOAuthSelection", () => {
  it("只顯示後端投影的可信 issuer，並回傳 exact 值", () => {
    const onSelect = vi.fn();
    render(
      <McpOAuthSelection
        authorizationServers={[
          "https://issuer-a.example.test",
          "https://issuer-b.example.test/path",
        ]}
        disabled={false}
        onSelect={onSelect}
      />,
    );

    expect(screen.getByText("issuer-a.example.test")).toBeDefined();
    fireEvent.click(
      screen.getByRole("button", {
        name: "使用 issuer-b.example.test 授權",
      }),
    );
    expect(onSelect).toHaveBeenCalledWith("https://issuer-b.example.test/path");
  });
});
