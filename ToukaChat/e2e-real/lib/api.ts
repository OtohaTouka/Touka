import path from "node:path";
import {
  API_ORIGIN,
  ENV_EMAIL,
  ENV_HEARTBEAT_TOKEN,
  ENV_PASSWORD,
  ENV_SANDBOX_ROOT,
  FAKE_MODEL_ID,
  MCP_TOOL,
  SOUL_ID,
  WEB_ORIGIN,
} from "./constants";

export interface HookRunRow {
  at: number;
  event: string;
  hook: string;
  hook_type: string;
  outcome: string;
  detail: string | null;
  incognito: boolean;
}

async function call<T>(method: string, url: string, token: string | null, body?: unknown): Promise<T> {
  // eslint-disable-next-line no-restricted-globals -- 測試直接打後端，沒有前端的 fetchTouka
  const response = await fetch(API_ORIGIN + url, {
    method,
    headers: {
      "content-type": "application/json",
      // 寫入端點查 CSRF Origin；前端來源是後端信任的 UI_DEFAULT_URL
      origin: WEB_ORIGIN,
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${url} returned ${response.status}: ${text.slice(0, 300)}`);
  return (text ? JSON.parse(text) : {}) as T;
}

export async function login(email: string, password: string): Promise<string> {
  const body = await call<{ access_token: string }>("POST", "/v1/auth/login", null, { email, password });
  return body.access_token;
}

/** 這個帳號在 global-setup 準備好；瀏覽器登入之前才能用（再登入會換掉瀏覽器的 refresh token）。 */
export function adminLogin(): Promise<string> {
  return login(process.env[ENV_EMAIL] ?? "", process.env[ENV_PASSWORD] ?? "");
}

/** 把沙盒的一次性管理員密碼換成這次產生的密碼。 */
export async function replaceInitialPassword(email: string, current: string, next: string): Promise<void> {
  const token = await login(email, current);
  await call("POST", "/v1/auth/change-password", token, { current_password: current, new_password: next });
}

/** 新靈魂預設 Base 沒有工具，要指定 Owner 才看得到工具。 */
export async function createSoul(token: string, soulId: string = SOUL_ID): Promise<void> {
  await call("POST", "/v1/souls", token, {
    agent_id: soulId,
    name: soulId,
    persona: "實際驗證用",
    config: {
      base_model: FAKE_MODEL_ID,
      enabled_tools: ["read", "write", "list", "ask_user", "dispatch_subagent", "mission_tools", MCP_TOOL],
    },
  });
  const identity = await call<{ agent_overrides?: Record<string, unknown> }>("GET", "/v1/identity", token);
  await call("POST", "/v1/identity", token, {
    ...identity,
    agent_overrides: {
      ...identity.agent_overrides,
      [soulId]: { role: "Owner", capabilities: ["heartbeat_awakening"] },
    },
  });
}

/** 新的一般帳號，擁有這些靈魂。回傳帳號 ID。 */
export async function createUser(adminToken: string, email: string, password: string, ownedAgents: string[]): Promise<string> {
  const user = await call<{ id: string }>("POST", "/v1/admin/users", adminToken, { email, password, role: "normal" });
  await call("PATCH", `/v1/admin/users/${user.id}`, adminToken, { owned_agents: ownedAgents });
  return user.id;
}

export interface ChatSeed {
  id: string;
  title: string;
  agentId?: string;
  updatedAt?: number;
  projectId?: string;
}

/** 直接建好一段對話，不經畫面：要先拿到「還不存在」的版本才存得進去。 */
export async function createChat(token: string, chat: ChatSeed): Promise<void> {
  const snapshot = await call<{ revision: string }>("GET", `/v1/ui/sessions/${chat.id}?snapshot=true`, token);
  await call("POST", `/v1/ui/sessions/${chat.id}`, token, {
    id: chat.id,
    title: chat.title,
    messages: { messages: {}, currentId: null },
    updatedAt: chat.updatedAt ?? 1,
    agent_id: chat.agentId,
    project_id: chat.projectId,
    revision: snapshot.revision,
  });
}

export async function setChatHome(token: string, sessionId: string, agentId: string): Promise<void> {
  await call("POST", "/v1/ui/sessions/set_home", token, { session_id: sessionId, agent_id: agentId, is_home: true });
}

/** 根目錄放沙盒裡：對話掛上專案時後端會在那裡寫交班檔，相對路徑會寫到後端的工作目錄。 */
export async function createProject(token: string, id: string, name: string): Promise<void> {
  const sandboxRoot = process.env[ENV_SANDBOX_ROOT];
  if (!sandboxRoot) throw new Error(`${ENV_SANDBOX_ROOT} is not set`);
  await call("POST", "/v1/ui/projects/save", token, {
    id,
    name,
    root_path: path.join(sandboxRoot, "e2e-real-project"),
    description: "實際驗證用",
  });
}

export async function disableHooks(token: string): Promise<void> {
  const current = await call<{ health: { revision: string | null } }>("GET", "/v1/hooks", token);
  await call("PUT", "/v1/hooks", token, {
    config: { enabled: false, hooks: {} },
    revision: current.health.revision,
  });
}

export async function hooksEnabled(token: string): Promise<boolean> {
  const body = await call<{ config: { enabled: boolean } }>("GET", "/v1/hooks", token);
  return body.config.enabled;
}

export async function setSlidingWindow(token: string, config: object): Promise<void> {
  await call("POST", "/v1/system/sliding_window", token, config);
}

/** 排程服務喚醒靈魂時打的端點；這裡沒有跑排程服務，權杖由 global-setup 產生。 */
export async function awakenSoul(initPrompt: string, sessionMode: "independent" | "home" = "independent"): Promise<void> {
  await call("POST", "/v1/heartbeat/awaken", null, {
    token: process.env[ENV_HEARTBEAT_TOKEN] ?? "",
    agent_id: SOUL_ID,
    pulse_count: 1,
    session_mode: sessionMode,
    init_prompt: initPrompt,
  });
}

export interface SavedMessage {
  role: string;
  generationFailed?: boolean;
}

/** 存檔裡這段對話的訊息節點（不是畫面上的）。 */
export async function savedMessages(token: string, sessionId: string): Promise<SavedMessage[]> {
  const session = await call<{ messages: { messages: Record<string, SavedMessage> } }>(
    "GET",
    `/v1/ui/sessions/${sessionId}`,
    token,
  );
  return Object.values(session.messages.messages);
}

/** 用標題找對話編號：畫面上看不到編號。 */
export async function sessionIdByTitle(token: string, title: string): Promise<string> {
  const sessions = await call<{ id: string; title: string }[]>("GET", "/v1/ui/sessions", token);
  const match = sessions.find((session) => session.title === title);
  if (!match) throw new Error(`no session titled ${title}`);
  return match.id;
}

export async function hookRuns(token: string): Promise<HookRunRow[]> {
  const body = await call<{ runs: HookRunRow[] }>("GET", "/v1/hooks/runs", token);
  return body.runs;
}
