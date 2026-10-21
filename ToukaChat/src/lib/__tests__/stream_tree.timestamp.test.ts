import { describe, it, expect } from "vitest";
import { buildUserTurnTree, appendAssistantNode, stampNodeTime } from "../stream_tree";
import type { TreeHistory } from "../tree_history";
import { getSystemEpoch, toEpochMs } from "../time_utils";

const backendStamp = 1_760_486_400_000;
const base: TreeHistory = {
  messages: {
    a0: { id: "a0", parentId: null, childrenIds: [], role: "assistant", content: "heartbeat", timestamp: backendStamp },
    a1: { id: "a1", parentId: "a0", childrenIds: [], role: "assistant", content: "old, no time" },
  },
  currentId: "a1",
};
base.messages.a0.childrenIds = ["a1"];

describe("message timestamps — new nodes carry the actual time (ms)", () => {
  it("stamps the user node with the given send time", () => {
    const { tree, userMsgId } = buildUserTurnTree(base, { userText: "hi", attachment: null, parentId: "a1", timestamp: 1234 });
    expect(tree.messages[userMsgId].timestamp).toBe(1234);
  });

  it("defaults to the frontend system clock in milliseconds", () => {
    const before = getSystemEpoch();
    const { tree, userMsgId } = buildUserTurnTree(base, { userText: "hi", attachment: null, parentId: "a1" });
    const stamp = tree.messages[userMsgId].timestamp!;
    expect(stamp).toBeGreaterThanOrEqual(before);
    expect(stamp).toBeLessThanOrEqual(getSystemEpoch());
  });

  it("keeps existing timestamps (including backend-written ones) and adds none to old nodes", () => {
    const { tree } = buildUserTurnTree(base, { userText: "hi", attachment: null, parentId: "a1", timestamp: 9 });
    expect(tree.messages.a0.timestamp).toBe(backendStamp);
    expect(tree.messages.a1).not.toHaveProperty("timestamp");
  });

  it("the assistant placeholder has no time until it is finished", () => {
    const { tree, assistantMsgId } = appendAssistantNode(base);
    expect(tree.messages[assistantMsgId]).not.toHaveProperty("timestamp");
  });
});

describe("stampNodeTime — completion time only when the node has none", () => {
  it.each([
    { name: "no time yet → stamped", node: "a1", expected: 77 },
    { name: "already has a time → unchanged", node: "a0", expected: backendStamp },
  ])("$name", ({ node, expected }) => {
    const out = stampNodeTime(base, node, 77);
    expect(out.messages[node].timestamp).toBe(expected);
    expect(base.messages.a1).not.toHaveProperty("timestamp"); // 不改原樹
  });

  it("missing node → same tree", () => {
    expect(stampNodeTime(base, "gone", 77)).toBe(base);
  });

  it("an existing node keeps the same object when unchanged", () => {
    expect(stampNodeTime(base, "a0", 77)).toBe(base);
  });
});

describe("toEpochMs — seconds or milliseconds to milliseconds", () => {
  it.each([
    [1_760_486_400_000, 1_760_486_400_000], // 毫秒
    [1_760_486_400, 1_760_486_400_000], // 匯入的 Open WebUI 檔是秒
    [1_760_486_400.5, 1_760_486_400_500],
    [0, undefined],
    [-5, undefined],
    [Number.NaN, undefined],
    ["1760486400", undefined], // 只收數字
    [undefined, undefined],
    [null, undefined],
  ])("%s → %s", (value, expected) => {
    expect(toEpochMs(value)).toBe(expected);
  });
});
