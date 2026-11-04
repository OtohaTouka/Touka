import http from "node:http";
import { FAKE_AFTER_TOOL_REPLY, FAKE_MODEL_ID, FAKE_TEXT_REPLY, WRITE_REQUEST_PATTERN } from "./constants";

export interface ModelCall {
  model: string;
  stream: boolean;
  lastRole: string;
  lastText: string;
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
  const fileName = call.lastText.match(WRITE_REQUEST_PATTERN)?.[1];
  if (call.lastRole === "user" && fileName && call.tools.includes("write")) {
    const args = JSON.stringify({ scope: "personal", path: fileName, content: "x" });
    return {
      role: "assistant",
      content: null,
      tool_calls: [{ id: "call_1", type: "function", function: { name: "write", arguments: args } }],
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
  return http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => {
      const url = req.url ?? "";
      if (url === "/_calls") return writeJson(res, calls);
      if (url === "/_reset") {
        calls = [];
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
        tools: (body.tools ?? []).flatMap((tool) => tool.function?.name ?? []),
      };
      calls.push(call);
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
    });
  });
}
