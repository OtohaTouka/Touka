/**
 * ADR-0292：UserPromptSubmit hook 擋下的訊息標記在 user 節點上，之後組歷史時它和後面的回覆
 * 都不送給模型（Claude Code 也會把擋下的訊息從上下文拿掉）。
 */
import { describe, it, expect } from "vitest";
import { appendAssistantNode, buildUserTurnTree, markHookBlocked } from "../stream_tree";
import { buildApiHistory } from "../chat/buildApiMessage";
import { getLinearMessages, type TreeHistory } from "../tree_history";

const empty: TreeHistory = { messages: {}, currentId: null };

function turn(tree: TreeHistory, text: string, reply?: string) {
  const parentId = tree.currentId;
  const { tree: withUser, userMsgId } = buildUserTurnTree(tree, { userText: text, attachment: null, parentId, timestamp: 1 });
  const { tree: withReply, assistantMsgId } = appendAssistantNode(withUser);
  const filled = reply === undefined ? withReply : {
    ...withReply,
    messages: { ...withReply.messages, [assistantMsgId]: { ...withReply.messages[assistantMsgId], content: reply } },
  };
  return { tree: filled, userMsgId, assistantMsgId };
}

describe("markHookBlocked", () => {
  it("marks the user message the reply belongs to", () => {
    const { tree, userMsgId, assistantMsgId } = turn(empty, "secret");
    const marked = markHookBlocked(tree, assistantMsgId);
    expect(marked.messages[userMsgId].hookBlocked).toBe(true);
    expect(marked.messages[assistantMsgId].hookBlocked).toBeUndefined();
    expect(tree.messages[userMsgId].hookBlocked).toBeUndefined();
  });

  it("returns the same tree when the reply or its user message is missing", () => {
    const { tree } = turn(empty, "x");
    expect(markHookBlocked(tree, "nope")).toBe(tree);
    const orphan = appendAssistantNode(empty).tree;
    expect(markHookBlocked(orphan, orphan.currentId!)).toBe(orphan);
  });
});

describe("buildApiHistory leaves blocked messages out", () => {
  it("drops the blocked message and the empty reply after it", () => {
    const first = turn(empty, "hello", "hi");
    const blocked = turn(first.tree, "secret");
    const marked = markHookBlocked(blocked.tree, blocked.assistantMsgId);
    const next = turn(marked, "next");
    const linear = getLinearMessages(next.tree).slice(0, -1);
    expect(buildApiHistory(linear, next.userMsgId, "next")).toEqual([
      { role: "user", content: "hello" },
      { role: "assistant", content: "hi" },
      { role: "user", content: "next" },
    ]);
  });

  it("still sends the message being sent now", () => {
    const { tree, userMsgId, assistantMsgId } = turn(empty, "again");
    const marked = markHookBlocked(tree, assistantMsgId);
    const linear = getLinearMessages(marked).slice(0, -1);
    expect(buildApiHistory(linear, userMsgId, "again")).toEqual([{ role: "user", content: "again" }]);
  });
});
