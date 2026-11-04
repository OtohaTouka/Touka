import {
  API_ORIGIN,
  ENV_EMAIL,
  ENV_HEARTBEAT_TOKEN,
  ENV_PASSWORD,
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
export async function createSoul(token: string): Promise<void> {
  await call("POST", "/v1/souls", token, {
    agent_id: SOUL_ID,
    name: SOUL_ID,
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
      [SOUL_ID]: { role: "Owner", capabilities: ["heartbeat_awakening"] },
    },
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

export async function hookRuns(token: string): Promise<HookRunRow[]> {
  const body = await call<{ runs: HookRunRow[] }>("GET", "/v1/hooks/runs", token);
  return body.runs;
}
