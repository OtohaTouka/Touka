import { fetchTouka, getActiveProxyUrl } from '../connections';
import { ERRORS, type ErrorCode } from '../error_codes';
import { errorMsg } from '../error_handler';

export class AuthRequestError extends Error {
  constructor(readonly code: ErrorCode) { super(errorMsg(code)); this.name = 'AuthRequestError'; }
}

export function safeAuthError(error: unknown): AuthRequestError {
  return error instanceof AuthRequestError ? error : new AuthRequestError('SYSTEM_ERROR');
}

export interface AuthRequestOptions { proxyUrl?: string; signal?: AbortSignal; credentialEpoch?: number; }
type TokenResponse = { access_token: string; token_type: string };
const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

async function authFailure(res: Response): Promise<AuthRequestError> {
  const body: unknown = await res.json().catch(() => null);
  const detail = record(body) && record(body.detail) ? body.detail : body;
  if (record(detail)) {
    for (const [code, definition] of Object.entries(ERRORS)) {
      if ((code.startsWith('AUTH_') || code === 'VALIDATION_ERROR') && definition.status === res.status &&
          (detail.id === definition.id || detail.code === code)) return new AuthRequestError(code as ErrorCode);
    }
  }
  return new AuthRequestError(res.status === 401 ? 'AUTH_INVALID_TOKEN' :
    res.status === 403 ? 'AUTH_PERMISSION_DENIED' : res.status === 429 ? 'AUTH_RATE_LIMITED' :
      res.status === 422 ? 'VALIDATION_ERROR' : 'SYSTEM_ERROR');
}

async function requestToken(path: string, body: string | undefined, options: AuthRequestOptions): Promise<TokenResponse> {
  let res: Response;
  try {
    res = await fetchTouka(options.proxyUrl ?? getActiveProxyUrl(), path, {
      method: 'POST', ...(body === undefined ? {} : { body }), signal: options.signal,
      credentialEpoch: options.credentialEpoch, nonStreaming: true,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new AuthRequestError('NETWORK_CONNECTION_ERROR');
  }
  if (!res.ok) throw await authFailure(res);
  const data: unknown = await res.json().catch(() => null);
  if (!record(data) || typeof data.access_token !== 'string' || !data.access_token.trim() || data.token_type !== 'bearer') {
    throw new AuthRequestError('AUTH_INVALID_TOKEN');
  }
  return { access_token: data.access_token, token_type: data.token_type };
}

// P3（認證縫 #6）：login/refresh/logout 改走 getActiveProxyUrl()——對齊 changePassword
// 與所有業務請求（AuthContext.fetchUserProfile 亦然）。舊制固定打 CONFIG.PROXY_URL，
// 多連線設定下 refresh cookie 存在於 CONFIG origin、業務請求打 active origin，401 後
// 對 active origin 打 /auth/refresh（無 cookie）→ 401 → 誤殺整個 session。單機同源
// 故未爆；此收斂消除 cookie/token 面向分裂的潛在不一致。
export const authService = {
  login: (email: string, password: string, options: AuthRequestOptions = {}) =>
    requestToken('/v1/auth/login', JSON.stringify({ email, password }), options),
  refresh: (options: AuthRequestOptions = {}) => requestToken('/v1/auth/refresh', undefined, options),
  register: async (email: string, password: string, role = 'normal') => {
    const res = await fetchTouka(getActiveProxyUrl(), '/v1/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, password, role })
    });
    if (!res.ok) throw new Error('Registration failed');
    return res.json();
  },
  logout: async () => {
    try {
      const res = await fetchTouka(getActiveProxyUrl(), '/v1/auth/logout', {
        method: 'POST'
      });
      if (res.ok) {
        return await res.json();
      }
    } catch {
      // 靜默忽略錯誤，以防阻礙前端清空憑證及狀態
    }
    return { status: 'success' };
  },
  /**
   * 自助修改密碼（POST /v1/auth/change-password，後端已存在）。
   * 回傳原始 Response 供呼叫端依 res.ok 分支並把錯誤 body 交給 getErrorMessage（禁令 44）。
   * 走 getActiveProxyUrl()（多連線正確性，對齊 GeneralTab/useUserSettings）。
   */
  changePassword: async (current_password: string, new_password: string): Promise<Response> => {
    return fetchTouka(getActiveProxyUrl(), '/v1/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password, new_password })
    });
  }
};
