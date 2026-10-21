"use client";

import React, { createContext, useContext, useState, useEffect, useCallback, useRef, useMemo } from "react";
import type { Session, SaveResult, StreamTerminalEvent, SessionSnapshot } from "../types";
import { captureRequestScope, isRequestScopeCurrent, subscribeCredentialScope, fetchTouka } from "../connections";
import type { RequestScope } from "../connections";
import { logSSOTError, errorMsg } from "@/lib/error_handler";
import { toast } from "sonner";
import { ERRORS } from '../error_codes';
import { applySessionChange, replaceSession, mergeSessionRefresh, parseSaveResult,
  unknownSave, rejectedSave, isSaved, sessionWritePayload, sessionContentKey, matchesSessionContent, parseSessionSnapshot } from '../session_persistence';
import type { PersistenceState, SessionChange } from '../session_persistence';

export interface SessionProducer {
  readonly sessionId: string;
  readonly scope: RequestScope | null;
  readonly generation: number;
}
interface PendingChange { change: SessionChange; deferred?: boolean }
interface SessionWriter {
  unconfirmed?: boolean;
  intent: number;
  recoveryBusy?: boolean;
  requireAbsence?: boolean;
  recovery?: SessionSnapshot;
  confirmed: Session;
  tail: Promise<unknown>;
  pending: PendingChange[];
  result?: PersistenceState;
  generation: number;
  halted: boolean;
  producer?: { token: SessionProducer; done: Promise<void>; release: () => void };
}
interface SessionContextType {
  sessions: Session[];
  setSessions: React.Dispatch<React.SetStateAction<Session[]>>;
  getSession: (id: string) => Session | undefined;
  refreshSessions: () => Promise<Session[]>;
  updateSessionTitle: (id: string, title: string) => Promise<SaveResult>;
  deleteSession: (id: string) => Promise<boolean>;
  syncSession: (session: Session, operation?: object) => Promise<SaveResult>;
  updateSession: (id: string, change: SessionChange, operation?: object, expectedBase?: Session) => Promise<SaveResult>;
  getPersistence: (id: string) => PersistenceState | undefined;
  recheckSession: (id: string) => Promise<SaveResult>;
  retrySaveSession: (id: string) => Promise<SaveResult>;
  canRetrySave: (id: string) => boolean;
  prepareResume: (id: string) => Promise<Session | undefined>;
  beginProducer: (id: string) => SessionProducer | null;
  completeProducer: (token: SessionProducer, change: SessionChange, result: SaveResult,
    generation?: StreamTerminalEvent['generation']) => Promise<SaveResult>;
  scopeKey: string;
}

const SessionListContext = createContext<SessionContextType | undefined>(undefined);
const scopeId = (scope: RequestScope | null) => JSON.stringify(scope);

export function SessionListProvider({ children }: { children: React.ReactNode }) {
  const [sessions, renderSessions] = useState<Session[]>([]);
  const sessionsRef = useRef<Session[]>([]);
  const writers = useRef(new Map<string, SessionWriter>());
  const operations = useRef(new WeakMap<object, Map<string, Promise<SaveResult>>>());
  const requests = useRef(new Set<AbortController>());
  const scopeRef = useRef<RequestScope | null>(captureRequestScope());
  const [scopeKey, setScopeKey] = useState(() => scopeId(scopeRef.current));

  // 全部 ingress 共用此同步 transition。React 只收到值，不執行帶副作用的 updater。
  const setSessions = useCallback<React.Dispatch<React.SetStateAction<Session[]>>>((action) => {
    const next = typeof action === 'function' ? action(sessionsRef.current) : action;
    sessionsRef.current = next;
    renderSessions(next);
  }, []);
  const getSession = useCallback((id: string) => sessionsRef.current.find(session => session.id === id), []);

  const ensureScope = useCallback(() => {
    const current = captureRequestScope();
    if (scopeId(current) !== scopeId(scopeRef.current)) {
      for (const controller of requests.current) controller.abort();
      requests.current.clear();
      for (const writer of writers.current.values()) writer.producer?.release();
      writers.current.clear();
      operations.current = new WeakMap();
      scopeRef.current = current;
      setSessions([]);
      setScopeKey(scopeId(current));
    }
    return current;
  }, [setSessions]);

  const getWriter = useCallback((session: Session) => {
    let writer = writers.current.get(session.id);
    if (!writer) {
      writer = { confirmed: structuredClone(session), tail: Promise.resolve(), pending: [], generation: 0, intent: 0, halted: false };
      writers.current.set(session.id, writer);
    }
    return writer;
  }, []);

  const report = useCallback((id: string, result: SaveResult) => {
    if (!isSaved(result)) {
      logSSOTError(new Error('Session persistence was not confirmed'), `SessionListProvider:syncSession:${id}`);
      toast.error(errorMsg('SESSION_SAVE_FAILED'), { id: 'session-sync-failed' });
    }
  }, []);

  const rejectChange = useCallback((id: string, writer: SessionWriter, pending?: PendingChange): SaveResult => {
    const result = rejectedSave();
    writer.halted = !writer.confirmed.incognito;
    writer.result = result;
    if (pending) writer.pending = writer.pending.filter(item => item !== pending);
    // 保留最後可呈現草稿；後續只允許明示核對，不自動重放失效語意。
    setSessions(previous => [...previous]);
    report(id, result);
    return result;
  }, [report, setSessions]);

  const readSnapshot = useCallback(async (id: string, scope: RequestScope | null): Promise<SessionSnapshot | null> => {
    if (!isRequestScopeCurrent(scope)) return null;
    const controller = new AbortController();
    requests.current.add(controller);
    try {
      const response = await fetchTouka(scope!.proxyOrigin, `/v1/ui/sessions/${encodeURIComponent(id)}?snapshot=true`,
        { requestScope: scope, signal: controller.signal });
      const body: unknown = response.ok && response.status === 200 ? await response.json() : null;
      if (!isRequestScopeCurrent(scope) || controller.signal.aborted) return null;
      return parseSessionSnapshot(body, id);
    } catch { return null; }
    finally { requests.current.delete(controller); }
  }, []);

  const bindBase = useCallback(async (writer: SessionWriter, scope: RequestScope | null,
    allowAbsentDelete = false): Promise<SaveResult | null> => {
    if (writer.confirmed.incognito || writer.confirmed.revision) return null;
    const snapshot = await readSnapshot(writer.confirmed.id, scope);
    if (!snapshot) return unknownSave();
    if (snapshot.exists) {
      if (writer.requireAbsence || !matchesSessionContent(writer.confirmed, snapshot.session)) return rejectedSave();
      writer.confirmed = { ...writer.confirmed, ...snapshot.session, revision: snapshot.revision };
    } else {
      if (!writer.requireAbsence && !allowAbsentDelete) return { status: 'failed', error_code: ERRORS.SESSION_NOT_FOUND.id };
      writer.confirmed = { ...writer.confirmed, revision: snapshot.revision };
    }
    writer.unconfirmed = false;
    return null;
  }, [readSnapshot]);

  const save = useCallback(async (session: Session, scope: RequestScope | null): Promise<SaveResult> => {
    if (session.incognito) return { status: 'skipped', reason: 'incognito' };
    if (!isRequestScopeCurrent(scope)) return rejectedSave();
    const controller = new AbortController();
    requests.current.add(controller);
    try {
      const res = await fetchTouka(scope!.proxyOrigin, `/v1/ui/sessions/${session.id}`, {
        method: 'POST', body: JSON.stringify({ ...sessionWritePayload(session), revision: session.revision }), requestScope: scope, signal: controller.signal,
      });
      if (!isRequestScopeCurrent(scope)) return unknownSave();
      const body: unknown = await res.json();
      if (!isRequestScopeCurrent(scope) || controller.signal.aborted) return unknownSave();
      if (res.status !== 200 || !res.ok || !body || typeof body !== 'object' ||
          !('status' in body) || body.status !== 'success' || !('session_id' in body) ||
          body.session_id !== session.id || !('persistence' in body)) return unknownSave();
      const result = parseSaveResult(body.persistence);
      // 一般全樹保存不能以 text/no_text 當作本次全樹已保存。
      return result?.status === 'saved' && result.representation === 'full' ? result :
        result?.status === 'failed' || result?.status === 'unknown' ? result : unknownSave();
    } catch {
      return unknownSave();
    } finally {
      requests.current.delete(controller);
    }
  }, []);

  const updateSession = useCallback((id: string, change: SessionChange, operation: object = {}, expectedBase?: Session): Promise<SaveResult> => {
    const scope = ensureScope();
    const current = getSession(id);
    if (!current) return Promise.resolve(rejectedSave());
    const capturedBase = expectedBase === undefined ? undefined : structuredClone(expectedBase);
    if (capturedBase && !matchesSessionContent(capturedBase, current)) {
      report(id, rejectedSave());
      return Promise.resolve(rejectedSave());
    }
    const writer = getWriter(current);
    const operationScope = JSON.stringify([id, writer.generation]);
    const sessionOperations = operations.current.get(operation) ?? new Map<string, Promise<SaveResult>>();
    const existingOperation = sessionOperations.get(operationScope);
    if (existingOperation) return existingOperation;
    writer.intent++;
    writer.recovery = undefined;
    const pending = { change, deferred: expectedBase !== undefined };
    writer.pending.push(pending);
    if (!writer.halted) writer.result = { status: 'pending' };
    let optimistic: Session;
    try { optimistic = pending.deferred ? current : applySessionChange(current, change); }
    catch { return Promise.resolve(rejectChange(id, writer, pending)); }
    if (!pending.deferred) setSessions(previous => replaceSession(previous, optimistic));
    else setSessions(previous => [...previous]);
    const generation = writer.generation;
    const run = writer.tail.then(async (): Promise<SaveResult> => {
      if (writer.producer) await writer.producer.done;
      if (writers.current.get(id) !== writer || writer.generation !== generation ||
          (!current.incognito && !isRequestScopeCurrent(scope))) return rejectedSave();
      if (writer.halted) {
        writer.pending = writer.pending.filter(item => item !== pending);
        return writer.result?.status === 'failed' || writer.result?.status === 'unknown' ? writer.result : unknownSave();
      }
      const baseError = await bindBase(writer, scope);
      if (writers.current.get(id) !== writer || writer.generation !== generation ||
          (!current.incognito && !isRequestScopeCurrent(scope))) return rejectedSave();
      if (capturedBase && (!matchesSessionContent(capturedBase, writer.confirmed) ||
          !getSession(id) || !matchesSessionContent(capturedBase, getSession(id)!))) {
        writer.pending = writer.pending.filter(item => item !== pending);
        writer.result = writer.pending.length ? { status: 'pending' } : writer.confirmed.incognito
          ? { status: 'skipped', reason: 'incognito' } : writer.confirmed.revision
            ? { status: 'saved', representation: 'full', revision: writer.confirmed.revision } : undefined;
        setSessions(previous => [...previous]);
        report(id, rejectedSave());
        return rejectedSave();
      }
      let intended: Session;
      try { intended = { ...applySessionChange(writer.confirmed, change), revision: writer.confirmed.revision }; }
      catch { return rejectChange(id, writer, pending); }
      const result = baseError ?? await save(intended, scope);
      if (writers.current.get(id) !== writer || writer.generation !== generation) return result;
      if (isSaved(result)) {
        writer.unconfirmed = false;
        writer.confirmed = { ...intended, ...(result.status === 'saved' ? { revision: result.revision } : {}) };
      } else writer.halted = true;
      writer.result = result;
      writer.pending = writer.pending.filter(item => item !== pending);
      if (isSaved(result)) {
        const latest = getSession(id);
        let preview: Session | undefined;
        try { preview = pending.deferred ? writer.pending.reduce<Session>((state, item) => item.deferred ? state : applySessionChange(state, item.change), intended) : latest; }
        catch { return rejectChange(id, writer); }
        if (preview) setSessions(previous => replaceSession(previous, { ...preview,
          ...(result.status === 'saved' ? { revision: result.revision } : {}) }));
      } else setSessions(previous => [...previous]);
      report(id, result);
      return result;
    });
    writer.tail = run;
    sessionOperations.set(operationScope, run);
    operations.current.set(operation, sessionOperations);
    return run;
  }, [ensureScope, getSession, getWriter, setSessions, save, report, bindBase, rejectChange]);

  const syncSession = useCallback((session: Session, operation?: object) => {
    ensureScope();
    if (!getSession(session.id)) setSessions(previous => replaceSession(previous, session));
    const writer = getWriter(getSession(session.id)!);
    if (!writer.confirmed.revision) writer.requireAbsence = true;
    // 只有建立／明確匯入使用 snapshot；既有 session 的操作改走 semantic change。
    return updateSession(session.id, () => session, operation);
  }, [ensureScope, getSession, getWriter, setSessions, updateSession]);

  const getPersistence = useCallback((id: string) => writers.current.get(id)?.result, []);
  const beginProducer = useCallback((id: string): SessionProducer | null => {
    const scope = ensureScope();
    const session = getSession(id);
    if (!session) return null;
    const writer = getWriter(session);
    if (writer.halted || writer.pending.length || writer.producer || writer.recoveryBusy) return null;
    let release!: () => void;
    const done = new Promise<void>(resolve => { release = resolve; });
    const token = { sessionId: id, scope, generation: writer.generation };
    writer.producer = { token, done, release };
    return token;
  }, [ensureScope, getSession, getWriter]);

  const completeProducer = useCallback(async (token: SessionProducer, change: SessionChange, terminal: SaveResult,
    generation: StreamTerminalEvent['generation'] = 'completed') => {
    const writer = writers.current.get(token.sessionId);
    if (!writer?.producer || writer.producer.token !== token || writer.generation !== token.generation ||
        (!writer.confirmed.incognito && !isRequestScopeCurrent(token.scope))) return rejectedSave();
    const producer = writer.producer;
    try {
      let intended: Session;
      try { intended = applySessionChange(writer.confirmed, change); }
      catch { return rejectChange(token.sessionId, writer); }
      let result: SaveResult = writer.confirmed.incognito ? { status: 'skipped', reason: 'incognito' } :
        generation === 'completed' || terminal.status === 'unknown' || terminal.status === 'failed' ? terminal : unknownSave();
      if (generation === 'completed' && (terminal.status === 'saved' || (terminal.status === 'skipped' &&
          (terminal.reason === 'no_text' || writer.confirmed.incognito)))) {
        if (terminal.status === 'saved') intended.revision = terminal.revision;
        result = await save(intended, token.scope);
      }
      if (writers.current.get(token.sessionId) !== writer || writer.producer !== producer) return result;
      writer.result = result;
      writer.halted = !isSaved(result);
      if (isSaved(result)) writer.confirmed = { ...intended,
        ...(result.status === 'saved' ? { revision: result.revision } : {}) };
      // 先合併真終態富樹，再重播尚未送出的純語意命令；不能覆蓋晚到的 UI 選擇。
      let preview: Session;
      try { preview = writer.pending.reduce((state, item) => item.deferred ? state : applySessionChange(state, item.change), intended); }
      catch { return rejectChange(token.sessionId, writer); }
      setSessions(previous => replaceSession(previous, preview));
      report(token.sessionId, result);
      return result;
    } finally {
      if (writer.producer === producer) writer.producer = undefined;
      producer.release();
    }
  }, [save, setSessions, report, rejectChange]);

  const recheckSession = useCallback(async (id: string): Promise<SaveResult> => {
    const scope = ensureScope();
    const writer = writers.current.get(id);
    const draft = getSession(id);
    if (!writer || !draft || writer.producer || writer.pending.length || writer.recoveryBusy) return rejectedSave();
    if (draft.incognito) return { status: 'skipped', reason: 'incognito' };
    const generation = writer.generation;
    const intent = writer.intent;
    const tail = writer.tail;
    writer.recoveryBusy = true;
    writer.recovery = undefined;
    const snapshot = await readSnapshot(id, scope);
    writer.recoveryBusy = false;
    if (writers.current.get(id) !== writer || writer.generation !== generation || !isRequestScopeCurrent(scope)) return unknownSave();
    if (writer.intent !== intent || writer.tail !== tail) {
      setSessions(previous => [...previous]);
      return rejectedSave();
    }
    let result: SaveResult = unknownSave();
    if (snapshot?.exists) {
      if (matchesSessionContent(draft, snapshot.session)) {
        result = { status: 'saved', representation: 'full', revision: snapshot.revision };
        writer.unconfirmed = false;
        writer.confirmed = { ...draft, ...snapshot.session, revision: snapshot.revision };
        setSessions(previous => replaceSession(previous, writer.confirmed));
      } else {
        result = rejectedSave();
        writer.recovery = snapshot;
      }
    } else if (snapshot?.exists === false) result = { status: 'failed', error_code: ERRORS.SESSION_NOT_FOUND.id };
    writer.result = result;
    writer.halted = !isSaved(result);
    setSessions(previous => [...previous]);
    return result;
  }, [ensureScope, getSession, readSnapshot, setSessions]);

  const retrySaveSession = useCallback((id: string): Promise<SaveResult> => {
    const scope = ensureScope();
    const writer = writers.current.get(id);
    const draft = getSession(id);
    if (!writer || !draft || writer.producer || writer.pending.length || writer.recoveryBusy || writer.result?.status !== 'failed' ||
        !writer.recovery?.exists) return Promise.resolve(rejectedSave());
    const revision = writer.recovery.revision;
    writer.recovery = undefined; // 一次人工指令只送一次；雙按不加入第二個 POST。
    writer.recoveryBusy = true;
    writer.result = { status: 'pending' };
    setSessions(previous => [...previous]);
    const generation = writer.generation;
    const run = writer.tail.then(async () => {
      if (writers.current.get(id) !== writer || writer.generation !== generation || !isRequestScopeCurrent(scope)) return rejectedSave();
      const intended = { ...draft, revision };
      const result = await save(intended, scope);
      if (writers.current.get(id) !== writer || writer.generation !== generation) return result;
      writer.recoveryBusy = false;
      writer.result = result;
      writer.halted = !isSaved(result);
      if (result.status === 'saved' && result.representation === 'full') {
        writer.unconfirmed = false;
        writer.confirmed = { ...intended, revision: result.revision };
        let preview: Session;
        try { preview = writer.pending.reduce((state, item) => item.deferred ? state : applySessionChange(state, item.change), writer.confirmed); }
        catch { return rejectChange(id, writer); }
        setSessions(previous => replaceSession(previous, preview));
      } else setSessions(previous => [...previous]);
      report(id, result);
      return result;
    });
    writer.tail = run;
    return run;
  }, [ensureScope, getSession, setSessions, save, report, rejectChange]);

  const prepareResume = useCallback(async (id: string): Promise<Session | undefined> => {
    const scope = ensureScope();
    const current = getSession(id);
    if (!current) return undefined;
    const writer = getWriter(current);
    if (writer.unconfirmed || writer.halted || writer.pending.length || writer.producer || writer.recoveryBusy) return undefined;
    if (current.incognito) return current;
    const intent = writer.intent;
    const generation = writer.generation;
    writer.recoveryBusy = true;
    const snapshot = await readSnapshot(id, scope);
    writer.recoveryBusy = false;
    if (!isRequestScopeCurrent(scope) || writers.current.get(id) !== writer || writer.generation !== generation ||
        writer.intent !== intent || writer.pending.length || writer.producer) return undefined;
    if (!snapshot?.exists) {
      writer.result = snapshot ? { status: 'failed', error_code: ERRORS.SESSION_NOT_FOUND.id } : unknownSave();
      writer.halted = true;
      setSessions(previous => [...previous]);
      return undefined;
    }
    writer.confirmed = { ...current, ...snapshot.session, revision: snapshot.revision };
    setSessions(previous => replaceSession(previous, writer.confirmed));
    return writer.confirmed;
  }, [ensureScope, getSession, getWriter, readSnapshot, setSessions]);

  const refreshSessions = useCallback(async () => {
    const scope = ensureScope();
    if (!scope) return sessionsRef.current;
    const controller = new AbortController();
    requests.current.add(controller);
    try {
      const res = await fetchTouka(scope.proxyOrigin, '/v1/ui/sessions', { requestScope: scope, signal: controller.signal });
      const parsed: unknown = res.ok ? await res.json() : null;
      if (!isRequestScopeCurrent(scope) || !Array.isArray(parsed)) return sessionsRef.current;
      const protectedIds = new Set([...writers.current].filter(([, writer]) =>
        writer.unconfirmed || writer.halted || writer.producer || writer.pending.length || writer.recoveryBusy).map(([id]) => id));
      const merged = mergeSessionRefresh(sessionsRef.current, parsed, protectedIds);
      for (const session of parsed as Session[]) {
        if (!protectedIds.has(session.id)) {
          const writer = writers.current.get(session.id);
          if (writer) writer.confirmed = structuredClone(session);
        }
      }
      setSessions(merged);
      return merged;
    } catch {
      return sessionsRef.current;
    } finally {
      requests.current.delete(controller);
    }
  }, [ensureScope, setSessions]);

  const updateSessionTitle = useCallback((id: string, title: string) =>
    updateSession(id, current => ({ ...current, title })), [updateSession]);

  const deleteSession = useCallback(async (id: string): Promise<boolean> => {
    const scope = ensureScope();
    const session = getSession(id);
    if (!session) return false;
    const writer = getWriter(session);
    if (writer.producer || writer.halted || writer.recoveryBusy) return false;
    const generation = writer.generation;
    const run = writer.tail.then(async () => {
      if (writers.current.get(id) !== writer || writer.generation !== generation || writer.halted ||
          (!session.incognito && !isRequestScopeCurrent(scope))) return false;
      if (!session.incognito) {
        const baseError = await bindBase(writer, scope, true);
        if (baseError || writers.current.get(id) !== writer || writer.generation !== generation) {
          writer.result = baseError ?? unknownSave(); writer.halted = true;
          setSessions(previous => [...previous]);
          return false;
        }
        const controller = new AbortController();
        requests.current.add(controller);
        try {
          const res = await fetchTouka(scope!.proxyOrigin, `/v1/ui/sessions/${id}`, {
            method: 'DELETE', headers: { 'If-Match': writer.confirmed.revision! }, requestScope: scope, signal: controller.signal,
          });
          const body: unknown = res.status === 200 && res.ok ? await res.json() : null;
          if (!isRequestScopeCurrent(scope) || controller.signal.aborted) return false;
          if (!body || typeof body !== 'object' ||
              !('status' in body) || body.status !== 'success' || !('session_id' in body) || body.session_id !== id) {
            toast.error(errorMsg('DB_DELETE_FAILED'), { id: 'session-delete-failed' });
            return false;
          }
        } catch {
          if (isRequestScopeCurrent(scope)) toast.error(errorMsg('DB_DELETE_FAILED'), { id: 'session-delete-failed' });
          return false;
        } finally {
          requests.current.delete(controller);
        }
      }
      writer.generation++;
      writer.halted = true;
      setSessions(previous => previous.filter(item => item.id !== id));
      return true;
    });
    writer.tail = run;
    return run;
  }, [ensureScope, getSession, getWriter, setSessions, bindBase]);

  useEffect(() => {
    const activeRequests = requests.current;
    const handleScopeChanged = () => { ensureScope(); void refreshSessions(); };
    const unsubscribe = subscribeCredentialScope(handleScopeChanged);
    // 非同步載入；所有實際 state transition 在單一同步入口。
    void refreshSessions();
    window.addEventListener('touka_auth_changed', handleScopeChanged);
    window.addEventListener('touka_connections_updated', handleScopeChanged);
    window.addEventListener('storage', handleScopeChanged);
    return () => {
      for (const controller of activeRequests) controller.abort();
      unsubscribe();
      window.removeEventListener('touka_auth_changed', handleScopeChanged);
      window.removeEventListener('touka_connections_updated', handleScopeChanged);
      window.removeEventListener('storage', handleScopeChanged);
    };
  }, [ensureScope, refreshSessions]);

  const value = useMemo<SessionContextType>(() => {
    // 舊元件在 await 之後仍可能持有命令；不能重新捕獲新登入的 scope 來送舊內容。
    const current = () => scopeId(captureRequestScope()) === scopeKey && scopeId(scopeRef.current) === scopeKey;
    return {
      sessions, scopeKey,
      setSessions: action => {
        if (!current()) return;
        const previous = sessionsRef.current;
        const proposed = typeof action === 'function' ? action(previous) : action;
        const retained = new Map<string, Session>();
        for (const local of previous) {
          const writer = writers.current.get(local.id);
          if (writer && (writer.unconfirmed || writer.halted || writer.producer || writer.pending.length || writer.recoveryBusy)) retained.set(local.id, local);
        }
        const next = proposed.map(session => {
          const protectedSession = retained.get(session.id);
          if (protectedSession) {
            retained.delete(session.id);
            return { ...protectedSession, deepResearch: session.deepResearch, mcpDispatcher: session.mcpDispatcher };
          }
          const writer = writers.current.get(session.id);
          const previousSession = writer?.confirmed ?? previous.find(item => item.id === session.id);
          if (previousSession && sessionContentKey(sessionWritePayload(session)) !== sessionContentKey(sessionWritePayload(previousSession))) {
            // 外部整樹是 UI 輸入，不是保存證據；下一個命令必須重新核對其基底。
            const unconfirmed = { ...session, revision: undefined };
            const unconfirmedWriter = writer ?? getWriter(unconfirmed);
            unconfirmedWriter.confirmed = structuredClone(unconfirmed);
            unconfirmedWriter.unconfirmed = true;
            unconfirmedWriter.intent++;
            unconfirmedWriter.recovery = undefined;
            return unconfirmed;
          }
          return session;
        });
        setSessions([...next, ...retained.values()]);
      },
      getSession: id => current() ? getSession(id) : undefined,
      getPersistence: id => current() ? getPersistence(id) : undefined,
      recheckSession: id => current() ? recheckSession(id) : Promise.resolve(rejectedSave()),
      retrySaveSession: id => current() ? retrySaveSession(id) : Promise.resolve(rejectedSave()),
      canRetrySave: id => current() && !writers.current.get(id)?.recoveryBusy && !!writers.current.get(id)?.recovery?.exists,
      prepareResume: id => current() ? prepareResume(id) : Promise.resolve(undefined),
      refreshSessions: () => current() ? refreshSessions() : Promise.resolve([]),
      updateSessionTitle: (id, title) => current() ? updateSessionTitle(id, title) : Promise.resolve(rejectedSave()),
      deleteSession: id => current() ? deleteSession(id) : Promise.resolve(false),
      syncSession: (session, operation) => current() ? syncSession(session, operation) : Promise.resolve(rejectedSave()),
      updateSession: (id, change, operation, expectedBase) => current() ? updateSession(id, change, operation, expectedBase) : Promise.resolve(rejectedSave()),
      beginProducer: id => current() ? beginProducer(id) : null,
      completeProducer: (token, change, result, generation) => current()
        ? completeProducer(token, change, result, generation) : Promise.resolve(rejectedSave()),
    };
  }, [sessions, scopeKey, setSessions, getSession, getWriter, getPersistence, refreshSessions, updateSessionTitle,
    deleteSession, syncSession, updateSession, beginProducer, completeProducer, recheckSession, retrySaveSession, prepareResume]);

  return (
    <SessionListContext.Provider value={value}>
      {/* 換 scope 重新建立 view/stream 子樹，包含 Chat 的私有 input、history、attachments 與 drafts refs。 */}
      <React.Fragment key={scopeKey}>{children}</React.Fragment>
    </SessionListContext.Provider>
  );
}

export function useSessionList() {
  const context = useContext(SessionListContext);
  if (!context) throw new Error("useSessionList must be used within SessionListProvider");
  return context;
}
