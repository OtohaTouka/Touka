import { describe, it, expect } from "vitest";
import { buildUserTurnTree } from "../stream_tree";
import type { ChatAttachment } from "../chat/attachment";
import type { TreeHistory } from "../tree_history";

const empty: TreeHistory = { messages: {}, currentId: null };

describe("buildUserTurnTree — attachment preservation (chat upload Phase 1)", () => {
  it("preserves text attachment fields (no longer stripped to type/url)", () => {
    const att: ChatAttachment = { type: "text", url: "", name: "notes.md", text: "hello", size: 5, mime: "text/markdown" };
    const { tree, userMsgId } = buildUserTurnTree(empty, { userText: "see file", attachment: att, parentId: null });
    const a = tree.messages[userMsgId].attachment;
    expect(a?.type).toBe("text");
    expect(a?.name).toBe("notes.md");
    expect(a?.text).toBe("hello"); // ← 之前的 {type,url} 複製會丟掉，這是回歸保護
    expect(a?.size).toBe(5);
  });

  it("still carries image attachment url", () => {
    const { tree, userMsgId } = buildUserTurnTree(empty, {
      userText: "",
      attachment: { type: "image", url: "data:img" },
      parentId: null,
    });
    expect(tree.messages[userMsgId].attachment?.url).toBe("data:img");
  });

  it("omits attachment when null", () => {
    const { tree, userMsgId } = buildUserTurnTree(empty, { userText: "hi", attachment: null, parentId: null });
    expect(tree.messages[userMsgId].attachment).toBeUndefined();
  });
});
