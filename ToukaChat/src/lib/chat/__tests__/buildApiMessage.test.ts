import { describe, it, expect } from "vitest";
import { buildApiHistory, buildApiMessage } from "../buildApiMessage";
import {
  DELIVERY_PLATFORM_NAMES, OTHER_PLATFORM_NAME, TURN_FAILED_NOTE, TURN_STOPPED_NOTE,
  TURN_UNDELIVERED_NOTE_TEMPLATE,
} from "../turnNotes";

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

// ADR-0318：被停止或出錯的那則回覆送給模型時前面加一行記號；只看助理、只認 true
describe("buildApiHistory turn-end notes", () => {
  const linear = [
    { id: "u1", role: "user" as const, content: "q1", generationStopped: true },
    { id: "a1", role: "assistant" as const, content: "half", generationStopped: true },
    { id: "u2", role: "user" as const, content: "q2" },
    { id: "a2", role: "assistant" as const, content: "", generationFailed: true },
    { id: "u3", role: "user" as const, content: "draft" },
  ];

  it("marks stopped and failed replies and leaves the rest alone", () => {
    expect(buildApiHistory(linear, "u3", "final").map(m => m.content)).toEqual([
      "q1", `${TURN_STOPPED_NOTE}\n\nhalf`, "q2", TURN_FAILED_NOTE, "final",
    ]);
  });

  it("puts the note before interjection notes and prefers stopped over failed", () => {
    const reply = {
      id: "a", role: "assistant" as const, content: "x", generationStopped: true, generationFailed: true,
      interjections: [{ id: "i", text: "y", urgent: false, status: "delivered" as const, step: 1, created_at: 1 }],
    };
    expect(buildApiHistory([reply], "none", "")[0].content).toBe(
      `${TURN_STOPPED_NOTE}\n\n〔本回合中使用者補充（第 1 步）：y〕\n\nx`,
    );
  });

  it("marks an SNS reply that was not delivered, after the failed note (ADR-0322 PR 2)", () => {
    const undelivered = (name: string) => TURN_UNDELIVERED_NOTE_TEMPLATE.replace("{platform}", name);
    const reply = {
      id: "a", role: "assistant" as const, content: "x",
      generationFailed: true, deliveryFailed: true, deliveryPlatform: "discord",
    };
    expect(buildApiHistory([reply], "none", "")[0].content).toBe(
      `${TURN_FAILED_NOTE}\n${undelivered(DELIVERY_PLATFORM_NAMES.discord)}\n\nx`,
    );
    const other = { id: "b", role: "assistant" as const, content: "", deliveryFailed: true, deliveryPlatform: "line" };
    expect(buildApiHistory([other], "none", "")[0].content).toBe(undelivered(OTHER_PLATFORM_NAME));
  });

  it.each(["constructor", "__proto__", "toString"])(
    "treats the object built-in name %s as an unknown platform, like the backend",
    (platform) => {
      const reply = { id: "c", role: "assistant" as const, content: "", deliveryFailed: true, deliveryPlatform: platform };
      expect(buildApiHistory([reply], "none", "")[0].content).toBe(
        TURN_UNDELIVERED_NOTE_TEMPLATE.replace("{platform}", OTHER_PLATFORM_NAME),
      );
    },
  );
});

describe("buildApiHistory old log lines (ADR-0322 PR 2b)", () => {
  it("drops old log lines from replies, keeps the user's own words, and adds notes after", () => {
    const linear = [
      { id: "u1", role: "user" as const, content: "✅ `時間記錄：我自己打的`\n" },
      {
        id: "a1", role: "assistant" as const, generationStopped: true,
        content: "先查\n✅ `時間記錄：2026-11-18 10:00:00 (週三)`\n好了\n\n⏹️ 已由使用者中斷。",
      },
    ];
    expect(buildApiHistory(linear, "none", "").map(m => m.content)).toEqual([
      "✅ `時間記錄：我自己打的`\n",
      `${TURN_STOPPED_NOTE}\n\n先查\n好了`,
    ]);
  });
});
