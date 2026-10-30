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

/** 等這個框送來第一個 `accept` 認得的訊息；逾時就失敗。 */
function waitFor<T>(
  frame: HTMLIFrameElement,
  timeoutMs: number,
  accept: (message: FrameMessage) => T | undefined,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      window.removeEventListener("message", onMessage);
      reject(new Error(errorMsg("SYSTEM_ERROR")));
    }, timeoutMs);
    function onMessage(event: MessageEvent) {
      const message = asMessage(event.data);
      const value = event.source === frame.contentWindow && message ? accept(message) : undefined;
      if (value === undefined) return;
      window.clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      resolve(value);
    }
    window.addEventListener("message", onMessage);
  });
}

let frame: Promise<HTMLIFrameElement> | null = null;
let requests = 0;

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
        element.remove();
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
export async function renderMermaidInSandbox(id: string, text: string): Promise<string> {
  const loading = loadFrame();
  const element = await loading;
  const request = ++requests;
  const reply = waitFor(element, RENDER_TIMEOUT_MS, (message) =>
    message.type === RESULT && message.request === request ? message : undefined,
  );
  element.contentWindow?.postMessage({ type: RENDER, request, id, text, config: MERMAID_CONFIG }, "*");
  const message = await reply.catch((err: unknown) => {
    // 框卡住時之後每張圖都會等到逾時；移除這個框，下次畫圖重建
    if (frame === loading) frame = null;
    element.remove();
    throw err;
  });
  if (typeof message.svg === "string") return message.svg;
  throw new Error(typeof message.error === "string" ? message.error : errorMsg("SYSTEM_ERROR"));
}
