import { describe, it, expect } from "vitest";
import { buildApiMessage } from "../buildApiMessage";

describe("buildApiMessage", () => {
  it("returns plain content when no attachment", () => {
    expect(buildApiMessage({ role: "user", content: "hi" })).toEqual({ role: "user", content: "hi" });
  });

  it("image attachment → multimodal JSON (text + image_url)", () => {
    const r = buildApiMessage({ role: "user", content: "look", attachment: { type: "image", url: "data:x" } });
    const parsed = JSON.parse(r.content);
    expect(parsed[0]).toEqual({ type: "text", text: "look" });
    expect(parsed[1]).toEqual({ type: "image_url", image_url: { url: "data:x" } });
  });

  it("text attachment → inlines the file content into the message text", () => {
    const r = buildApiMessage({
      role: "user",
      content: "see file",
      attachment: { type: "text", url: "", name: "a.md", text: "FILEBODY" },
    });
    expect(r.content).toContain("see file");
    expect(r.content).toContain("FILEBODY");
    expect(r.content).toContain("[Attached file: a.md]");
  });

  it("text attachment without text → plain content (no crash)", () => {
    const r = buildApiMessage({ role: "user", content: "x", attachment: { type: "text", url: "" } });
    expect(r.content).toBe("x");
  });

  it("file_ref attachment → injects a tool-read hint with the vault path", () => {
    const r = buildApiMessage({
      role: "user",
      content: "summarize this",
      attachment: { type: "file_ref", url: "", name: "report.pdf", relpath: "outputs/uploads/s1/report.pdf" },
    });
    expect(r.content).toContain("summarize this");
    expect(r.content).toContain("report.pdf");
    expect(r.content).toContain("outputs/uploads/s1/report.pdf");
  });

  it("file_ref without relpath → plain content (no crash)", () => {
    const r = buildApiMessage({ role: "user", content: "x", attachment: { type: "file_ref", url: "" } });
    expect(r.content).toBe("x");
  });

  it("preserves the role union type/value", () => {
    expect(buildApiMessage({ role: "assistant", content: "y" }).role).toBe("assistant");
  });
});
