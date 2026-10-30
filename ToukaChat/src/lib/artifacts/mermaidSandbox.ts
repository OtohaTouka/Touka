import { errorMsg } from "@/lib/error_handler";
import { MERMAID_CONFIG } from "./mermaidConfig";
import { ARTIFACT_CSP, ARTIFACT_SANDBOX, escapeForScript } from "./sandbox";

/**
 * 流程圖在沙箱框裡畫（ADR-0294）。
 *
 * mermaid 畫圖時會先把圖放進頁面量尺寸，圖裡的 `<img>`、圖片節點、角色圖示在這時就會載入，消毒輸出擋不到。
 * 所以 mermaid 只在一個隱藏的框裡跑：沿用 artifact 預覽的 sandbox（`allow-scripts`，和 Touka 不同來源）與
 * CSP（不准任何外部載入，ADR-0114）。畫好的 SVG 字串經 postMessage 傳回，呼叫端再交給 `sanitizeMermaidSvg`
 * 才放進頁面。框只建一次，各圖共用（載入失敗或某張圖逾時就移除，下次畫圖重建）；只收這個框送來的訊息，
 * 框裡也只收父頁面送來的。
 *
 * 同一張圖（同一個 id）一次只送一個請求進框，串流時內容一直變也不會在框裡排隊排到逾時；每張圖的 15 秒從送進框才開始算。
 */
const RENDER = "touka-mermaid-render";
const RESULT = "touka-mermaid-result";
const READY = "touka-mermaid-ready";
// 框只載入一次，但第一次可能很慢（實測無頭 Edge 偶爾要二十多秒才開始執行框裡的程式），所以和每張圖分開計時
const FRAME_LOAD_TIMEOUT_MS = 60_000;
const RENDER_TIMEOUT_MS = 15_000;
// 隱藏但保留版面：mermaid 量文字尺寸要有版面（display:none 量不到）
const HIDDEN_FRAME_STYLE = "position:fixed;left:-10000px;top:0;width:1024px;height:768px;visibility:hidden;border:0";

const BRIDGE = `(function(){"use strict";
var M=window.mermaid;if(!M)return;
window.addEventListener("message",function(e){
if(e.source!==window.parent)return;var d=e.data;
if(!d||d.type!=="${RENDER}"||typeof d.id!=="string"||typeof d.text!=="string")return;
function reply(r){r.type="${RESULT}";r.request=d.request;window.parent.postMessage(r,"*");}
try{var c=d.config||{};c.secure=Array.from(new Set((c.secure||[]).concat(M.mermaidAPI.defaultConfig.secure||[])));M.initialize(c);}
catch(err){reply({error:String(err)});return;}
M.render(d.id,d.text).then(function(r){reply({svg:r.svg});},function(err){
var left=document.getElementById("d"+d.id);if(left)left.remove();reply({error:String(err&&err.message||err)});});
});
window.parent.postMessage({type:"${READY}"},"*");
})();`;

export function buildMermaidSrcDoc(runtime: string): string {
  return `<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${ARTIFACT_CSP}">
</head><body>
<script>${escapeForScript(runtime)}</script>
<script>${BRIDGE}</script>
</body></html>`;
}

type FrameMessage = { type?: unknown; request?: unknown; svg?: unknown; error?: unknown };

function asMessage(data: unknown): FrameMessage | null {
  return typeof data === "object" && data !== null ? (data as FrameMessage) : null;
}

/** 等的框被移除了（不是這張圖自己逾時）；可以換新框重送一次。 */
class FrameRemovedError extends Error {}

// 每個框上還在等的請求；移除框時一起結束，不讓它們各自等到逾時
const waiting = new Map<HTMLIFrameElement, Set<(err: Error) => void>>();

function removeFrame(element: HTMLIFrameElement): void {
  element.remove();
  const stops = waiting.get(element);
  waiting.delete(element);
  stops?.forEach((stop) => stop(new FrameRemovedError(errorMsg("SYSTEM_ERROR"))));
}

/** 等這個框送來第一個 `accept` 認得的訊息；逾時或框被移除就失敗。 */
function waitFor<T>(
  frame: HTMLIFrameElement,
  timeoutMs: number,
  accept: (message: FrameMessage) => T | undefined,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const stops = waiting.get(frame) ?? new Set<(err: Error) => void>();
    waiting.set(frame, stops);
    const timer = window.setTimeout(() => stop(new Error(errorMsg("SYSTEM_ERROR"))), timeoutMs);
    function finish() {
      window.clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      stops.delete(stop);
    }
    function stop(err: Error) {
      finish();
      reject(err);
    }
    function onMessage(event: MessageEvent) {
      const message = asMessage(event.data);
      const value = event.source === frame.contentWindow && message ? accept(message) : undefined;
      if (value === undefined) return;
      finish();
      resolve(value);
    }
    stops.add(stop);
    window.addEventListener("message", onMessage);
  });
}

let frame: Promise<HTMLIFrameElement> | null = null;
let requests = 0;

type Job = { text: string; callers: Array<{ resolve: (svg: string) => void; reject: (err: unknown) => void }> };
// 有請求在框裡的圖：值是等著送的下一份（只留最新的內容，被取代的呼叫者拿同一個結果），沒有就是 null
const lanes = new Map<string, Job | null>();

function loadFrame(): Promise<HTMLIFrameElement> {
  if (!frame) {
    frame = import("@/generated/mermaidRuntime").then(({ MERMAID_RUNTIME }) => {
      const element = document.createElement("iframe");
      element.setAttribute("sandbox", ARTIFACT_SANDBOX);
      element.setAttribute("aria-hidden", "true");
      element.tabIndex = -1;
      element.style.cssText = HIDDEN_FRAME_STYLE;
      const ready = waitFor(element, FRAME_LOAD_TIMEOUT_MS, (message) => (message.type === READY ? element : undefined));
      element.srcdoc = buildMermaidSrcDoc(MERMAID_RUNTIME);
      document.body.appendChild(element);
      return ready.catch((err: unknown) => {
        removeFrame(element);
        throw err;
      });
    });
    frame.catch(() => {
      frame = null;
    });
  }
  return frame;
}

/** 在沙箱框裡畫一張圖，回傳 mermaid 的 SVG 字串（還沒消毒）。 */
export function renderMermaidInSandbox(id: string, text: string): Promise<string> {
  return new Promise((resolve, reject) => {
    if (!lanes.has(id)) {
      void drain(id, { text, callers: [{ resolve, reject }] });
      return;
    }
    const next = lanes.get(id) ?? { text, callers: [] };
    next.text = text;
    next.callers.push({ resolve, reject });
    lanes.set(id, next);
  });
}

/** 依序畫這張圖：前一個有結果才送下一份。 */
async function drain(id: string, first: Job): Promise<void> {
  let job: Job | null | undefined = first;
  while (job) {
    const { text, callers } = job;
    lanes.set(id, null);
    await renderOnce(id, text).then(
      (svg) => callers.forEach((caller) => caller.resolve(svg)),
      (err: unknown) => callers.forEach((caller) => caller.reject(err)),
    );
    job = lanes.get(id);
  }
  lanes.delete(id);
}

/** 送一個請求進框；框在等的時候被移除就換新框重送，每個請求最多換一次。 */
async function renderOnce(id: string, text: string, switched = false): Promise<string> {
  const loading = loadFrame();
  const element = await loading;
  if (!element.isConnected) {
    if (switched) throw new Error(errorMsg("SYSTEM_ERROR"));
    return renderOnce(id, text, true);
  }
  const request = ++requests;
  const reply = waitFor(element, RENDER_TIMEOUT_MS, (message) =>
    message.type === RESULT && message.request === request ? message : undefined,
  );
  element.contentWindow?.postMessage({ type: RENDER, request, id, text, config: MERMAID_CONFIG }, "*");
  let message: FrameMessage;
  try {
    message = await reply;
  } catch (err) {
    if (err instanceof FrameRemovedError && !switched) return renderOnce(id, text, true);
    // 這張圖自己逾時＝框卡住，之後每張圖都會等到逾時；移除這個框，下次畫圖重建。
    // 已經換過一次框、新框又被移除時也走到這裡，那時框已移除、frame 也已清空，這兩行不會再動到別的框。
    if (frame === loading) frame = null;
    removeFrame(element);
    throw err;
  }
  if (typeof message.svg === "string") return message.svg;
  throw new Error(typeof message.error === "string" ? message.error : errorMsg("SYSTEM_ERROR"));
}
