import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { CHAT_ROOT, WEB_ORIGIN } from "./constants";

const WEB_ROOT = path.join(CHAT_ROOT, "out");
const MIME: Record<string, string> = {
  ".html": "text/html",
  ".js": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".woff2": "font/woff2",
  ".wasm": "application/wasm",
  ".ico": "image/x-icon",
  ".txt": "text/plain",
};

function resolveFile(urlPath: string): string | null {
  for (const candidate of [urlPath, `${urlPath}.html`, path.posix.join(urlPath, "index.html")]) {
    const file = path.join(WEB_ROOT, candidate);
    const inside = !path.relative(WEB_ROOT, file).startsWith("..");
    if (inside && fs.existsSync(file) && fs.statSync(file).isFile()) return file;
  }
  return null;
}

/** 送打包好的正式版畫面（`out/`），不是開發模式。 */
export function createStaticWeb(): http.Server {
  if (!fs.existsSync(path.join(WEB_ROOT, "index.html"))) {
    throw new Error(`${WEB_ROOT} has no build; run npm run test:e2e:real (it builds first)`);
  }
  return http.createServer((req, res) => {
    const file = resolveFile(decodeURIComponent(new URL(req.url ?? "/", WEB_ORIGIN).pathname));
    if (!file) {
      res.statusCode = 404;
      return res.end("not found");
    }
    res.setHeader("content-type", MIME[path.extname(file)] ?? "application/octet-stream");
    fs.createReadStream(file).pipe(res);
  });
}
