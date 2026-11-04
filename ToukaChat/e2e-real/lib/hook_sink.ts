import http from "node:http";
import { HOOK_SINK_ORIGIN } from "./constants";

export type HookReply = "ok" | "allow" | "block_prompt" | "fail";

export interface ReceivedHook {
  reply: string;
  payload: Record<string, unknown>;
  headers: http.IncomingHttpHeaders;
}

export const BLOCK_REASON = "實際驗證：這則訊息被 hook 擋下";

const REPLIES: Record<HookReply, { status: number; body: object }> = {
  ok: { status: 200, body: {} },
  // hook 想放行工具：Touka 不採用，審批卡照樣要跳（禁令 67 ①）
  allow: {
    status: 200,
    body: { hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: "allow" } },
  },
  block_prompt: { status: 200, body: { decision: "block", reason: BLOCK_REASON } },
  fail: { status: 500, body: {} },
};

export function hookUrl(reply: HookReply): string {
  return `${HOOK_SINK_ORIGIN}/hook/${reply}`;
}

/** 收 http hook 的小伺服器：路徑決定怎麼回，收到的內容經 `/_received` 給測試核對。 */
export function createHookSink(): http.Server {
  let received: ReceivedHook[] = [];
  return http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => {
      res.setHeader("content-type", "application/json");
      const url = req.url ?? "";
      if (url === "/_received") return res.end(JSON.stringify(received));
      if (url === "/_reset") {
        received = [];
        return res.end("{}");
      }
      const reply = url.split("/").at(-1) as HookReply;
      const planned = REPLIES[reply];
      if (!planned) {
        res.statusCode = 404;
        return res.end("{}");
      }
      received.push({
        reply,
        payload: JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}") as Record<string, unknown>,
        headers: req.headers,
      });
      res.statusCode = planned.status;
      res.end(JSON.stringify(planned.body));
    });
  });
}
