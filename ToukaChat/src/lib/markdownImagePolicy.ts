/**
 * 模型輸出的 Markdown 圖片要不要自動載入（ADR-0294）。
 *
 * 只自動載入 Touka 自己的來源：前端頁面、目前連線的後端（後端是本機時，另一種本機寫法同協定同埠也算）、
 * `data:image/*` 與頁面自己的 `blob:`。其餘不載入，改由呼叫端顯示佔位連結；帶帳號密碼或不是 http(s) 的
 * 網址連結也不給。網址一律先用 `new URL` 解析再比來源，字串開頭判斷擋不住 `//evil`、`/\evil`、夾 tab 的寫法。
 */
export type MarkdownImageDecision =
  | { kind: "load"; src: string }
  | { kind: "blocked"; host: string; href: string | null };

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);
const WEB_PROTOCOLS = new Set(["http:", "https:"]);

function parseUrl(value: string, base?: string): URL | null {
  try {
    return new URL(value, base);
  } catch {
    return null;
  }
}

function isBackendOrigin(url: URL, backend: URL | null): boolean {
  if (!backend || !WEB_PROTOCOLS.has(backend.protocol)) return false;
  if (url.origin === backend.origin) return true;
  return LOOPBACK_HOSTS.has(backend.hostname) && LOOPBACK_HOSTS.has(url.hostname) &&
    url.protocol === backend.protocol && url.port === backend.port;
}

export function decideMarkdownImage(
  src: string | undefined,
  pageOrigin: string,
  backendBase: string,
): MarkdownImageDecision {
  const page = parseUrl(pageOrigin);
  const url = src && page ? parseUrl(src, page.href) : null;
  if (!page || !url) return { kind: "blocked", host: "", href: null };

  if (url.protocol === "data:") {
    return /^data:image\//i.test(url.href) ? { kind: "load", src: url.href } : { kind: "blocked", host: "", href: null };
  }
  if (url.protocol === "blob:") {
    return url.origin !== "null" && url.origin === page.origin
      ? { kind: "load", src: url.href }
      : { kind: "blocked", host: "", href: null };
  }
  if (!WEB_PROTOCOLS.has(url.protocol)) return { kind: "blocked", host: "", href: null };
  if (url.username || url.password) return { kind: "blocked", host: url.host, href: null };

  if (url.origin === page.origin || isBackendOrigin(url, parseUrl(backendBase))) {
    return { kind: "load", src: url.href };
  }
  return { kind: "blocked", host: url.host, href: url.href };
}
