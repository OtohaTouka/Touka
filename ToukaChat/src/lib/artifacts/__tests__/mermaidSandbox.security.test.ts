import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { runInNewContext } from "node:vm";
import { buildMermaidSrcDoc } from "../mermaidSandbox";
import { MERMAID_CONFIG } from "../mermaidConfig";
import { ARTIFACT_CSP } from "../sandbox";
import { errorMsg } from "@/lib/error_handler";

/**
 * ADR-0294：mermaid 畫圖時就會載入圖裡的外部圖片，所以只在沙箱框裡畫
 * （artifact 預覽的 sandbox 與 CSP，ADR-0114）。頁面與框之間只收對方送來、對得上的訊息。
 */
vi.mock("@/generated/mermaidRuntime", () => ({
  MERMAID_RUNTIME: "window.loaded = '</script><img src=x>';",
}));

const RENDER = "touka-mermaid-render";
const RESULT = "touka-mermaid-result";
const READY = "touka-mermaid-ready";

describe("buildMermaidSrcDoc", () => {
  it("puts the artifact CSP first and escapes the runtime", () => {
    const doc = buildMermaidSrcDoc("a='</script><script>alert(1)'");
    expect(doc).toContain(`<meta http-equiv="Content-Security-Policy" content="${ARTIFACT_CSP}">`);
    expect(doc.indexOf("Content-Security-Policy")).toBeLessThan(doc.indexOf("<script>"));
    expect(doc).not.toContain("</script><script>alert");
    expect(doc.match(/<\/script>/g)).toHaveLength(2);
  });

  it("allows no external loading in the frame, even if the shared artifact CSP is loosened", () => {
    // 流程圖框和 artifact 預覽共用 ARTIFACT_CSP；這裡寫死流程圖框要的值，artifact 那邊放寬時這裡會紅
    const csp = buildMermaidSrcDoc("").match(/http-equiv="Content-Security-Policy" content="([^"]*)"/)?.[1] ?? "";
    const entries = csp.split(";").map((part) => part.trim()).filter(Boolean).map((part) => {
      const [name, ...values] = part.split(/\s+/);
      return [name.toLowerCase(), values.join(" ")];
    });
    // 同一條指令寫兩次時瀏覽器只認第一條，下面的整份比對卻只看得到最後一條，所以先擋重複
    expect(new Set(entries.map(([name]) => name)).size).toBe(entries.length);
    const directives = Object.fromEntries(entries);
    expect(directives).toEqual({
      "default-src": "'none'",
      "script-src": "'unsafe-inline'",
      "style-src": "'unsafe-inline'",
      "img-src": "data: blob:",
      "font-src": "data:",
      "media-src": "data: blob:",
      "connect-src": "'none'",
      "form-action": "'none'",
      "base-uri": "'none'",
    });
  });
});

// 框裡的橋接程式：取出 srcdoc 的第二段 script，在只有假 window／document 的環境執行
const BRIDGE = [...buildMermaidSrcDoc("").matchAll(/<script>([\s\S]*?)<\/script>/g)][1][1];

type FakeMermaid = {
  initialize: ReturnType<typeof vi.fn>;
  render: ReturnType<typeof vi.fn>;
  mermaidAPI: { defaultConfig: { secure: string[] } };
};

function runBridge(mermaid: FakeMermaid) {
  const listeners: Array<(event: { source: unknown; data: unknown }) => void> = [];
  const parent = { postMessage: vi.fn() };
  const removed: string[] = [];
  const document = { getElementById: (id: string) => ({ remove: () => removed.push(id) }) };
  const window = {
    mermaid,
    parent,
    addEventListener: (type: string, listener: (typeof listeners)[number]) => {
      if (type === "message") listeners.push(listener);
    },
  };
  runInNewContext(BRIDGE, { window, document });
  const send = (data: unknown, source: unknown = parent) => listeners.forEach((listener) => listener({ source, data }));
  return { parent, removed, send };
}

function fakeMermaid(render: () => Promise<unknown>): FakeMermaid {
  return {
    initialize: vi.fn(),
    render: vi.fn(render),
    mermaidAPI: { defaultConfig: { secure: ["securityLevel", "futureKey"] } },
  };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("mermaid sandbox frame — bridge inside the frame", () => {
  it("tells the parent page it is ready", () => {
    const { parent } = runBridge(fakeMermaid(() => Promise.resolve({ svg: "" })));
    expect(parent.postMessage).toHaveBeenCalledWith({ type: READY }, "*");
  });

  it("ignores requests that are not from the parent page or are malformed", async () => {
    const mermaid = fakeMermaid(() => Promise.resolve({ svg: "<svg/>" }));
    const { send } = runBridge(mermaid);
    send({ type: RENDER, request: 1, id: "m1", text: "graph TD" }, {});
    send({ type: "other", request: 1, id: "m1", text: "graph TD" });
    send({ type: RENDER, request: 1, id: 1, text: "graph TD" });
    send({ type: RENDER, request: 1, id: "m1", text: null });
    await flush();
    expect(mermaid.initialize).not.toHaveBeenCalled();
    expect(mermaid.render).not.toHaveBeenCalled();
  });

  it("keeps the running version's protected keys, renders, and replies with the request number", async () => {
    const mermaid = fakeMermaid(() => Promise.resolve({ svg: "<svg/>" }));
    const { parent, send } = runBridge(mermaid);
    send({ type: RENDER, request: 7, id: "m1", text: "graph TD", config: { securityLevel: "strict", secure: ["themeCSS"] } });
    await flush();
    const [config] = mermaid.initialize.mock.calls[0];
    expect(config.securityLevel).toBe("strict");
    expect([...config.secure].sort()).toEqual(["futureKey", "securityLevel", "themeCSS"]);
    expect(mermaid.render).toHaveBeenCalledWith("m1", "graph TD");
    expect(parent.postMessage).toHaveBeenLastCalledWith({ svg: "<svg/>", type: RESULT, request: 7 }, "*");
  });

  it("removes mermaid's leftover element and reports the error", async () => {
    const mermaid = fakeMermaid(() => Promise.reject(new Error("bad diagram")));
    const { parent, removed, send } = runBridge(mermaid);
    send({ type: RENDER, request: 3, id: "m1", text: "graph" });
    await flush();
    expect(removed).toEqual(["dm1"]);
    expect(parent.postMessage).toHaveBeenLastCalledWith({ error: "bad diagram", type: RESULT, request: 3 }, "*");
  });
});

describe("mermaid sandbox frame — parent page", () => {
  let renderMermaidInSandbox: typeof import("../mermaidSandbox").renderMermaidInSandbox;

  beforeEach(async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.resetModules();
    document.body.innerHTML = "";
    ({ renderMermaidInSandbox } = await import("../mermaidSandbox"));
  });

  afterEach(() => {
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  async function openFrame(count = 1): Promise<HTMLIFrameElement> {
    await vi.waitFor(() => expect(document.querySelectorAll("iframe")).toHaveLength(count));
    return document.querySelectorAll("iframe")[count - 1];
  }

  function message(data: unknown, source: Window | null) {
    window.dispatchEvent(new MessageEvent("message", { data, source }));
  }

  it("creates one hidden frame with the artifact sandbox and CSP", async () => {
    void renderMermaidInSandbox("m1", "graph TD");
    const frame = await openFrame();
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame.srcdoc).toContain(`content="${ARTIFACT_CSP}"`);
    expect(frame.srcdoc).not.toContain("</script><img");
    expect(frame.getAttribute("aria-hidden")).toBe("true");
    expect(frame.style.visibility).toBe("hidden");
  });

  it("sends the diagram only after the frame is ready and takes only that frame's matching reply", async () => {
    const result = renderMermaidInSandbox("m1", "graph TD; A-->B");
    const frame = await openFrame();
    const post = vi.spyOn(frame.contentWindow as Window, "postMessage");

    message({ type: READY }, window);
    await Promise.resolve();
    expect(post).not.toHaveBeenCalled();

    message({ type: READY }, frame.contentWindow);
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith(
      { type: RENDER, request: 1, id: "m1", text: "graph TD; A-->B", config: MERMAID_CONFIG },
      "*",
    );

    message({ type: RESULT, request: 1, svg: "<svg>forged</svg>" }, window);
    message({ type: RESULT, request: 2, svg: "<svg>other</svg>" }, frame.contentWindow);
    message({ type: RESULT, request: 1, svg: "<svg>real</svg>" }, frame.contentWindow);
    await expect(result).resolves.toBe("<svg>real</svg>");
  });

  it("shares one frame between diagrams and matches replies by request number", async () => {
    const first = renderMermaidInSandbox("a", "graph A");
    const second = renderMermaidInSandbox("b", "graph B");
    const frame = await openFrame();
    const post = vi.spyOn(frame.contentWindow as Window, "postMessage");
    message({ type: READY }, frame.contentWindow);
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(document.querySelectorAll("iframe")).toHaveLength(1);

    message({ type: RESULT, request: 2, svg: "<svg>B</svg>" }, frame.contentWindow);
    message({ type: RESULT, request: 1, svg: "<svg>A</svg>" }, frame.contentWindow);
    await expect(first).resolves.toBe("<svg>A</svg>");
    await expect(second).resolves.toBe("<svg>B</svg>");
  });

  it("rejects with the frame's error, or a generic error when the reply has neither", async () => {
    const bad = renderMermaidInSandbox("m1", "graph");
    const empty = renderMermaidInSandbox("m2", "graph");
    const frame = await openFrame();
    const post = vi.spyOn(frame.contentWindow as Window, "postMessage");
    message({ type: READY }, frame.contentWindow);
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(2));

    message({ type: RESULT, request: 1, error: "Parse error" }, frame.contentWindow);
    message({ type: RESULT, request: 2 }, frame.contentWindow);
    await expect(bad).rejects.toThrow("Parse error");
    await expect(empty).rejects.toThrow(errorMsg("SYSTEM_ERROR"));
  });

  it("waits up to a minute for the frame to load, then removes it and builds a new one next time", async () => {
    const result = renderMermaidInSandbox("m1", "graph");
    const frame = await openFrame();
    let settled = false;
    result.catch(() => undefined).finally(() => (settled = true));
    vi.advanceTimersByTime(30_000);
    await Promise.resolve();
    expect(settled).toBe(false);
    expect(frame.isConnected).toBe(true);

    const failed = expect(result).rejects.toThrow(errorMsg("SYSTEM_ERROR"));
    vi.advanceTimersByTime(30_000);
    await failed;
    expect(frame.isConnected).toBe(false);

    void renderMermaidInSandbox("m2", "graph");
    const next = await openFrame();
    expect(next).not.toBe(frame);
  });

  it("gives up on a diagram the loaded frame never answers, then removes the frame and builds a new one next time", async () => {
    const result = renderMermaidInSandbox("m1", "graph");
    const frame = await openFrame();
    const post = vi.spyOn(frame.contentWindow as Window, "postMessage");
    message({ type: READY }, frame.contentWindow);
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const failed = expect(result).rejects.toThrow(errorMsg("SYSTEM_ERROR"));
    vi.advanceTimersByTime(15_000);
    await failed;
    expect(frame.isConnected).toBe(false);

    void renderMermaidInSandbox("m2", "graph");
    const next = await openFrame();
    expect(next).not.toBe(frame);
  });
});

describe("mermaid sandbox frame — one request per diagram, frame removal", () => {
  let renderMermaidInSandbox: typeof import("../mermaidSandbox").renderMermaidInSandbox;

  beforeEach(async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.resetModules();
    document.body.innerHTML = "";
    ({ renderMermaidInSandbox } = await import("../mermaidSandbox"));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    document.body.innerHTML = "";
  });

  async function openFrame(): Promise<HTMLIFrameElement> {
    await vi.waitFor(() => expect(document.querySelectorAll("iframe")).toHaveLength(1));
    return document.querySelectorAll("iframe")[0];
  }

  /** 開框、送 READY，回傳框與它收到的請求（spy 在 READY 之前裝好）。 */
  async function readyFrame() {
    const frame = await openFrame();
    const post = vi.spyOn(frame.contentWindow as Window, "postMessage");
    message({ type: READY }, frame.contentWindow);
    return { frame, post };
  }

  function message(data: unknown, source: Window | null) {
    window.dispatchEvent(new MessageEvent("message", { data, source }));
  }

  const settle = async () => {
    for (let i = 0; i < 20; i++) await Promise.resolve();
  };

  function sent(post: { mock: { calls: unknown[][] } }, index: number) {
    return post.mock.calls[index][0] as { request: number; id: string; text: string };
  }

  function watch(promise: Promise<unknown>) {
    const state = { settled: false };
    promise.catch(() => undefined).finally(() => (state.settled = true));
    return state;
  }

  it("sends one request per diagram at a time; newer text replaces the waiting one and every caller gets a result", async () => {
    const first = renderMermaidInSandbox("m1", "graph 1");
    const { frame, post } = await readyFrame();
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1));

    const second = renderMermaidInSandbox("m1", "graph 2");
    const third = renderMermaidInSandbox("m1", "graph 3");
    await settle();
    expect(post).toHaveBeenCalledTimes(1);

    message({ type: RESULT, request: 1, svg: "<svg>1</svg>" }, frame.contentWindow);
    await expect(first).resolves.toBe("<svg>1</svg>");
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(post).toHaveBeenLastCalledWith({ type: RENDER, request: 2, id: "m1", text: "graph 3", config: MERMAID_CONFIG }, "*");

    message({ type: RESULT, request: 2, svg: "<svg>3</svg>" }, frame.contentWindow);
    await expect(second).resolves.toBe("<svg>3</svg>");
    await expect(third).resolves.toBe("<svg>3</svg>");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("starts a waiting request's 15 seconds when it is sent, not when it was asked for", async () => {
    // 假計時器下 vi.waitFor 每次檢查都會推進時間；這個測試要算準時間，送出後一律用 settle() 再檢查
    const first = renderMermaidInSandbox("m1", "graph 1");
    const { frame, post } = await readyFrame();
    await settle();
    expect(post).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(2_000);
    const second = renderMermaidInSandbox("m1", "graph 2");
    await settle();
    vi.advanceTimersByTime(8_000);
    message({ type: RESULT, request: 1, svg: "<svg>1</svg>" }, frame.contentWindow);
    await expect(first).resolves.toBe("<svg>1</svg>");
    await settle();
    expect(post).toHaveBeenCalledTimes(2);

    const state = watch(second);
    vi.advanceTimersByTime(14_000);
    await settle();
    expect(state.settled).toBe(false);
    message({ type: RESULT, request: sent(post, 1).request, svg: "<svg>2</svg>" }, frame.contentWindow);
    await expect(second).resolves.toBe("<svg>2</svg>");
  });

  it("gives every replaced caller the error when the newest text fails", async () => {
    const first = renderMermaidInSandbox("m1", "graph 1");
    const { frame, post } = await readyFrame();
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const second = renderMermaidInSandbox("m1", "graph 2");
    const third = renderMermaidInSandbox("m1", "graph 3");
    message({ type: RESULT, request: 1, svg: "<svg>1</svg>" }, frame.contentWindow);
    await expect(first).resolves.toBe("<svg>1</svg>");
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(2));

    message({ type: RESULT, request: 2, error: "Parse error" }, frame.contentWindow);
    await expect(second).rejects.toThrow("Parse error");
    await expect(third).rejects.toThrow("Parse error");
  });

  it("does not let a failed request block the next one for the same diagram", async () => {
    const bad = renderMermaidInSandbox("m1", "graph");
    const { frame, post } = await readyFrame();
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    message({ type: RESULT, request: 1, error: "Parse error" }, frame.contentWindow);
    await expect(bad).rejects.toThrow("Parse error");

    const good = renderMermaidInSandbox("m1", "graph TD");
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    message({ type: RESULT, request: 2, svg: "<svg>ok</svg>" }, frame.contentWindow);
    await expect(good).resolves.toBe("<svg>ok</svg>");
  });

  it("resends a diagram in a new frame at once when another diagram's timeout removes the frame", async () => {
    const added = vi.spyOn(window, "addEventListener");
    const removed = vi.spyOn(window, "removeEventListener");
    const slow = renderMermaidInSandbox("a", "graph A");
    const { frame, post } = await readyFrame();
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1));

    vi.advanceTimersByTime(10_000);
    const other = renderMermaidInSandbox("b", "graph B");
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(2));

    const slowFailed = expect(slow).rejects.toThrow(errorMsg("SYSTEM_ERROR"));
    vi.advanceTimersByTime(5_000);
    await slowFailed;
    expect(frame.isConnected).toBe(false);

    // 不必等到 b 自己的 15 秒：框一移除就在新框重送
    const { frame: next, post: nextPost } = await readyFrame();
    expect(next).not.toBe(frame);
    await vi.waitFor(() => expect(nextPost).toHaveBeenCalledTimes(1));
    expect(sent(nextPost, 0)).toMatchObject({ id: "b", text: "graph B" });
    message({ type: RESULT, request: sent(nextPost, 0).request, svg: "<svg>B</svg>" }, next.contentWindow);
    await expect(other).resolves.toBe("<svg>B</svg>");

    // 舊框被移除不影響新框上的請求
    const later = renderMermaidInSandbox("c", "graph C");
    await vi.waitFor(() => expect(nextPost).toHaveBeenCalledTimes(2));
    message({ type: RESULT, request: sent(nextPost, 1).request, svg: "<svg>C</svg>" }, next.contentWindow);
    await expect(later).resolves.toBe("<svg>C</svg>");

    expect(vi.getTimerCount()).toBe(0);
    const count = (spy: typeof added) => spy.mock.calls.filter(([type]) => type === "message").length;
    expect(count(removed)).toBe(count(added));
  });

  it("switches frames at most once per request", async () => {
    const a = renderMermaidInSandbox("a", "graph A");
    const { post } = await readyFrame();
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    vi.advanceTimersByTime(5_000);
    const c = renderMermaidInSandbox("c", "graph C");
    vi.advanceTimersByTime(5_000);
    const b = renderMermaidInSandbox("b", "graph B");
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(3));

    // a 逾時移除第一個框；c、b 都換到第二個框（c 先送）
    vi.advanceTimersByTime(5_000);
    await expect(a).rejects.toThrow(errorMsg("SYSTEM_ERROR"));
    const { post: secondPost } = await readyFrame();
    await vi.waitFor(() => expect(secondPost).toHaveBeenCalledTimes(2));
    expect(sent(secondPost, 0).id).toBe("c");
    expect(sent(secondPost, 1).id).toBe("b");

    // c 在第二個框逾時、移除這個框：b 已經換過一次框，不再換第三個
    const bFailed = expect(b).rejects.toThrow(errorMsg("SYSTEM_ERROR"));
    const cFailed = expect(c).rejects.toThrow(errorMsg("SYSTEM_ERROR"));
    await vi.advanceTimersByTimeAsync(15_000);
    await bFailed;
    await cFailed;
    await settle();
    expect(document.querySelectorAll("iframe")).toHaveLength(0);
  });

  it("uses a new frame when the frame it waited for was removed before it could send", async () => {
    const a = renderMermaidInSandbox("a", "graph A");
    const { frame, post } = await readyFrame();
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1));

    // a 逾時的同一刻要畫 b：b 拿到的是馬上就被移除的框
    const aFailed = expect(a).rejects.toThrow(errorMsg("SYSTEM_ERROR"));
    vi.advanceTimersByTime(15_000);
    const b = renderMermaidInSandbox("b", "graph B");
    await aFailed;
    expect(frame.isConnected).toBe(false);

    const { frame: next, post: nextPost } = await readyFrame();
    await vi.waitFor(() => expect(nextPost).toHaveBeenCalledTimes(1));
    expect(sent(nextPost, 0)).toMatchObject({ id: "b", text: "graph B" });
    message({ type: RESULT, request: sent(nextPost, 0).request, svg: "<svg>B</svg>" }, next.contentWindow);
    await expect(b).resolves.toBe("<svg>B</svg>");
  });
});
