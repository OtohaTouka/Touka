/**
 * ADR-0292：UserPromptSubmit hook 擋下的訊息和它的空回覆從對話樹拿掉，存回去的對話裡就沒有它，
 * 之後心跳、重新觸發、滑動視窗摘要與歸檔都讀不到（Claude Code 也會把擋下的訊息從上下文拿掉）。
 */
import { describe, it, expect } from "vitest";
import { appendAssistantNode, buildUserTurnTree, dropBlockedTurn } from "../stream_tree";
import { getLinearMessages, type TreeHistory } from "../tree_history";

const empty: TreeHistory = { messages: {}, currentId: null };

function turn(tree: TreeHistory, text: string, reply?: string, parentId = tree.currentId) {
  const { tree: withUser, userMsgId } = buildUserTurnTree(tree, { userText: text, attachment: null, parentId, timestamp: 1 });
  const { tree: withReply, assistantMsgId } = appendAssistantNode(withUser);
  const filled = reply === undefined ? withReply : {
    ...withReply,
    messages: { ...withReply.messages, [assistantMsgId]: { ...withReply.messages[assistantMsgId], content: reply } },
  };
  return { tree: filled, userMsgId, assistantMsgId };
}

describe("dropBlockedTurn", () => {
  it("removes the blocked message and its empty reply", () => {
    const first = turn(empty, "hello", "hi");
    const blocked = turn(first.tree, "secret");
    const dropped = dropBlockedTurn(blocked.tree, blocked.assistantMsgId);
    expect(dropped.messages[blocked.userMsgId]).toBeUndefined();
    expect(dropped.messages[blocked.assistantMsgId]).toBeUndefined();
    expect(dropped.messages[first.assistantMsgId].childrenIds).toEqual([]);
    expect(dropped.currentId).toBe(first.assistantMsgId);
    expect(getLinearMessages(dropped).map(m => m.content)).toEqual(["hello", "hi"]);
    expect(blocked.tree.messages[blocked.userMsgId]).toBeDefined();
  });

  it("empties a conversation whose first message was blocked", () => {
    const { tree, assistantMsgId } = turn(empty, "secret");
    expect(dropBlockedTurn(tree, assistantMsgId)).toEqual({ messages: {}, currentId: null });
  });

  it("moves to the latest other first message when an edited first message was blocked", () => {
    const first = turn(empty, "hello", "hi");
    const second = turn(first.tree, "more", "ok");
    const other = turn(second.tree, "again", "sure", null);
    const blocked = turn(other.tree, "secret", undefined, null);
    const dropped = dropBlockedTurn(blocked.tree, blocked.assistantMsgId);
    expect(dropped.messages[blocked.userMsgId]).toBeUndefined();
    expect(dropped.currentId).toBe(other.assistantMsgId);
    expect(getLinearMessages(dropped).map(m => m.content)).toEqual(["again", "sure"]);
    // 只剩較早的那條時接到它的末端
    const blockedAlone = turn(second.tree, "secret", undefined, null);
    const onlyFirst = dropBlockedTurn(blockedAlone.tree, blockedAlone.assistantMsgId);
    expect(onlyFirst.currentId).toBe(second.assistantMsgId);
    expect(getLinearMessages(onlyFirst).map(m => m.content)).toEqual(["hello", "hi", "more", "ok"]);
  });

  it("keeps the other branches under the same message", () => {
    const first = turn(empty, "hello", "hi");
    const kept = turn(first.tree, "a", "b");
    const blocked = turn(kept.tree, "secret", undefined, first.assistantMsgId);
    const dropped = dropBlockedTurn(blocked.tree, blocked.assistantMsgId);
    expect(dropped.messages[first.assistantMsgId].childrenIds).toEqual([kept.userMsgId]);
    expect(dropped.currentId).toBe(first.assistantMsgId);
    expect(getLinearMessages(dropped).map(m => m.content)).toEqual(["hello", "hi", "a", "b"]);
  });

  it("returns the same tree when the reply is not a fresh reply to a user message", () => {
    const { tree, assistantMsgId } = turn(empty, "x");
    expect(dropBlockedTurn(tree, "nope")).toBe(tree);
    const orphan = appendAssistantNode(empty).tree;
    expect(dropBlockedTurn(orphan, orphan.currentId!)).toBe(orphan);
    const answered = turn(tree, "y");
    expect(dropBlockedTurn(answered.tree, assistantMsgId)).toBe(answered.tree);
  });
});
