import type { SaveResult, Session, SessionSnapshot, BufferedStreamState, StreamStartedEvent } from './types';
import { ERRORS } from './error_codes';
import type { components } from '../types/api';

/** 僅前端生命週期；傳輸結果仍由 Pydantic 生成的 SaveResult 定義。 */
export type PersistenceState = SaveResult | { status: 'pending' };
export type SessionChange = (current: Readonly<Session>) => Session;

export const unknownSave = (): SaveResult => ({ status: 'unknown', error_code: ERRORS.SESSION_SAVE_FAILED.id });
export const rejectedSave = (): SaveResult => ({ status: 'failed', error_code: ERRORS.SESSION_SAVE_FAILED.id });
export const isSaved = (result: PersistenceState): boolean =>
  (result.status === 'saved' && result.representation === 'full') ||
  (result.status === 'skipped' && result.reason === 'incognito');

/** 不接受額外欄位，尤其不將伺服器的原始 detail／輸入反射到 UI。 */
export function parseSaveResult(value: unknown): SaveResult | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const data = value as Record<string, unknown>;
  const keys = Object.keys(data).sort().join(',');
  if (data.status === 'saved' && keys === 'representation,revision,status' &&
      (data.representation === 'text' || data.representation === 'full') &&
      typeof data.revision === 'string' && data.revision.length > 0) {
    return { status: 'saved', representation: data.representation, revision: data.revision };
  }
  if (data.status === 'skipped' && keys === 'reason,status' &&
      (data.reason === 'incognito' || data.reason === 'no_text')) {
    return { status: 'skipped', reason: data.reason };
  }
  if ((data.status === 'failed' || data.status === 'unknown') && keys === 'error_code,status' &&
      typeof data.error_code === 'number' && Object.values(ERRORS).some(error => error.id === data.error_code)) {
    return { status: data.status, error_code: data.error_code };
  }
  return null;
}

export function applySessionChange(current: Session, change: SessionChange): Session {
  const next = change(structuredClone(current));
  // 命令不能改身分 key 或將記憶體無痕轉為可持久化資料。
  return { ...next, id: current.id, ...(current.incognito ? { incognito: true } : {}) };
}

export function replaceSession(list: Session[], session: Session): Session[] {
  return list.some(item => item.id === session.id)
    ? list.map(item => item.id === session.id ? session : item)
    : [session, ...list];
}

export function mergeSessionRefresh(local: Session[], remote: Session[], protectedIds: ReadonlySet<string>): Session[] {
  const retained = local.filter(session => session.incognito || protectedIds.has(session.id));
  const retainedIds = new Set(retained.map(session => session.id));
  return [...retained, ...remote.filter(session => !retainedIds.has(session.id))]
    .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
}

/** UI 可覆寫欄位；不把 revision、owner 或 server 衍生值當作可寫內容。缺席仍是缺席。 */
const writableFields = ['id', 'title', 'messages', 'updatedAt', 'is_home', 'home_for_agent', 'agent_id',
  'project_id', 'auth_level', 'active_mcp_servers', 'active_skills', 'context_summary'] as const satisfies readonly (keyof Session)[];

export function sessionWritePayload(session: Session): Record<string, unknown> {
  return Object.fromEntries(writableFields.filter(key => session[key] !== undefined).map(key => [key, session[key]]));
}

export function sessionContentKey(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(sessionContentKey).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.entries(value).filter(([, item]) => item !== undefined)
    .sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${sessionContentKey(item)}`).join(',')}}`;
  return JSON.stringify(value);
}

export function matchesSessionContent(intended: Session, actual: Session): boolean {
  const fields = sessionWritePayload(intended);
  return Object.entries(fields).every(([key, value]) => sessionContentKey(value) ===
    sessionContentKey(actual[key as keyof Session]));
}

export function parseSessionSnapshot(value: unknown, id: string): SessionSnapshot | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const data = value as Record<string, unknown>;
  if (Object.keys(data).sort().join(',') !== 'exists,revision,session' || typeof data.revision !== 'string' || !data.revision) return null;
  if (data.exists === false && data.session === null) return data as SessionSnapshot;
  if (data.exists !== true || !data.session || typeof data.session !== 'object' || Array.isArray(data.session)) return null;
  const session = data.session as Record<string, unknown>;
  const authLevels = { Manual: true, Auto: true, YOLO: true } satisfies
    Record<components['schemas']['SessionDocument']['auth_level'], true>;
  if (session.id !== id || typeof session.title !== 'string' || !Number.isSafeInteger(session.updatedAt) ||
      !Number.isSafeInteger(session.last_activity_at) || typeof session.is_home !== 'boolean' ||
      !['home_for_agent', 'agent_id', 'project_id', 'context_summary'].every(key =>
        session[key] === null || typeof session[key] === 'string') ||
      typeof session.auth_level !== 'string' || !Object.hasOwn(authLevels, session.auth_level) ||
      !['active_mcp_servers', 'active_skills'].every(key => Array.isArray(session[key]) &&
        (session[key] as unknown[]).every(item => typeof item === 'string')) ||
      !session.messages || typeof session.messages !== 'object' ||
      Array.isArray(session.messages)) return null;
  const tree = session.messages as Record<string, unknown>;
  if (!tree.messages || typeof tree.messages !== 'object' || Array.isArray(tree.messages) ||
      (tree.currentId !== null && typeof tree.currentId !== 'string')) return null;
  return data as SessionSnapshot;
}

/** 狀態 GET 仍是未信任輸入；只有同一組座標可供 decoder 恢復游標。 */
export function parseBufferedStreamState(value: unknown, id: string): BufferedStreamState | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const state = value as Record<string, unknown>;
  if (state.session_id !== id || typeof state.stream_id !== 'string' || !state.stream_id ||
      !['active', 'done', 'error', 'cancelled'].includes(String(state.status)) ||
      !Number.isSafeInteger(state.last_seq) || typeof state.started_at !== 'number' || !Number.isFinite(state.started_at)) return null;
  const started = state.started as StreamStartedEvent | null;
  if (!started || started.type !== 'touka_stream_started' || started.session_id !== id || started.stream_id !== state.stream_id ||
      typeof started.user_parent_id !== 'string' || !started.user_parent_id ||
      typeof started.assistant_node_id !== 'string' || !started.assistant_node_id) return null;
  if (state.terminal !== null && state.terminal !== undefined) {
    const terminal = state.terminal as Record<string, unknown>;
    if (terminal.type !== 'touka_stream_terminal' || terminal.session_id !== id || terminal.stream_id !== state.stream_id ||
        terminal.user_parent_id !== started.user_parent_id || terminal.assistant_node_id !== started.assistant_node_id ||
        !['completed', 'error', 'cancelled'].includes(String(terminal.generation)) || !parseSaveResult(terminal.persistence)) return null;
  }
  return state as BufferedStreamState;
}
