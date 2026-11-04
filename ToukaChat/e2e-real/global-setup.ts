import crypto from "node:crypto";
import fs from "node:fs";
import type http from "node:http";
import net from "node:net";
import path from "node:path";
import { adminLogin, createSoul, replaceInitialPassword } from "./lib/api";
import { startBackend } from "./lib/backend";
import {
  API_PORT,
  BACKEND_LOG,
  ENV_EMAIL,
  ENV_PASSWORD,
  ENV_SANDBOX_ROOT,
  FAKE_MODEL_PORT,
  HOOK_SINK_PORT,
  LOOPBACK,
  OUTPUT_DIR,
  SHOTS_DIR,
  WEB_PORT,
} from "./lib/constants";
import { createFakeModel } from "./lib/fake_model";
import { createHookSink } from "./lib/hook_sink";
import { createStaticWeb } from "./lib/static_web";

function listen(server: http.Server, port: number): Promise<void> {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, LOOPBACK, () => resolve());
  });
}

/** 後端的埠先確認空著：別的程式占著時 health 會回應，測試就打到別人身上。 */
async function assertPortFree(port: number): Promise<void> {
  const probe = net.createServer();
  await new Promise<void>((resolve, reject) => {
    probe.once("error", () => reject(new Error(`Port ${port} is in use; stop that program or free the port`)));
    probe.listen(port, LOOPBACK, () => probe.close(() => resolve()));
  });
}

/** 回傳這次用過的兩個密碼，teardown 要確認沒有落到記錄檔。 */
async function prepareAccount(sandboxRoot: string): Promise<string[]> {
  const raw = fs.readFileSync(path.join(sandboxRoot, "config", "INITIAL_ADMIN_PASSWORD.txt"), "utf8");
  const email = raw.match(/email:\s*(\S+)/)?.[1];
  const initial = raw.match(/password:\s*(\S+)/)?.[1];
  if (!email || !initial) throw new Error("INITIAL_ADMIN_PASSWORD.txt has no email or password");
  const password = `${crypto.randomBytes(12).toString("base64url")}aA1!`;
  await replaceInitialPassword(email, initial, password);
  process.env[ENV_EMAIL] = email;
  process.env[ENV_PASSWORD] = password;
  await createSoul(await adminLogin());
  return [initial, password];
}

/** backend.log 會傳成 CI 產物，測試帳號的密碼也不能在裡面。訊息不印密碼本身。 */
function assertLogHasNoPassword(passwords: string[]): void {
  const log = fs.readFileSync(BACKEND_LOG, "utf8");
  if (passwords.some((password) => log.includes(password))) {
    throw new Error(`${BACKEND_LOG} contains a test account password`);
  }
}

/** 起假模型、hook 接收端、靜態前端與真後端；回傳的函式是 global teardown。 */
export default async function globalSetup(): Promise<() => Promise<void>> {
  fs.rmSync(SHOTS_DIR, { recursive: true, force: true });
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  const servers: [http.Server, number][] = [
    [createFakeModel(), FAKE_MODEL_PORT],
    [createHookSink(), HOOK_SINK_PORT],
    [createStaticWeb(), WEB_PORT],
  ];
  await assertPortFree(API_PORT);
  await Promise.all(servers.map(([server, port]) => listen(server, port)));
  const closeServers = () => {
    for (const [server] of servers) server.close();
  };
  try {
    const backend = await startBackend();
    process.env[ENV_SANDBOX_ROOT] = backend.sandboxRoot;
    let passwords: string[];
    try {
      passwords = await prepareAccount(backend.sandboxRoot);
    } catch (error) {
      await backend.stop().catch(() => undefined);
      throw error;
    }
    return async () => {
      closeServers();
      try {
        await backend.stop();
      } finally {
        // 後端失敗時最需要看記錄檔，記錄檔也照樣會傳出去，所以一樣要查
        assertLogHasNoPassword(passwords);
      }
    };
  } catch (error) {
    closeServers();
    throw error;
  }
}
