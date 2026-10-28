import type { ChatAttachment } from "./attachment";
import type { Interjection } from "@/lib/types";
import { withInterjectionNotes } from "./interjections";
import { buildTextInlinePart, buildFileRefHint } from "./uploadClassify";

/**
 * 把一個對話節點（含附件）組成送往後端的 API 訊息（SSOT，純函式）。
 *
 * - image → 多模態 JSON 字串（text + image_url，後端 adapter 再解析）。
 * - text  → 把抽出的檔案內容**內嵌**進文字（純文字，後端零改）。
 * - 其餘  → 純文字原樣。
 *
 * role 以泛型保留呼叫端的 union 型別（"user"|"assistant"|"system"）。純函式、可單元測試——
 * 訊息組裝邏輯集中一處，StreamRegistryProvider 只負責呼叫（SRP）。
 */
export function buildApiMessage<R extends string>(input: {
  role: R;
  content: string;
  attachment?: ChatAttachment;
}): { role: R; content: string } {
  const { role, content, attachment } = input;
  if (attachment?.type === "image") {
    return {
      role,
      content: JSON.stringify([
        { type: "text", text: content || "" },
        { type: "image_url", image_url: { url: attachment.url } },
      ]),
    };
  }
  if (attachment?.type === "text" && attachment.text) {
    return {
      role,
      content: (content || "") + buildTextInlinePart(attachment.name || "file", attachment.text),
    };
  }
  // 已上傳文件：注入「請 AI 用工具讀」提示（含 vault 相對路徑）。
  if (attachment?.type === "file_ref" && attachment.relpath) {
    return {
      role,
      content: (content || "") + buildFileRefHint(attachment.name || "file", attachment.relpath),
    };
  }
  return { role, content };
}

interface HistoryNode<R extends string> {
  id: string;
  parentId?: string | null;
  role: R;
  content: string;
  attachment?: ChatAttachment;
  interjections?: Interjection[];
  hookBlocked?: boolean;
}

/**
 * 組下一輪的 API 訊息：本輪 user 用 resolvedInput；助理節點加插話標註（ADR-0249）。
 * 被 hook 擋下的 user 訊息和接在它後面的回覆不送（ADR-0292；Claude Code 也會把擋下的訊息
 * 從上下文拿掉）。
 */
export function buildApiHistory<R extends string>(
  linear: HistoryNode<R>[],
  userMsgId: string,
  resolvedInput: string,
): { role: R; content: string }[] {
  const blocked = new Set(linear.filter(m => m.hookBlocked && m.id !== userMsgId).map(m => m.id));
  return linear
    .filter(m => !blocked.has(m.id) && !(m.parentId && blocked.has(m.parentId)))
    .map(m => buildApiMessage({
      role: m.role,
      content: m.id === userMsgId ? resolvedInput : withInterjectionNotes(m.content, m.interjections),
      attachment: m.attachment,
    }));
}
