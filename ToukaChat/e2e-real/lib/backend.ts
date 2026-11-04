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
const FORCE_KILL_WAIT_MS = 10_000;
const POLL_MS = 500;
const IS_WINDOWS = process.platform === "win32";
const SANDBOX_MARKER = "SANDBOX_ROOT=";
const SANDBOX_PREFIX = "touka_smoke_e2e_real_";
// tests/network_guard.py 的 format_violations 開頭
const NETWORK_VIOLATION_MARK = "自動化測試企圖連到外部網路";

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

async function closedWithin(closed: Promise<number | null>, timeoutMs: number): Promise<number | null | "timeout"> {
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<"timeout">((resolve) => {
    timer = setTimeout(() => resolve("timeout"), timeoutMs);
  });
  try {
    return await Promise.race([closed, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

function endLog(log: fs.WriteStream): Promise<void> {
  return new Promise((resolve) => log.end(resolve));
}

function removeSandbox(sandboxRoot: string): void {
  fs.rmSync(sandboxRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 });
}

/** 結束碼不是 0 時帶出原因：網路防護在結束前才印這段，有這段才算證明是外連。 */
function nonZeroExitReason(): string {
  const log = fs.readFileSync(BACKEND_LOG, "utf8");
  const start = log.indexOf(NETWORK_VIOLATION_MARK);
  return start >= 0 ? log.slice(start).trim() : `no external network attempt in the log; see ${BACKEND_LOG}`;
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
 * 等 close 不是 exit：輸出讀完才關記錄檔，違規訊息才不會漏掉。
 * 逾時才強制結束，此時算失敗，也只刪後端自己印出來的沙盒路徑。
 */
async function stopBackend(
  child: ChildProcessWithoutNullStreams,
  closed: Promise<number | null>,
  sandboxRoot: string,
  log: fs.WriteStream,
): Promise<void> {
  child.stdin.end();
  const code = await closedWithin(closed, STOP_TIMEOUT_MS);
  if (code === "timeout") {
    forceKill(child);
    await closedWithin(closed, FORCE_KILL_WAIT_MS);
    await endLog(log);
    removeSandbox(sandboxRoot);
    throw new Error(`Backend did not stop in time; see ${BACKEND_LOG}`);
  }
  await endLog(log);
  if (code !== 0) throw new Error(`Backend exited with ${code}: ${nonZeroExitReason()}`);
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
  const closed = new Promise<number | null>((resolve) => child.once("close", resolve));
  const log = fs.createWriteStream(BACKEND_LOG);
  // 記錄檔出問題不蓋掉測試結果，但要看得到；強制結束後才到的輸出直接丟掉
  log.on("error", (error) => console.error(`Cannot write ${BACKEND_LOG}: ${error.message}`));
  const writeLog = (data: Buffer) => {
    if (!log.writableEnded) log.write(data);
  };
  child.stderr.on("data", writeLog);
  const sandboxLine = new Promise<string>((resolve, reject) => {
    let seen = "";
    void closed.then((code) => reject(new Error(`Backend exited with ${code}; see ${BACKEND_LOG}`)));
    child.stdout.on("data", (data: Buffer) => {
      writeLog(data);
      seen += data.toString("utf8");
      const line = seen.split(/\r?\n/).find((text) => text.startsWith(SANDBOX_MARKER));
      if (line) resolve(line.slice(SANDBOX_MARKER.length));
    });
  });
  let startedRoot: string | undefined;
  try {
    const sandboxRoot = checkedSandboxRoot(await sandboxLine);
    startedRoot = sandboxRoot;
    await waitForHealth(child);
    return { sandboxRoot, stop: () => stopBackend(child, closed, sandboxRoot, log) };
  } catch (error) {
    forceKill(child);
    await closedWithin(closed, FORCE_KILL_WAIT_MS);
    await endLog(log);
    if (startedRoot) {
      try {
        removeSandbox(startedRoot);
      } catch (cleanupError) {
        throw new AggregateError([error, cleanupError], "Backend failed to start and its sandbox was not removed");
      }
    }
    throw error;
  }
}
