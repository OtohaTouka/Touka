import type { ChatAttachment } from "./attachment";
import type { Interjection } from "@/lib/types";
import { withInterjectionNotes } from "./interjections";
import { withTurnEndNote } from "./turnNotes";
import { neutralizeRecordMarkers, recordLevels, renderToolRecord, withToolRecord } from "./toolRecords";
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

function neutralizedAttachment(attachment?: ChatAttachment): ChatAttachment | undefined {
  if (!attachment) return attachment;
  return {
    ...attachment,
    ...(attachment.name ? { name: neutralizeRecordMarkers(attachment.name) } : {}),
    ...(attachment.text ? { text: neutralizeRecordMarkers(attachment.text) } : {}),
  };
}

const neutralizedInterjections = (items?: Interjection[]) => (Array.isArray(items)
  ? items.map(i => (typeof i?.text === 'string' ? { ...i, text: neutralizeRecordMarkers(i.text) } : i))
  : items);

/** 組下一輪的 API 訊息：本輪 user 用 resolvedInput；助理節點加插話標註（ADR-0249），
 *  被停止或出錯的再加一行記號（ADR-0318）。toolRecords 開著時，助理節點存下的工具步驟依視窗寫成紀錄
 *  放在原本文字前面（ADR-0319；群聊不開）；每一則的假紀錄標記都換掉，只有系統組的那段是真的。 */
export function buildApiHistory<R extends string>(
  linear: {
    id: string; role: R; content: string; attachment?: ChatAttachment; interjections?: Interjection[];
    generationStopped?: boolean; generationFailed?: boolean; steps?: unknown;
  }[],
  userMsgId: string,
  resolvedInput: string,
  options: { toolRecords?: boolean } = {},
): { role: R; content: string }[] {
  const levels = options.toolRecords ? recordLevels(linear) : linear.map(() => null);
  return linear.map((m, i) => {
    const content = neutralizeRecordMarkers(m.id === userMsgId ? resolvedInput : (m.content ?? ''));
    const interjections = neutralizedInterjections(m.interjections);
    const level = levels[i];
    const record = level ? renderToolRecord(m.steps, level) : '';
    return buildApiMessage({
      role: m.role,
      content: m.id === userMsgId ? content
        : m.role === 'assistant'
          ? withTurnEndNote(withInterjectionNotes(withToolRecord(content, record), interjections), m)
          : withInterjectionNotes(content, interjections),
      attachment: neutralizedAttachment(m.attachment),
    });
  });
}
