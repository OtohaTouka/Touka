import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import ArtifactView from "../ArtifactView";
import { useArtifacts } from "@/hooks/useArtifacts";

/** ADR-0294：artifact 的 Markdown 和聊天訊息走同一個共用元件，外部圖片不自動載入。 */
describe("ArtifactView — Markdown artifact", () => {
  beforeEach(() => useArtifacts.setState({ artifacts: [], currentArtifactId: null }));

  it("does not load an outside image", () => {
    useArtifacts.setState({
      artifacts: [{ id: "m1", title: "Notes", type: "markdown", content: "# Notes\n\n![x](https://evil.example/a.png?d=1)" }],
      currentArtifactId: "m1",
      isOpen: true,
      activeTab: "artifacts",
    });
    const { container } = render(<ArtifactView />);
    expect(container.querySelector('img[src*="evil.example"]')).toBeNull();
    expect(screen.getByRole("link", { name: /外部圖片（未載入）/ })).toHaveAttribute("rel", "noopener noreferrer");
  });
});
