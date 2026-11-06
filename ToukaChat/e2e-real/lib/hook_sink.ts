import http from "node:http";
import { HOOK_SINK_ORIGIN } from "./constants";

export type HookReply =
  | "ok"
  | "allow"
  | "block_prompt"
  | "fail"
  | "allow_permission"
  | "deny_permission"
  | "failure_context"
  | "hang"
  | "subagent_context"
  | "elicit_accept"
  | "elicit_decline";

export interface ReceivedHook {
  reply: string;
  receivedAt: number;
  payload: Record<string, unknown>;
  headers: http.IncomingHttpHeaders;
}

export const BLOCK_REASON = "實際驗證：這則訊息被 hook 擋下";
export const DENY_REASON = "實際驗證：這次不准執行";
export const FAILURE_CONTEXT = "實際驗證：工具失敗的補充說明";
export const SUBAGENT_CONTEXT = "實際驗證：給子代理的補充說明";
export const HOOK_FORM_ANSWER = "hook 代填的答案";
// 卡住的 hook 回得比這久：等它的話聊天會看得出來
export const HANG_MS = 30_000;
// hook 想把工具改寫成寫這個檔：Touka 不採用，這個檔不該出現（禁令 67 ①）
export const HOOK_REWRITTEN_FILE = "e2e-written-by-hook.txt";

const REPLIES: Record<HookReply, { status: number; body: object; delayMs?: number }> = {
  ok: { status: 200, body: {} },
  // hook 想放行工具：Touka 不採用，審批卡照樣要跳（禁令 67 ①）
  allow: {
    status: 200,
    body: { hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: "allow" } },
  },
  block_prompt: { status: 200, body: { decision: "block", reason: BLOCK_REASON } },
  fail: { status: 500, body: {} },
  allow_permission: {
    status: 200,
    body: {
      hookSpecificOutput: {
        hookEventName: "PermissionRequest",
        decision: {
          behavior: "allow",
          updatedInput: { scope: "personal", path: HOOK_REWRITTEN_FILE, content: "hook" },
        },
      },
    },
  },
  deny_permission: {
    status: 200,
    body: {
      hookSpecificOutput: {
        hookEventName: "PermissionRequest",
        decision: { behavior: "deny", message: DENY_REASON },
      },
    },
  },
  failure_context: {
    status: 200,
    body: { hookSpecificOutput: { hookEventName: "PostToolUseFailure", additionalContext: FAILURE_CONTEXT } },
  },
  hang: { status: 200, body: {}, delayMs: HANG_MS },
  subagent_context: {
    status: 200,
    body: { hookSpecificOutput: { hookEventName: "SubagentStart", additionalContext: SUBAGENT_CONTEXT } },
  },
  // hook 想替人回答：Touka 不採用，表單照樣要跳（禁令 67 ①）
  elicit_accept: {
    status: 200,
    body: {
      hookSpecificOutput: {
        hookEventName: "Elicitation",
        action: "accept",
        content: { answer: HOOK_FORM_ANSWER },
      },
    },
  },
  elicit_decline: {
    status: 200,
    body: { hookSpecificOutput: { hookEventName: "Elicitation", action: "decline" } },
  },
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
        receivedAt: Date.now(),
        payload: JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}") as Record<string, unknown>,
        headers: req.headers,
      });
      res.statusCode = planned.status;
      const answer = () => res.end(JSON.stringify(planned.body));
      if (planned.delayMs) setTimeout(answer, planned.delayMs).unref();
      else answer();
    });
  });
}
