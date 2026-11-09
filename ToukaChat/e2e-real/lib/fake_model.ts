import http from "node:http";
import {
  CANARY_API_KEY,
  FAKE_AFTER_TOOL_REPLY,
  FAKE_MODEL_ID,
  FAKE_TEXT_REPLY,
  MODEL_400_REQUEST,
  MODEL_BAD_JSON_REQUEST,
  MODEL_EMPTY_REPLY_REQUEST,
  MODEL_HOLD_REQUEST,
  MODEL_NO_REPLY_REQUEST,
  toolRequestIn,
} from "./constants";

export interface ModelCall {
  model: string;
  stream: boolean;
  lastRole: string;
  lastText: string;
  /** 這次請求所有訊息的文字（看前情、舊家內容有沒有帶到）。 */
  allText: string;
  tools: string[];
}

interface ChatMessage {
  role?: string;
  content?: unknown;
}

interface ChatRequest {
  model?: string;
  stream?: boolean;
  messages?: ChatMessage[];
  tools?: { function?: { name?: string } }[];
}

type AssistantMessage =
  | { role: "assistant"; content: string }
  | { role: "assistant"; content: null; tool_calls: ToolCallOut[] };

interface ToolCallOut {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

const USAGE = { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 };

function contentText(content: unknown): string {
  return typeof content === "string" ? content : JSON.stringify(content ?? "");
}

function assistantReply(call: ModelCall): AssistantMessage {
  if (call.lastRole === "tool") return { role: "assistant", content: FAKE_AFTER_TOOL_REPLY };
  const request = call.lastRole === "user" ? toolRequestIn(call.lastText) : null;
  if (request && call.tools.includes(request.tool)) {
    return {
      role: "assistant",
      content: null,
      tool_calls: [
        { id: "call_1", type: "function", function: { name: request.tool, arguments: request.args } },
      ],
    };
  }
  return { role: "assistant", content: FAKE_TEXT_REPLY };
}

function writeStream(res: http.ServerResponse, model: string, message: AssistantMessage): void {
  const base = { id: "c1", object: "chat.completion.chunk", created: 1, model };
  const send = (body: object) => res.write(`data: ${JSON.stringify(body)}\n\n`);
  res.writeHead(200, { "content-type": "text/event-stream" });
  if ("tool_calls" in message) {
    const toolCalls = message.tool_calls.map((call, index) => ({ index, ...call }));
    send({ ...base, choices: [{ index: 0, delta: { role: "assistant", tool_calls: toolCalls } }] });
    send({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }], usage: USAGE });
  } else {
    send({ ...base, choices: [{ index: 0, delta: { role: "assistant", content: message.content } }] });
    send({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "stop" }], usage: USAGE });
  }
  res.end("data: [DONE]\n\n");
}

function writeJson(res: http.ServerResponse, body: unknown): void {
  res.setHeader("content-type", "application/json");
  res.end(JSON.stringify(body));
}

/** OpenAI 相容的假模型。後端每輪一次拿完整回覆（一般 JSON），串流也支援。`/_calls` 給測試核對。 */
export function createFakeModel(): http.Server {
  let calls: ModelCall[] = [];
  // 「第幾次呼叫才等」：心跳的 init_prompt 只進第一輪，第二輪之後認不出來，只能數次數
  let holdAtCall = 0;
  let held: { res: http.ServerResponse; call: ModelCall }[] = [];
  return http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => {
      const url = req.url ?? "";
      if (url === "/_calls") return writeJson(res, calls);
      if (url === "/_reset") {
        // 上一條留下的「第幾次才扣」也清掉，不然會扣到這一條的呼叫。還扣著的直接斷線：
        // 後端對讀到一半斷線不重試，那一輪馬上結束；回應的話那一輪會接著跑、再呼叫模型。
        calls = [];
        holdAtCall = 0;
        for (const item of held) item.res.destroy();
        held = [];
        return writeJson(res, {});
      }
      const hold = url.match(/^\/_hold\/(\d+)$/);
      if (hold) {
        holdAtCall = calls.length + Number(hold[1]);
        return writeJson(res, {});
      }
      if (url === "/_held") return writeJson(res, { count: held.length });
      if (url === "/_release") {
        holdAtCall = 0;
        for (const item of held) answer(item.res, item.call);
        held = [];
        return writeJson(res, {});
      }
      if (url.endsWith("/models")) {
        return writeJson(res, { object: "list", data: [{ id: FAKE_MODEL_ID, object: "model" }] });
      }
      if (!url.endsWith("/chat/completions")) {
        res.statusCode = 404;
        return res.end("{}");
      }
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}") as ChatRequest;
      const last = body.messages?.at(-1) ?? {};
      const call: ModelCall = {
        model: body.model ?? "",
        stream: Boolean(body.stream),
        lastRole: last.role ?? "",
        lastText: contentText(last.content),
        allText: (body.messages ?? []).map((message) => contentText(message.content)).join("\n"),
        tools: (body.tools ?? []).flatMap((tool) => tool.function?.name ?? []),
      };
      calls.push(call);
      const waiting = calls.length === holdAtCall || (call.lastRole === "user" && call.lastText.includes(MODEL_HOLD_REQUEST));
      if (waiting) {
        held.push({ res, call });
        return;
      }
      return answer(res, call);
    });
  });
}

/**
 * 最後一則 user 裡最新的那一段。失敗那則空白回覆送出前會被拿掉，前後兩則 user 用空行併成一則
 * （ADR-0231 決定 6），尾端還附著 Touka 加的區塊（`<continuity>`、hook 輸出、IDE 內容）；
 * 先去掉這些區塊，再照最新那一段的要求回，不照上一則失敗的。測試訊息本身不含空行。
 * 工具與扣住的觸發字仍看整段（目前沒有測試在失敗後接著送這兩種）：失敗後同一段再送一則時兩則 user
 * 會併成一則，上一則含扣住的觸發字的話，這一則也會被扣。
 */
function newestUserText(call: ModelCall): string {
  if (call.lastRole !== "user") return "";
  const body = call.lastText.split(/\n\n<(?:continuity|hook_output|ide_context)>/)[0];
  return body.trimEnd().split("\n\n").at(-1) ?? "";
}

function answer(res: http.ServerResponse, call: ModelCall): void {
  const newest = newestUserText(call);
  if (newest.includes(MODEL_400_REQUEST)) {
    res.statusCode = 400;
    return writeJson(res, { error: { message: `bad request api_key=${CANARY_API_KEY}` } });
  }
  if (newest.includes(MODEL_BAD_JSON_REQUEST)) {
    res.setHeader("content-type", "text/html");
    res.end("<html>not json</html>");
    return;
  }
  if (newest.includes(MODEL_NO_REPLY_REQUEST)) {
    return writeJson(res, { error: { type: "overloaded_error", message: "overloaded" } });
  }
  if (newest.includes(MODEL_EMPTY_REPLY_REQUEST)) {
    return writeJson(res, {
      id: "c1",
      object: "chat.completion",
      created: 1,
      model: call.model,
      choices: [{
        index: 0,
        message: { role: "assistant", content: "" },
        finish_reason: "insufficient_system_resource",
      }],
      usage: USAGE,
    });
  }
  const message = assistantReply(call);
  if (call.stream) return writeStream(res, call.model, message);
  return writeJson(res, {
    id: "c1",
    object: "chat.completion",
    created: 1,
    model: call.model,
    choices: [{ index: 0, message, finish_reason: "tool_calls" in message ? "tool_calls" : "stop" }],
    usage: USAGE,
  });
}
