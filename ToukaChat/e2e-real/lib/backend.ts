import { execFileSync, spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  API_ORIGIN,
  API_PORT,
  BACKEND_LOG,
  CANARY_API_KEY,
  FAKE_MODEL_ORIGIN,
  REPO_ROOT,
  WEB_ORIGIN,
} from "./constants";

const START_TIMEOUT_MS = 240_000;
const STOP_TIMEOUT_MS = 60_000;
const POLL_MS = 500;
const IS_WINDOWS = process.platform === "win32";
const SANDBOX_MARKER = "SANDBOX_ROOT=";
const SANDBOX_PREFIX = "touka_smoke_e2e_real_";

export interface RunningBackend {
  sandboxRoot: string;
  stop: () => Promise<void>;
}

function venvPython(): string {
  return path.join(REPO_ROOT, ".venv", ...(IS_WINDOWS ? ["Scripts", "python.exe"] : ["bin", "python"]));
}

/** 只接受系統暫存裡、這個環境自己建的資料夾；其他值一律不用，更不會刪。 */
function checkedSandboxRoot(raw: string): string {
  const root = fs.realpathSync(raw);
  const inTemp = path.dirname(root) === fs.realpathSync(os.tmpdir());
  if (!inTemp || !path.basename(root).startsWith(SANDBOX_PREFIX)) {
    throw new Error(`Backend reported an unexpected sandbox root: ${root}`);
  }
  return root;
}

function forceKill(child: ChildProcessWithoutNullStreams): void {
  if (child.pid === undefined) return;
  try {
    if (IS_WINDOWS) execFileSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
    else process.kill(-child.pid, "SIGKILL");
  } catch {
    // 已經結束了
  }
}

function waitForExit(child: ChildProcessWithoutNullStreams, timeoutMs: number): Promise<number | null | "timeout"> {
  if (child.exitCode !== null) return Promise.resolve(child.exitCode);
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve("timeout"), timeoutMs);
    child.once("exit", (code) => {
      clearTimeout(timer);
      resolve(code);
    });
  });
}

async function waitForHealth(child: ChildProcessWithoutNullStreams): Promise<void> {
  const deadline = Date.now() + START_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`Backend exited with ${child.exitCode}; see ${BACKEND_LOG}`);
    try {
      // eslint-disable-next-line no-restricted-globals -- 測試直接打後端，沒有前端的 fetchTouka
      if ((await fetch(`${API_ORIGIN}/health`)).ok) return;
    } catch {
      // 還沒起來
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
  throw new Error(`Backend did not become healthy; see ${BACKEND_LOG}`);
}

/**
 * 正常結束靠關掉標準輸入：後端的 atexit 清沙盒、把外連違規變成非 0 結束碼，所以這裡看結束碼。
 * 逾時才強制結束，此時算失敗，也只刪後端自己印出來的沙盒路徑。
 */
async function stopBackend(child: ChildProcessWithoutNullStreams, sandboxRoot: string, log: fs.WriteStream): Promise<void> {
  child.stdin.end();
  const code = await waitForExit(child, STOP_TIMEOUT_MS);
  log.end();
  if (code === "timeout") {
    forceKill(child);
    fs.rmSync(sandboxRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 });
    throw new Error(`Backend did not stop in time; see ${BACKEND_LOG}`);
  }
  if (code !== 0) {
    throw new Error(`Backend exited with ${code} (external network attempt or sandbox cleanup failure); see ${BACKEND_LOG}`);
  }
}

export async function startBackend(): Promise<RunningBackend> {
  const script = path.join(REPO_ROOT, "tests", "e2e", "serve_real_backend.py");
  const child = spawn(
    venvPython(),
    [
      script,
      "--api-port", String(API_PORT),
      "--fake-model-url", `${FAKE_MODEL_ORIGIN}/v1`,
      "--fake-model-api-key", CANARY_API_KEY,
      "--web-origin", WEB_ORIGIN,
    ],
    {
      cwd: path.join(REPO_ROOT, "ToukaProxy"),
      detached: !IS_WINDOWS,
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
    },
  );
  const log = fs.createWriteStream(BACKEND_LOG);
  child.stderr.on("data", (data: Buffer) => log.write(data));
  const sandboxLine = new Promise<string>((resolve, reject) => {
    let seen = "";
    child.once("exit", (code) => reject(new Error(`Backend exited with ${code}; see ${BACKEND_LOG}`)));
    child.stdout.on("data", (data: Buffer) => {
      log.write(data);
      seen += data.toString("utf8");
      const line = seen.split(/\r?\n/).find((text) => text.startsWith(SANDBOX_MARKER));
      if (line) resolve(line.slice(SANDBOX_MARKER.length));
    });
  });
  try {
    const sandboxRoot = checkedSandboxRoot(await sandboxLine);
    await waitForHealth(child);
    return { sandboxRoot, stop: () => stopBackend(child, sandboxRoot, log) };
  } catch (error) {
    forceKill(child);
    throw error;
  }
}
