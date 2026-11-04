import { spawnSync } from "node:child_process";
import path from "node:path";
import { API_ORIGIN, CHAT_ROOT } from "./lib/constants";

// 前端的後端網址在建置時寫死，所以實際驗證要自己建一份（輸出到 gitignore 的 out/）。
function run(script: string, args: string[]): void {
  const result = spawnSync(process.execPath, [path.join(CHAT_ROOT, script), ...args], {
    cwd: CHAT_ROOT,
    stdio: "inherit",
    env: { ...process.env, NEXT_PUBLIC_PROXY_URL: API_ORIGIN, NEXT_TELEMETRY_DISABLED: "1" },
  });
  if (result.status !== 0) process.exit(result.status ?? 1);
}

run("scripts/build-artifact-runtime.mjs", []);
run("node_modules/next/dist/bin/next", ["build"]);
