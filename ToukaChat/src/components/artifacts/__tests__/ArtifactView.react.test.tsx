import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import ArtifactView from "../ArtifactView";
import { useArtifacts } from "@/hooks/useArtifacts";

// lazy 依賴以 mock 取代（不需真的 Babel/真的 runtime bundle）。
vi.mock("@/lib/artifacts/transpile", () => ({
  transpileArtifact: vi.fn(async (src: string) =>
    src.includes("BREAK")
      ? { error: "Unexpected token (1:0)" }
      : { code: "module.exports.default = function(){ return null; };" },
  ),
}));
vi.mock("@/generated/artifactRuntime", () => ({
  ARTIFACT_RUNTIME: "/*RUNTIME*/window.React={};window.__touka_mount=function(){};",
}));

function setReactArtifact(content: string) {
  useArtifacts.setState({
    artifacts: [{ id: "a1", title: "Demo", type: "react", content }],
    currentArtifactId: "a1",
    isOpen: true,
    activeTab: "artifacts",
  });
}

describe("ArtifactView — React artifact (ADR-0114)", () => {
  beforeEach(() => useArtifacts.setState({ artifacts: [], currentArtifactId: null }));

  it("renders an interactive React artifact in a sandboxed iframe (no same-origin, no network)", async () => {
    setReactArtifact("export default () => <div/>;");
    render(<ArtifactView />);

    const iframe = await waitFor(() => {
      const f = document.querySelector('iframe[title="React Artifact"]') as HTMLIFrameElement | null;
      expect(f).not.toBeNull();
      return f as HTMLIFrameElement;
    });

    // 安全不變式 #2：sandbox 只有 allow-scripts，絕無 same-origin。
    expect(iframe.getAttribute("sandbox")).toBe("allow-scripts");
    const srcdoc = iframe.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toContain("/*RUNTIME*/"); // runtime 已注入
    expect(srcdoc).toContain("__touka_mount"); // 掛載呼叫
    expect(srcdoc).toContain("module.exports.default"); // 轉譯後使用者碼
    expect(srcdoc).toContain("connect-src 'none'"); // 安全不變式 #3：無網路
  });

  it("shows a friendly transpile error instead of crashing", async () => {
    setReactArtifact("BREAK export default");
    render(<ArtifactView />);
    await waitFor(() => {
      expect(screen.getByText(/編譯失敗|Compile failed/)).toBeTruthy();
    });
    // 轉譯失敗時不渲染 iframe
    expect(document.querySelector('iframe[title="React Artifact"]')).toBeNull();
  });
});
