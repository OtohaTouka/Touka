import { describe, it, expect } from "vitest";
import { formatToOpenWebUI } from "../export-utils";
import type { Session } from "../types";
import { getSystemEpoch } from "../time_utils";

const T0 = 1_760_486_400_000; // 2025-10-15 08:00 Asia/Taipei（毫秒）

const session = (messages: Record<string, unknown>): Session => ({
  id: "s1", title: "t", updatedAt: T0 + 9_000, agent_id: "Hanatou",
  messages: { messages, currentId: Object.keys(messages).pop() ?? null },
} as unknown as Session);

describe("formatToOpenWebUI — legacy dates are the message's actual time (seconds)", () => {
  it.each([
    { name: "ms timestamp → seconds", node: { timestamp: T0 + 1_500 }, expected: T0 / 1000 + 1 },
    { name: "seconds timestamp (imported) → unchanged", node: { timestamp: T0 / 1000 }, expected: T0 / 1000 },
    { name: "old createdAt still honoured", node: { createdAt: T0 }, expected: T0 / 1000 },
    { name: "timestamp wins over createdAt", node: { timestamp: T0, createdAt: T0 + 60_000 }, expected: T0 / 1000 },
  ])("$name", ({ node, expected }) => {
    const out = formatToOpenWebUI(session({ m1: { id: "m1", parentId: null, childrenIds: [], role: "user", content: "x", ...node } }), "m");
    expect(out.chat.history.messages.m1.timestamp).toBe(expected);
    expect(out.created_at).toBe(expected);
  });

  it("no time at all → export time (as before)", () => {
    const before = Math.floor(getSystemEpoch() / 1000);
    const out = formatToOpenWebUI(session({ m1: { id: "m1", parentId: null, childrenIds: [], role: "user", content: "x" } }), "m");
    expect(out.chat.history.messages.m1.timestamp).toBeGreaterThanOrEqual(before);
    expect(out.updated_at).toBe(Math.floor((T0 + 9_000) / 1000));
  });
});
