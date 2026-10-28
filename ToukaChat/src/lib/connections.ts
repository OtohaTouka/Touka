/* eslint-disable no-restricted-globals */
/**
 * 統一 API 適配器 (Unified API Adapter)
 * 支持 ToukaProxy, Ollama, OpenAI, OpenRouter, LiteLLM
 * NOTE: fetchTouka() 與 fetchChatStream() 是 fetch 的唯一授權呼叫點，
 *       其他模組必須透過這兩個函式發送請求。
 */
import { BackendProtocol, Connection, StreamStartedEvent, StreamTerminalEvent, UiPersistence } from './types';
import { CONFIG } from './config';
import { logSSOTError, ToukaApiError } from './error_handler';
import { getLocalStorageItem, setLocalStorageItem } from '../hooks/useLocalStorage';
import { PROVIDER_CATALOG, buildChatEndpoint } from './providerCatalog';
import { getSystemEpoch } from './time_utils';
import { parseSaveResult } from './session_persistence';
import { INTERJECTION_EVENT_TYPES, ROUND_INTERRUPTED_EVENT } from './chat/interjections';

// BackendProvider 由供應商目錄 SSOT 衍生（providerCatalog.ts），不再各自維護一份清單。
export type BackendProvider = keyof typeof PROVIDER_CATALOG;

// P4 fix: store access token in module-level memory, not localStorage.
// On page refresh, the HttpOnly refresh cookie auto-restores the session.
let _accessToken: string | null = null;

// P3（認證縫 #9）：token 世代——clearAccessToken（登出／換帳號）遞增之，作廢所有
// 在途 refresh run，使晚到的成功回應不得把記憶體 access token 重新填回（半殭屍）。
let _tokenGeneration = 0;

export function setAccessToken(token: string): void {
    // 明確登入／還原是新的 client scope，即使是同 principal 或相同 token。
    _accessToken = token;
    _tokenGeneration++;
    _refreshPromise = null;
    notifyCredentialScopeChanged();
}
export function getAccessToken(): string | null { return _accessToken; }
/** 登入尚未有 principal 時，仍可錨定明確憑證世代。 */
export function getCredentialGeneration(): number { return _tokenGeneration; }
export function clearAccessToken(): void {
    _accessToken = null;
    _refreshPromise = null;
    _tokenGeneration++;               // 作廢在途 refresh（回填前會比對世代）
    _knownAgentIds = new Set();       // 清跨帳號殘留（換帳號登入時 AgentProvider 重填）
    notifyCredentialScopeChanged();
}

export interface RequestScope {
    readonly proxyOrigin: string;
    readonly authenticatedPrincipalId: string;
    readonly authEpoch: number;
}

export interface ToukaRequestInit extends RequestInit {
    /** 本機保存操作的 scope fence；不會送到 fetch 或後端。null 明確拒絕。 */
    requestScope?: RequestScope | null;
    /** 一般 HTTP 請求的取消不代表串流；仍沿原 requestTimeoutMs。 */
    nonStreaming?: boolean;
    /** 僅瀏覽器內 auth 操作前置；不傳入 wire。 */
    credentialEpoch?: number;
}

const credentialScopeListeners = new Set<() => void>();
function notifyCredentialScopeChanged(): void {
    for (const listener of credentialScopeListeners) listener();
}

export function subscribeCredentialScope(listener: () => void): () => void {
    credentialScopeListeners.add(listener);
    return () => { credentialScopeListeners.delete(listener); };
}

function tokenPrincipal(token: string | null): string | null {
    if (!token) return null;
    try {
        const parts = token.split('.');
        if (parts.length !== 3) return null;
        const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
        if (typeof payload.sub !== 'string' || !payload.sub) return null;
        if (payload.type !== undefined && typeof payload.type !== 'string') return null;
        // 只作瀏覽器內防串身分；不驗簽、不替代後端認證或權限。
        return `${payload.type ?? ''}:${payload.sub}`;
    } catch {
        return null;
    }
}

export function captureRequestScope(): RequestScope | null {
    const principal = tokenPrincipal(_accessToken);
    return principal === null ? null : {
        proxyOrigin: getActiveProxyUrl(), authenticatedPrincipalId: principal, authEpoch: _tokenGeneration,
    };
}

export function isRequestScopeCurrent(scope: RequestScope | null): boolean {
    const current = captureRequestScope();
    return !!scope && !!current && scope.proxyOrigin === current.proxyOrigin &&
        scope.authenticatedPrincipalId === current.authenticatedPrincipalId && scope.authEpoch === current.authEpoch;
}

function assertRequestScope(scope: RequestScope | null | undefined, proxyUrl: string, options: ToukaRequestInit): void {
    if (options.credentialEpoch !== undefined && (options.credentialEpoch !== _tokenGeneration ||
        proxyUrl !== getActiveProxyUrl() || options.signal?.aborted)) {
        throw new DOMException('Authentication request scope changed', 'AbortError');
    }
    if (scope === undefined) return;
    const explicitAuthorization = new Headers(options.headers).get('Authorization');
    if (!isRequestScopeCurrent(scope) || scope?.proxyOrigin !== proxyUrl ||
        (explicitAuthorization !== null && explicitAuthorization !== `Bearer ${_accessToken}`) || options.signal?.aborted) {
        throw new DOMException('Session request scope changed', 'AbortError');
    }
}

// ── 修復 2（ADR-0144）：session 死亡通知通路 ─────────────────────────────────────
// refresh 被後端權威判死（401/403＝撤銷/過期/重放偵測）時呼叫訂閱者（AuthContext
// 清狀態＋導回登入頁）。舊行為「保留舊卡」讓 app 卡在帶死卡狂打 401 的死循環。
let _onSessionExpired: (() => void) | null = null;
export function setSessionExpiredHandler(cb: (() => void) | null): void {
  _onSessionExpired = cb;
}

// ── 修復 3（ADR-0144）：曾登入旗標 ──────────────────────────────────────────────
// 登入成功時標記、登出「不」清除：曾登入過的瀏覽器對 X-New-Auth-Token（LDH 降級卡）
// 一律不採納——人類面認證失敗必須 fail-loud 走登入頁，絕不無聲換成 agent 面
// DebugAgent 卡（07-11 殭屍身分事故）。從未登入的瀏覽器維持首裝領卡相容。
const HAS_LOGGED_IN_KEY = 'touka_has_logged_in';
export function hasEverLoggedIn(): boolean {
  return getLocalStorageItem<boolean>(HAS_LOGGED_IN_KEY, false) === true;
}
export function markHasLoggedIn(): void {
  setLocalStorageItem(HAS_LOGGED_IN_KEY, true);
}

/**
 * 批1 ①：解 JWT exp 判斷 access token 是否（將）過期。
 *
 * 只讀 payload、不驗簽（驗簽是後端職責）。`skewSec` 提前量避免「剛好卡在邊界的
 * token 送達後端時已過期」。解析失敗一律回 `false`（fail-safe：寧可送出讓後端裁決，
 * 也不要因解析 bug 觸發無限 refresh）。時間走 time_utils SSOT（禁令 43）。
 */
export function isTokenExpired(token: string, skewSec = 30): boolean {
  try {
    const part = token.split('.')[1];
    if (!part) return false;
    const payload = JSON.parse(atob(part.replace(/-/g, '+').replace(/_/g, '/')));
    if (typeof payload.exp !== 'number') return false;
    return payload.exp * 1000 <= getSystemEpoch() + skewSec * 1000;
  } catch {
    return false;
  }
}

/** 認證端點：送出前不對其做「先 refresh」（否則 /auth/refresh 會遞迴觸發自己）。 */
function isAuthEndpoint(cleanPath: string): boolean {
  return /\/auth\/(refresh|login|register|logout)(\/|$)/.test(cleanPath);
}

// 批1 ①：single-flight refresh——並發的過期請求共用同一個 /auth/refresh，
// 避免打爆後端速率限制（refresh 30 次/60s）。
let _refreshPromise: Promise<string | null> | null = null;

/**
 * 批1 ①：以 HttpOnly refresh cookie 換新 access token（single-flight）。
 * 成功則更新記憶體 `_accessToken` 並回傳新 token。
 * 失敗分兩類（修復 2，ADR-0144）：
 *  - 401/403＝後端權威判死（撤銷/過期/重放偵測）→ 清卡＋通知 session-expired
 *    訂閱者（AuthContext 導回登入頁）。
 *  - 網路錯誤／5xx／429＝暫時性故障 → 保留舊卡回 `null`，稍後自然重試
 *    （不誤殺 session；由 ② 的「已登入不採納 X-New-Auth-Token」兜住不被挾持）。
 */
export async function refreshAccessToken(proxyUrl: string): Promise<string | null> {
  if (_refreshPromise) return _refreshPromise;
  const gen = _tokenGeneration;  // P3（#9）：記錄本 run 世代；期間登出即作廢
  const run = (async (): Promise<string | null> => {
    try {
      const res = await fetch(`${proxyUrl}/v1/auth/refresh`, { // noqa: FETCH_SSOT — 認證續期，避免 fetchTouka 遞迴
        method: 'POST',
        credentials: 'include',
      });
      if (!res.ok) {
        if ((res.status === 401 || res.status === 403) && _accessToken && gen === _tokenGeneration) {
          clearAccessToken();
          try {
            _onSessionExpired?.();
          } catch (e) {
            logSSOTError(e, 'Connections:sessionExpiredHandler');
          }
        }
        return null;
      }
      const data = await res.json().catch(() => null);
      const tok = data && typeof data.access_token === 'string' ? data.access_token : null;
      // P3（#9）：期間 clearAccessToken（登出/換帳號）已遞增世代 → 本 run 作廢，
      // 不得把記憶體 access token 重新填回（否則 UI 已登出、模組仍持有效卡＝半殭屍）。
      if (gen !== _tokenGeneration) return null;
      if (tok) {
        // 正常同 principal 續期保留 scope；不同／無法辨認主體不得沿舊操作。
        if (tokenPrincipal(tok) !== tokenPrincipal(_accessToken)) setAccessToken(tok);
        else _accessToken = tok;
      }
      return tok;
    } catch (e) {
      logSSOTError(e, 'Connections:refreshAccessToken');
      return null;
    }
  })();
  _refreshPromise = run;
  try {
    return await run;
  } finally {
    if (_refreshPromise === run) _refreshPromise = null;
  }
}

// SSOT: the set of known agent IDs, populated by AgentProvider from GET /v1/agents.
// resolveSoulId() consults this instead of a hardcoded agent-name list, so no agent
// IDs are written into production code here.
let _knownAgentIds = new Set<string>();
export function setKnownAgentIds(ids: string[]): void { _knownAgentIds = new Set(ids); }

/**
 * Resolve the X-Touka-Soul-ID identity header for a model id, derived from the agent
 * SSOT (no hardcoded agent names). Returns "" when the model is not a known agent —
 * the backend (monitor.py) treats this header as a priority-1 identity source, so it
 * must only be set for SSOT-confirmed agents, never arbitrary model ids.
 */
export function resolveSoulId(model: string): string {
    const lowModel = model.toLowerCase();
    // 正規化輔助：確保符合 PascalCase SSOT (ToukaGroup, DebugAgent)
    const normalize = (id: string): string =>
        (({ toukagroup: "ToukaGroup", debugagent: "DebugAgent" } as Record<string, string>)[id.toLowerCase()] || id);
    if (lowModel.startsWith("toukasoul")) return normalize(model.slice(9)); // 去除 toukasoul 前綴
    const known = [..._knownAgentIds].find((id) => id.toLowerCase() === lowModel);
    if (known) return known; // SSOT canonical id
    if (model.includes(":")) return normalize(model.split(":").pop() || "");
    return "";
}

/**
 * M5 fix: sanitize a header value by stripping characters that enable
 * HTTP header injection (CR \r, LF \n, null byte \x00).
 *
 * These characters allow an attacker who controls a header value to inject
 * arbitrary additional headers into the HTTP request.
 */
export function sanitizeHeaderValue(value: string): string {
    return value.replace(/[\r\n\x00]/g, '');
}

export interface ChatMessage {
    role: "user" | "assistant" | "system";
    content: string;
}

export interface ChatOptions {
    ui_persistence?: UiPersistence;
    requestScope?: RequestScope | null;
    provider: BackendProvider;
    baseUrl: string;
    apiKey?: string;
    model: string;
    stream?: boolean;
    protocol?: BackendProtocol;
    project_id?: string;
    session_id?: string;
    auth_level?: string;
    active_skills?: string[];
    /** HAX G17（ADR-0086）：無痕模式——觸發後端零殘留閘門。 */
    incognito?: boolean;
    /** ADR-0087：深入研究模式——觸發後端注入研究協調 Skill（多步搜尋＋引用報告）。 */
    deepResearch?: boolean;
    /** ADR-0161：MCP 動態掛載模式——觸發後端漸進揭露＋connect_mcp_server 工具。 */
    mcpDispatcher?: boolean;
}

export function getActiveConnection(): Connection | null {
    try {
        if (typeof window !== 'undefined') {
            const conns = getLocalStorageItem<Connection[]>("touka_connections", []);
            return conns.find((c: Connection) => c.enabled && (c.provider === 'proxy' || c.id === 'proxy')) || null;
        }
    } catch (e) {
        logSSOTError(e, "Connections:getActiveConnection");
    }
    return null;
}

export function getActiveProxyUrl(): string {
    const conn = getActiveConnection();
    const url = conn?.url || "";
    if (url) {
        return url.replace(/\/v1\/?$/, "").replace(/\/$/, "");
    }
    return CONFIG.PROXY_URL;
}

/**
 * ADR-0106：解析頭像 `<img src>`。上傳後 avatar_url 為 proxy 相對路徑
 * （`/v1/users/{id}/avatar?v=`），須補上 proxy 來源（否則相對路徑會錯指向前端 origin）；
 * 舊有的絕對外部 URL（http/https/data:）原樣回傳。
 */
export function avatarSrc(url?: string | null): string {
    if (!url) return "";
    if (/^(https?:|data:)/.test(url)) return url;
    if (url.startsWith("/")) return `${getActiveProxyUrl()}${url}`;
    return url;
}

export function getActiveHeartbeatUrl(): string {
    const proxyUrl = getActiveProxyUrl();
    if (proxyUrl) {
        try {
            const url = new URL(proxyUrl);
            if ((url.hostname === "localhost" || url.hostname === "127.0.0.1") && url.port === "8000") {
                return `${url.protocol}//${url.hostname}:8082`;
            }
        } catch (e) {
            logSSOTError(e, "Connections:getActiveHeartbeatUrl");
        }
    }
    return CONFIG.HEARTBEAT_URL;
}

export async function fetchTouka(
    proxyUrl: string,
    path: string,
    options: ToukaRequestInit = {},
    _retried = false, // 修復 2 內部旗標：401→續期→重送最多一次（防迴圈）
): Promise<Response> {
    const cleanPath = path.startsWith('/') ? path : '/' + path;
    const { requestScope, nonStreaming, credentialEpoch: _credentialEpoch, ...requestOptions } = options;
    assertRequestScope(requestScope, proxyUrl, options);

    // 批1 ①：送出前 token 續期——已登入且 access token（將）過期時，先 single-flight
    // refresh 再送。根因：本機 LDH 對「帶過期人類 token」的請求不回 401，而是靜默降級成
    // DebugAgent；讓 token 永不過期抵達後端，LDH 就永不觸發（比 401-retry 可靠、比定時
    // timer 不受睡眠影響）。refresh 失敗只保留舊卡（由下方領卡防挾持 ② 兜住）。
    if (_accessToken && !isAuthEndpoint(cleanPath) && isTokenExpired(_accessToken)) {
        await refreshAccessToken(proxyUrl);
    }
    assertRequestScope(requestScope, proxyUrl, options);

    // --- [認證全鏈路整合：自動領卡與送卡] ---
    const headers = new Headers(options.headers || {});

    // 1. 送卡：從記憶體獲取並注入 Token (P4 fix: no longer localStorage)
    // usedModuleToken＝本請求帶的是「模組管理的人類卡」（呼叫端自帶 Authorization
    // 者除外）——僅這類請求適用修復 2 的 401 續期重送。
    const usedModuleToken = !!_accessToken && !headers.has('Authorization');
    if (usedModuleToken) {
        headers.set('Authorization', `Bearer ${_accessToken}`);
    }

    // FormData（如頭像 multipart 上傳）：不可手動設 Content-Type，須由瀏覽器自帶 multipart
    // boundary；否則後端解析 multipart 失敗。其餘有 body 者預設 JSON。
    if (options.body && !headers.has('Content-Type') && !(options.body instanceof FormData)) {
        headers.set('Content-Type', 'application/json');
    }
    
    // 一般自帶 signal 的串流沿用長連線豁免；scoped 保存仍合併使用者中止與原設定逾時。
    const timeoutMs = options.signal && requestScope === undefined && !nonStreaming ? 0 : (getActiveConnection()?.requestTimeoutMs ?? 0);
    let timeoutHandle: ReturnType<typeof setTimeout> | undefined;
    const finalOptions: RequestInit = { ...requestOptions, headers, credentials: 'include' };
    if (timeoutMs > 0) {
        const controller = new AbortController();
        timeoutHandle = setTimeout(
            () => controller.abort(new DOMException('Connection request timed out', 'TimeoutError')),
            timeoutMs,
        );
        finalOptions.signal = options.signal ? AbortSignal.any([options.signal, controller.signal]) : controller.signal;
    }

    try {
        assertRequestScope(requestScope, proxyUrl, options);
        const resp = await fetch(`${proxyUrl}${cleanPath}`, finalOptions); // noqa: FETCH_SSOT
        assertRequestScope(requestScope, proxyUrl, options);

        // 2. 領卡：若後端發放新 Token，存入記憶體 (P4 fix: no longer localStorage)
        //    批1 ②：僅在「未登入」時採納。已登入（_accessToken 非空）時忽略——本機 LDH
        //    對過期人類 token 會靜默降級並發一張 7 天 DebugAgent 卡，無條件採納會挾持整個
        //    app 成 DebugAgent（側欄錯亂、模型變 DebugAgent）。
        //    修復 3（ADR-0144）再收斂：「曾登入過的瀏覽器」永不採納（殭屍身分對釘）；
        //    僅「未登入且從未登入過」維持 LDH 首次建卡（首裝精靈等 pre-login）相容。
        try {
            const newToken = resp.headers.get("X-New-Auth-Token");
            if (newToken && !_accessToken && !hasEverLoggedIn()) {
                _accessToken = newToken;
            }
        } catch (e) {
            logSSOTError(e, "Connections:fetchTouka:領卡");
        }

        // 修復 2（ADR-0144）：帶模組卡的請求被判 401（例：改密碼撤銷——token 未過期
        // 但已失效，送出前續期攔不到）→ single-flight 續期一次並以新卡重送一次。
        // 續期也被判死時 refreshAccessToken 已清卡＋通知導回登入，這裡原樣回傳 401。
        // auth 端點除外：登入失敗等 401 是正常業務回應，不得觸發續期。
        if (resp.status === 401 && usedModuleToken && !_retried && !isAuthEndpoint(cleanPath)) {
            const freshToken = await refreshAccessToken(proxyUrl);
            assertRequestScope(requestScope, proxyUrl, options);
            if (freshToken) {
                return await fetchTouka(proxyUrl, path, options, true);
            }
        }

        // 僅在路徑尚未包含 /v1 且發生 404 時嘗試自動回退
        if (resp.status === 404 && !cleanPath.startsWith('/v1')) {
            return await fetchTouka(proxyUrl, '/v1' + cleanPath, options, _retried);
        }
        return resp;
    } finally {
        if (timeoutHandle !== undefined) clearTimeout(timeoutHandle);
    }
}

export async function fetchToolSchemas(): Promise<Record<string, unknown>[]> {
    const proxyUrl = getActiveProxyUrl();
    const res = await fetchTouka(proxyUrl, "/v1/debug/tools");
    if (!res.ok) return [];
    const data = await res.json();
    return data.tools || [];
}

/**
 * 執行登出邏輯：通知後端廢止 Token 並清除記憶體中的 accessToken。
 * 導向 /login 的責任由呼叫端 AuthContext.logout() 負責，
 * 確保 React state 正確清空後再觸發 AuthGuard 路由跳轉。
 */
export async function logoutTouka(): Promise<void> {
    const proxyUrl = getActiveProxyUrl();
    const generation = _tokenGeneration;
    try {
        await fetchTouka(proxyUrl, "/v1/auth/logout", { method: 'POST' });
    } catch (e) {
        logSSOTError(e, "Connections:logoutTouka");
    } finally {
        // P3（#9）：走 clearAccessToken（遞增世代作廢在途 refresh＋清 knownAgentIds），
        // 不再裸設 _accessToken=null——否則登出時的在途 refresh 晚到會復活記憶體卡。
        if (generation === _tokenGeneration && proxyUrl === getActiveProxyUrl()) clearAccessToken();
    }
}

export async function fetchChatStream(
    messages: ChatMessage[],
    options: ChatOptions,
    onChunk: (text: string) => void,
    onDone: (terminal?: StreamTerminalEvent) => void | Promise<void>,
    onError: (err: unknown) => void,
    onEvent?: (event: Record<string, unknown>) => void,
    signal?: AbortSignal,
    onBeforeSend?: (payload: Record<string, unknown>) => Promise<boolean | void>,
    onThinking?: (text: string) => void
) {
    const { provider, baseUrl, apiKey, model, protocol = 'openai', project_id, session_id, auth_level, active_skills, incognito, deepResearch, mcpDispatcher } = options;
    let url = "";
    const requestScope = options.requestScope === undefined
        ? (options.ui_persistence ? captureRequestScope() : undefined) : options.requestScope;
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    
    // --- 靈魂識別加固 (X-Touka-Soul-ID) ---
    if (provider === 'proxy') {
        // soulId 由 agent SSOT 衍生（resolveSoulId），不寫死任何 agent 名稱
        const soulId = resolveSoulId(model);
        if (soulId) {
            // M5 fix: sanitize before setting to prevent header injection
            headers["X-Touka-Soul-ID"] = sanitizeHeaderValue(soulId);
        }

        // 技能啟用標頭：將本 session 啟用的技能 ID 列表傳遞給後端 SkillLayer
        if (active_skills?.length) {
            headers["x-touka-skills"] = active_skills.join(",");
        }
    }

    let body: Record<string, unknown> = {};

    // URL 組裝集中於 buildChatEndpoint（SSOT；解 /v1 重複拼接）。headers/body 仍依 provider 分支。
    url = buildChatEndpoint(provider, baseUrl, protocol);

    switch (provider) {
        case "proxy":
            if (protocol === 'anthropic') {
                if (apiKey) headers["x-api-key"] = apiKey;
                headers["anthropic-version"] = "2023-06-01";
                const systemMsg = messages.find(m => m.role === 'system')?.content;
                body = { 
                    model, 
                    system: systemMsg, 
                    messages: messages.filter(m => m.role !== 'system'), 
                    stream: true, 
                    max_tokens: 4096,
                    project_id,
                    session_id,
                    auth_level,
                    incognito,
                    deep_research: deepResearch,
                    mcp_dispatcher: mcpDispatcher
                };
            } else {
                if (apiKey) headers["Authorization"] = "Bearer " + apiKey;
                body = { model: model, messages, stream: true, project_id, session_id, auth_level, incognito, deep_research: deepResearch, mcp_dispatcher: mcpDispatcher };
            }
            break;
        case "openai":
        case "openrouter":
        case "litellm":
            if (apiKey) headers["Authorization"] = "Bearer " + apiKey;
            body = { model: model, messages, stream: true, project_id };
            break;
        case "ollama":
            body = { model, messages, stream: true };
            break;
        default:
            if (apiKey) headers["Authorization"] = "Bearer " + apiKey;
            body = { model: model, messages, stream: true, project_id };
    }

    if (provider === 'proxy' && options.ui_persistence) body.ui_persistence = options.ui_persistence;

    // --- [除錯攔截點] ---
    if (onBeforeSend) {
        const proceed = await onBeforeSend(body);
        if (proceed === false) return;
    }

    try {
        // 批1 ①：proxy 串流送出前 token 續期（同 fetchTouka）——帶過期卡會被本機 LDH 靜默
        // 降級成 DebugAgent，使回合錯記在 DebugAgent 名下、側欄分組錯亂、模型顯示變 DebugAgent。
        if (provider === 'proxy' && _accessToken && isTokenExpired(_accessToken)) {
            await refreshAccessToken(baseUrl);
        }
        // --- [認證全鏈路整合：Streaming 模式送卡] ---
        // 每次送出都重讀 _accessToken（修復 2 的 401 續期重送需以新卡重打）。
        const sendStream = async (): Promise<Response> => {
            if (provider === 'proxy') assertRequestScope(requestScope, baseUrl, { headers, signal });
            if (_accessToken) {
                headers["Authorization"] = `Bearer ${_accessToken}`;
            }
            return fetch(url, { // noqa: FETCH_SSOT — 多供應商 SSE 串流（Ollama/OpenAI/xAI），URL 格式與 fetchTouka 不相容
                method: "POST",
                headers,
                body: JSON.stringify(body),
                signal
            });
        };
        let response = await sendStream();

        // --- [認證全鏈路整合：Streaming 模式領卡] ---
        // 批1 ②＋修復 3：僅「未登入且此瀏覽器從未登入過」採納（同 fetchTouka）。
        try {
            const newToken = response.headers.get("X-New-Auth-Token");
            if (newToken && !_accessToken && !hasEverLoggedIn()) {
                _accessToken = newToken;
            }
        } catch (e) {
            logSSOTError(e, "Connections:fetchChatStream:領卡");
        }

        // 修復 2（ADR-0144）：帶模組卡串流被判 401 → single-flight 續期一次重送
        //（改密碼撤銷等「未過期但已失效」情境）。續期死＝handler 已通知導回登入，
        // 走下方 !response.ok 的既有錯誤路徑（onError）。
        if (provider === 'proxy' && response.status === 401 && _accessToken) {
            const freshToken = await refreshAccessToken(baseUrl);
            if (freshToken) {
                response = await sendStream();
            }
        }

        if (!response.ok) {
            const errData = await response.json().catch(() => ({}));
            // ToukaException 形狀 {"detail":{"code",...}}：保留錯誤碼給聊天畫面判斷（如 AGENT_BRAIN_NOT_SET）
            const code = typeof errData?.detail?.code === 'string' ? errData.detail.code : undefined;
            const reason = typeof errData?.detail?.reason === 'string' ? errData.detail.reason : undefined;
            throw new ToukaApiError(errData?.error?.message || `HTTP Error ${response.status}`, response.status, code, reason);
        }
        
        await consumeChatStream(response, onChunk, onDone, onEvent, onThinking,
            options.ui_persistence && session_id ? { session_id, ...options.ui_persistence } : undefined,
            requestScope, signal);
    } catch (err) {
        onError(err);
    }
}

type StreamExpectation = Pick<StreamStartedEvent, 'session_id' | 'user_parent_id' | 'assistant_node_id'> & { stream_id?: string };
export interface ResumeStreamContract { started: StreamStartedEvent; requestScope: RequestScope | null }

/** 唯一 bytes→lines 入口；EOF/DONE 只表示連線結束，UI 必須另有同代合法 terminal。 */
async function consumeChatStream(
    response: Response, onChunk: (text: string) => void,
    onDone: (terminal?: StreamTerminalEvent) => void | Promise<void>,
    onEvent?: (event: Record<string, unknown>) => void, onThinking?: (text: string) => void,
    expected?: StreamExpectation, scope?: RequestScope | null, signal?: AbortSignal,
    admittedStart?: StreamStartedEvent,
): Promise<void> {
    const reader = response.body?.getReader();
    if (!reader) throw new Error('Response body is null');
    const decoder = new TextDecoder('utf-8', { fatal: true });
    let pending = '';
    let started: StreamStartedEvent | undefined = admittedStart;
    let terminal: StreamTerminalEvent | undefined;
    const sameScope = () => {
        if (scope !== undefined && (!isRequestScopeCurrent(scope) || signal?.aborted)) {
            throw new DOMException('Session request scope changed', 'AbortError');
        }
    };
    const line = (raw: string) => {
        if (terminal) return;
        if (!expected) { dispatchSseLine(raw, onChunk, onEvent, onThinking); return; }
        let event: Record<string, unknown>;
        try { event = JSON.parse(raw.replace(/^data:\s*/, '').trim()); } catch { return; }
        if (!event || typeof event !== 'object' || Array.isArray(event)) return;
        if (event.type === 'touka_stream_started' || event.type === 'touka_stream_terminal') {
            if (event.session_id !== expected.session_id || event.user_parent_id !== expected.user_parent_id ||
                event.assistant_node_id !== expected.assistant_node_id || typeof event.stream_id !== 'string' ||
                !event.stream_id || (expected.stream_id && event.stream_id !== expected.stream_id)) return;
            if (event.type === 'touka_stream_started') {
                if (!started && Object.keys(event).sort().join(',') === 'assistant_node_id,session_id,stream_id,type,user_parent_id') {
                    started = event as unknown as StreamStartedEvent;
                    onEvent?.(event);
                }
            } else if (started && started.stream_id === event.stream_id &&
                Object.keys(event).sort().join(',') === 'assistant_node_id,generation,persistence,session_id,stream_id,type,user_parent_id' &&
                (event.generation === 'completed' || event.generation === 'error' || event.generation === 'cancelled')) {
                const persistence = parseSaveResult(event.persistence);
                if (persistence) terminal = { ...started, type: 'touka_stream_terminal', generation: event.generation, persistence };
            }
            return;
        }
        if (started) dispatchSseLine(raw, onChunk, onEvent, onThinking);
    };
    try {
        while (!terminal) {
            const { done, value } = await reader.read();
            sameScope();
            pending += done ? decoder.decode() : decoder.decode(value, { stream: true });
            const lines = pending.split('\n');
            pending = lines.pop() || '';
            for (const raw of lines) line(raw);
            if (done) { if (pending) line(pending); break; }
        }
        sameScope();
        if (expected) await onDone(terminal);
        else await onDone();
    } finally {
        await reader.cancel().catch(() => {});
        reader.releaseLock();
    }
}

/**
 * 將單一 SSE 行分發為 onChunk（增量文字）或 onEvent（結構化事件）。
 *
 * fetchChatStream 與 resumeChatStream（ADR-0081 重整接回）共用的解析 SSOT，
 * 確保「即時串流」與「緩衝重播」對同一事件的解讀完全一致。
 */
function dispatchSseLine(
    line: string,
    onChunk: (text: string) => void,
    onEvent?: (event: Record<string, unknown>) => void,
    onThinking?: (text: string) => void
): void {
    const cleanLine = line.replace(/^data: /, "").trim();
    if (!cleanLine || cleanLine === "[DONE]") return;

    try {
        const json = JSON.parse(cleanLine);
        // skill_creation_approval_request（ADR-0149）：演化部署自帶審批卡（帶
        // source_code）。過去不在白名單 → 前端丟棄 → 後端空等 180s 逾時＝互動部署靜默
        // 失敗。與 skill_install_approval_request 同走 onSkillInstallRequest → EvolutionModal。
        const customEventTypes = ["touka_status", "tool_start", "tool_result", "error", "loop_status", "skill_install_approval_request", "skill_creation_approval_request", "ask_user_request", "touka_progress", "tool_stream", "tool_approval_request", "tool_approval_resolved", "mcp_interaction_request", "mcp_interaction_resolved", ...INTERJECTION_EVENT_TYPES, ROUND_INTERRUPTED_EVENT];

        if (json.type && customEventTypes.includes(json.type) && onEvent) {
            onEvent(json);
            return;
        }

        // ADR-0152 思考分頻：後端把思考走獨立 delta.reasoning_content（DeepSeek 串流
        // 慣例）。前端摺疊顯示，不混入正文；未接 onThinking 的呼叫端安全忽略。
        const openaiReasoning = json.choices?.[0]?.delta?.reasoning_content;
        if (openaiReasoning) {
            onThinking?.(openaiReasoning);
            return;
        }

        const openaiContent = json.choices?.[0]?.delta?.content;
        if (openaiContent) {
            onChunk(openaiContent);
            return;
        }

        if (json.type === 'content_block_delta' && json.delta?.text) {
            onChunk(json.delta.text);
        }
    } catch {
        // Ignore JSON parse errors for incomplete lines
    }
}

/**
 * ADR-0081：接回伺服器端串流緩衝（重整後恢復）。
 *
 * 走 fetchTouka（GET，自動注入 Authorization、credentials，FETCH_SSOT 合規），
 * 自 ``starting_after`` 游標重播後跟流至終態；解析與 fetchChatStream 共用
 * dispatchSseLine，前端以同一套邏輯重建訊息樹。
 */
export async function resumeChatStream(
    proxyUrl: string,
    sessionId: string,
    startingAfter: number,
    onChunk: (text: string) => void,
    onDone: (terminal?: StreamTerminalEvent) => void | Promise<void>,
    onError: (err: unknown) => void,
    onEvent?: (event: Record<string, unknown>) => void,
    signal?: AbortSignal,
    onThinking?: (text: string) => void,
    contract?: ResumeStreamContract,
): Promise<void> {
    try {
        const res = await fetchTouka(
            proxyUrl,
            `/v1/ui/sessions/${encodeURIComponent(sessionId)}/stream?starting_after=${startingAfter}${contract ? `&stream_id=${encodeURIComponent(contract.started.stream_id)}` : ''}`,
            { signal, ...(contract ? { requestScope: contract.requestScope } : {}) }
        );
        if (!res.ok) {
            onError(new Error(`Resume stream failed: HTTP ${res.status}`));
            return;
        }
        await consumeChatStream(res, onChunk, onDone, onEvent, onThinking, contract?.started, contract?.requestScope, signal,
            startingAfter >= 0 ? contract?.started : undefined);
    } catch (err) {
        onError(err);
    }
}
