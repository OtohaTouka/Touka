"use client";

/**
 * ADR-0080 背景續跑：進行中串流的 per-session 登錄表（SSOT）。
 *
 * 設計：
 * - 每個進行中的對話 session 對應一個 StreamState（自有 AbortController + 累積樹 + status）。
 * - 串流回調（onChunk/onEvent/onDone）以函數式 setState 更新「對應 sid」的 entry，
 *   與「前端正在顯示哪個 session」完全無關 → 切走後背景續跑、切回從登錄表衍生顯示。
 * - 防污染由 per-session 隔離達成：chunk 只寫進自己 sid 的樹，碰不到別的對話
 *   （取代 2026-06-11 review #1 的 abort-based 防污染）。
 *
 * SRP：本 provider 只負責進行中串流的生命週期與累積 + 完成時寫回 sessions。
 * 顯示綁定（active session 衍生）與 artifact 面板由 ChatProvider 處理。
 */
import React, { createContext, useContext, useState, useRef, useCallback, useEffect } from "react";
import { fetchChatStream, fetchTouka, resumeChatStream, ChatMessage,
  captureRequestScope, isRequestScopeCurrent } from "../connections";
import type { RequestScope } from '../connections';
import { useSessionList } from "./SessionListProvider";
import type { SessionProducer } from './SessionListProvider';
import { useDebug } from "./DebugProvider";
import { buildUserTurnTree, appendAssistantNode, restoreAssistantNode, applyChunkToTree, applyThinkingToTree, applyEventToTree, recordInterjection, stampNodeTime } from "../stream_tree";
import { applyInterjectionEvent } from "../chat/interjectionStore";
import { INTERJECTION_EVENT_TYPES } from "../chat/interjections";
import { getLinearMessages, migrateFlatToTree, TreeHistory } from "../tree_history";
import { getSystemEpoch } from "../time_utils";
import { BackendProtocol, SkillInstallRequest, AskUserRequest, SaveResult, StreamTerminalEvent, StreamStartedEvent } from "../types";
import { useArtifacts } from "@/hooks/useArtifacts";
import { useAgentBrainNotice } from "@/hooks/useAgentBrainNotice";
import { logSSOTError, errorMsg } from "@/lib/error_handler";
import { toast } from "sonner";
import type { ChatAttachment } from "@/lib/chat/attachment";
import { buildApiHistory } from "@/lib/chat/buildApiMessage";
import { isSaved, unknownSave, parseBufferedStreamState } from '../session_persistence';
import { useTranslation } from 'react-i18next';

/** 同時可背景生成的對話數上限（溫和上限，SSOT）。 */
export const MAX_CONCURRENT_STREAMS = 5;

export type StreamStatus = "saving" | "waiting" | "streaming" | "done" | "error";

export interface StreamState {
  sessionId: string;
  controller: AbortController;
  tree: TreeHistory;
  assistantMsgId: string;
  aiText: string;
  /** ADR-0152 思考分頻：delta.reasoning_content 的累積（寫入節點 thinking）。 */
  aiThinking: string;
  status: StreamStatus;
  model: string;
  incognito?: boolean;
  producer?: SessionProducer;
  requestScope: RequestScope | null;
  persistence?: SaveResult;
  started?: StreamStartedEvent;
  settling?: boolean;
}

export interface StartStreamParams {
  sessionId: string;
  baseTree: TreeHistory;
  userText: string;
  resolvedInput: string; // resolveMentions 後的文字，送 API 用（樹存原文 userText）
  attachment: ChatAttachment | null;
  parentId: string | null;
  model: string;
  protocol: BackendProtocol;
  proxyUrl: string;
  projectId?: string;
  authLevel: string;
  activeSkills: string[];
  onBeforeSend?: (payload: Record<string, unknown>) => Promise<boolean | void>;
  onSkillInstallRequest?: (req: SkillInstallRequest) => void;
  onAskUser?: (req: AskUserRequest) => void;
  incognito?: boolean;
  deepResearch?: boolean;
  mcpDispatcher?: boolean;
}

interface StreamRegistryContextType {
  startStream: (params: StartStreamParams) => Promise<boolean>;
  stopStream: (sessionId: string) => void;
  getStream: (sessionId: string) => StreamState | undefined;
  checkSessionStream: (sessionId: string) => Promise<void>;
  streamingSessionIds: string[];
  activeCount: number;
}

const StreamRegistryContext = createContext<StreamRegistryContextType | undefined>(undefined);

export function StreamRegistryProvider({ children }: { children: React.ReactNode }) {
  const { t } = useTranslation();
  const { sessions, getSession, updateSession, beginProducer, completeProducer, prepareResume, getPersistence } = useSessionList();
  const { registerSimHandlers } = useDebug();
  const notifyBrainNotSet = useAgentBrainNotice();

  const [streams, setStreams] = useState<Record<string, StreamState>>({});
  // ref 鏡像：讓 onDone/stopStream 能讀到最新 controller/tree，而不在 setState updater 內做副作用
  const streamsRef = useRef<Record<string, StreamState>>(streams);

  // 從 ref（唯一真相）同步計算下一狀態、立即寫回 ref，再排程 render。
  // streams 只由 registry 自己變更，故以 ref 為準可保證同步迴圈內連續呼叫讀到最新值。
  const commit = useCallback((updater: (prev: Record<string, StreamState>) => Record<string, StreamState>) => {
    const next = updater(streamsRef.current);
    streamsRef.current = next;
    setStreams(next);
  }, []);

  const getStream = useCallback((sessionId: string) => {
    const state = streams[sessionId];
    return state?.status === 'error' && isSaved(getPersistence(sessionId) ?? unknownSave()) ? undefined : state;
  }, [streams, getPersistence]);

  // 完成時把最終樹寫回 session（沿用既有 useChatStream onDone 的寫回模式）
  const persistDone = useCallback(async (s: StreamState, terminal: SaveResult,
    generation: StreamTerminalEvent['generation']) => {
    const updatedAt = getSystemEpoch();
    // 助理回覆的完成時間；已有時間（例如重整後接回的節點）就保留。
    const treeMessages = stampNodeTime(s.tree, s.assistantMsgId, updatedAt);
    const linear = getLinearMessages(treeMessages);
    const firstUserTitle = linear.find(m => m.role === "user")?.content.slice(0, 20) || "新對話";
    if (!s.producer) return unknownSave();
    return completeProducer(s.producer, current => ({ ...current, messages: treeMessages,
      title: current.title === '新對話' ? firstUserTitle : current.title, agent_id: s.model, updatedAt }), terminal, generation);
  }, [completeProducer]);

  // 共用回調（一般串流與 ADR-0081 resume 接回都走同一套，解析語意一致）
  const createStreamCallbacks = useCallback((
    sessionId: string,
    controller: AbortController,
    onSkillInstallRequest?: (req: SkillInstallRequest) => void,
    onAskUser?: (req: AskUserRequest) => void,
  ) => {
    const ownsStream = () => streamsRef.current[sessionId]?.controller === controller &&
      isRequestScopeCurrent(streamsRef.current[sessionId]?.requestScope);
    const onChunk = (chunk: string) => {
      if (!ownsStream() || streamsRef.current[sessionId]?.settling) return;
      commit(prev => {
        const s = prev[sessionId];
        if (!s || s.controller !== controller) return prev;
        const aiText = s.aiText + chunk;
        return { ...prev, [sessionId]: { ...s, aiText, status: "streaming", tree: applyChunkToTree(s.tree, s.assistantMsgId, aiText) } };
      });
    };

    // ADR-0152 思考分頻：reasoning 累積寫入節點 thinking（與 onChunk 同構、獨立頻道）。
    const onThinking = (chunk: string) => {
      if (!ownsStream() || streamsRef.current[sessionId]?.settling) return;
      commit(prev => {
        const s = prev[sessionId];
        if (!s || s.controller !== controller) return prev;
        const aiThinking = s.aiThinking + chunk;
        return { ...prev, [sessionId]: { ...s, aiThinking, status: "streaming", tree: applyThinkingToTree(s.tree, s.assistantMsgId, aiThinking) } };
      });
    };

    const onEvent = (event: Record<string, unknown>) => {
      if (!ownsStream() || streamsRef.current[sessionId]?.settling) return;
      if (event.type === 'touka_stream_started') {
        const s = streamsRef.current[sessionId];
        if (event.session_id === sessionId && event.assistant_node_id === s.assistantMsgId &&
            event.user_parent_id === s.tree.messages[s.assistantMsgId]?.parentId &&
            typeof event.stream_id === 'string' && event.stream_id && !s.started) {
          commit(prev => ({ ...prev, [sessionId]: { ...s, started: event as unknown as StreamStartedEvent } }));
        }
        return;
      }
      // ADR-0249：插話——送達的寫進助理節點 interjections[]；未送達的留在佇列，回合結束後送出。
      if (INTERJECTION_EVENT_TYPES.includes(event.type as string)) {
        const record = applyInterjectionEvent(sessionId, event);
        if (record) commit(prev => {
          const s = prev[sessionId];
          if (!s || s.controller !== controller) return prev;
          return { ...prev, [sessionId]: { ...s, tree: recordInterjection(s.tree, s.assistantMsgId, record) } };
        });
        return;
      }
      // 安裝申請（skill_install_approval_request）與演化部署（skill_creation_approval_request，
      // ADR-0149）同走 EvolutionModal：前者收 env 輸入、後者呈現 source_code 供審。
      if (event.type === "skill_install_approval_request" || event.type === "skill_creation_approval_request") {
        onSkillInstallRequest?.(event as unknown as SkillInstallRequest);
        return;
      }
      // HAX G10（ADR-0085）：AI 詢問使用者 → 觸發 AskUserModal（不更新樹）
      if (event.type === "ask_user_request") {
        onAskUser?.(event as unknown as AskUserRequest);
        return;
      }
      // mission_tools 完成 → 切換到 MISSION 分頁並通知看板刷新（沿用既有全域行為）
      if (event.type === "tool_result" && event.tool === "mission_tools") {
        useArtifacts.getState().setActiveTab("mission");
        useArtifacts.getState().setIsOpen(true);
        window.dispatchEvent(new CustomEvent("touka_mission_updated"));
      }
      commit(prev => {
        const s = prev[sessionId];
        if (!s || s.controller !== controller) return prev;
        return { ...prev, [sessionId]: { ...s, tree: applyEventToTree(s.tree, s.assistantMsgId, event) } };
      });
    };

    const onDone = async (terminal?: StreamTerminalEvent) => {
      const s = streamsRef.current[sessionId];
      if (!s || !ownsStream() || s.settling) return;
      if (terminal && (!s.started || terminal.stream_id !== s.started.stream_id ||
          terminal.session_id !== sessionId || terminal.assistant_node_id !== s.assistantMsgId ||
          terminal.user_parent_id !== s.started.user_parent_id)) return;
      commit(prev => ({ ...prev, [sessionId]: { ...s, settling: true, status: 'saving' } }));
      // EOF／DONE 沒有保存證據；無痕只代表記憶體內容，不需要持久化核對。
      const result = await persistDone(s, terminal?.persistence ?? (s.incognito ?
        { status: 'skipped', reason: 'incognito' } : unknownSave()), terminal?.generation ?? 'error');
      if (!ownsStream()) return;
      commit(prev => {
        if (!isSaved(result)) return { ...prev, [sessionId]: { ...s, settling: true, status: 'error', persistence: result } };
        const next = { ...prev };
        delete next[sessionId];
        return next;
      });
    };

    const onError = async (err: unknown) => {
      const s = streamsRef.current[sessionId];
      if (!s || !ownsStream() || s.settling) return;
      commit(prev => ({ ...prev, [sessionId]: { ...s, settling: true, status: 'error' } }));
      // 使用者主動中止（AbortError）不提示；其餘為串流/連線錯誤，明確提示（P5b 失效可見化）
      if ((err as { name?: string })?.name !== "AbortError") {
        logSSOTError(new Error('Stream delivery failed'), `StreamRegistry:onError:${sessionId}`);
        // 沒設主要模型由專屬通知處理（model-p0 U7），其餘照舊顯示連線錯誤
        if (!notifyBrainNotSet(err)) toast.error(errorMsg("NETWORK_CONNECTION_ERROR"));
      }
      const generation = (err as { name?: string })?.name === 'AbortError' ? 'cancelled' : 'error';
      const result = await persistDone(s, unknownSave(), generation);
      if (ownsStream()) commit(prev => {
        if (s.incognito && isSaved(result)) { const next = { ...prev }; delete next[sessionId]; return next; }
        return { ...prev, [sessionId]: { ...s, settling: true, status: 'error', persistence: result } };
      });
    };

    return { onChunk, onThinking, onEvent, onDone, onError };
  }, [commit, persistDone, notifyBrainNotSet]);

  const startStream = useCallback(async (params: StartStreamParams): Promise<boolean> => {
    const { sessionId, baseTree, userText, resolvedInput, attachment, parentId,
      model, protocol, proxyUrl, projectId, authLevel, activeSkills,
      onBeforeSend, onSkillInstallRequest, onAskUser, incognito, deepResearch, mcpDispatcher } = params;

    const existing = streamsRef.current;
    // 同 session 已在進行中 → 不重入；達並發上限且為新 session → 拒絕
    if (existing[sessionId] && (existing[sessionId].status !== 'error' || !isSaved(getPersistence(sessionId) ?? unknownSave()))) return false;
    if (Object.values(existing).filter(stream => ['saving', 'waiting', 'streaming'].includes(stream.status)).length >= MAX_CONCURRENT_STREAMS) {
      toast.warning(t('hooks.use_chat_stream.background_limit_reached'));
      return false;
    }
    const requestScope = captureRequestScope();

    // ADR-0081：user 回合先行持久化——重整後 resume 的重建基底，
    // 也讓 user 訊息不再因重整消失；後端完成持久化以 currentId=user 為掛點。
    const userMsgId = crypto.randomUUID();
    const assistantMsgId = crypto.randomUUID();
    // 送出時間只取一次：保存重試時重算的樹必須相同。
    const sentAt = getSystemEpoch();
    const initial = getSession(sessionId);
    const initialTree = initial ? migrateFlatToTree(initial.messages) : baseTree;
    let userTree: TreeHistory;
    try { userTree = buildUserTurnTree(initialTree, { userText, attachment, parentId, userMsgId, timestamp: sentAt }).tree; }
    catch { toast.error(errorMsg('SESSION_SAVE_FAILED')); return false; }
    let tree = appendAssistantNode(userTree, assistantMsgId).tree;
    const controller = new AbortController();

    commit(prev => ({
      ...prev,
      [sessionId]: { sessionId, controller, tree, assistantMsgId, aiText: "", aiThinking: "", status: "saving", model, incognito, requestScope },
    }));
    const updatedAt = sentAt;
    const ownsStream = () => streamsRef.current[sessionId]?.controller === controller;
    const fail = (result: SaveResult) => {
      if (ownsStream()) commit(prev => ({ ...prev, [sessionId]: { ...prev[sessionId], status: 'error', persistence: result } }));
      return false;
    };
    let saved: SaveResult;
    try {
      saved = await updateSession(sessionId, current => ({ ...current, messages:
        buildUserTurnTree(migrateFlatToTree(current.messages), { userText, attachment, parentId, userMsgId, timestamp: sentAt }).tree, updatedAt }));
    } catch { return fail(unknownSave()); }
    if (!ownsStream()) return false;
    if (!isSaved(saved) || controller.signal.aborted || (!incognito && !isRequestScopeCurrent(requestScope))) {
      return fail(saved);
    }
    let producer: SessionProducer | null;
    try { producer = beginProducer(sessionId); } catch { return fail(unknownSave()); }
    if (!producer) return fail(unknownSave());
    const latest = getSession(sessionId);
    if (!latest) return fail(unknownSave());
    tree = appendAssistantNode(migrateFlatToTree(latest.messages), assistantMsgId).tree;
    commit(prev => ({ ...prev, [sessionId]: { ...prev[sessionId], status: 'waiting', producer, tree } }));

    // 組 API 訊息序列（線性路徑去掉待填的 assistant；user 節點以 resolvedInput 送出）。
    // 訊息組裝與插話標註（ADR-0249）集中於 buildApiHistory（SSOT、純函式）。
    const messagesForStream: ChatMessage[] = buildApiHistory(getLinearMessages(tree).slice(0, -1), userMsgId, resolvedInput);

    const { onChunk, onThinking, onEvent, onDone, onError } = createStreamCallbacks(sessionId, controller, onSkillInstallRequest, onAskUser);

    // debug 模擬模式：sim handlers 餵資料給本串流的回調（需在 fetchChatStream 之前註冊）
    registerSimHandlers({ onChunk, onEvent, onDone });

    void fetchChatStream(
      messagesForStream,
      { provider: "proxy", baseUrl: proxyUrl, model, protocol, project_id: projectId, session_id: sessionId,
        auth_level: authLevel, active_skills: activeSkills, incognito, deepResearch, mcpDispatcher, requestScope,
        ui_persistence: incognito ? { mode: 'incognito', user_parent_id: userMsgId, assistant_node_id: assistantMsgId } :
          { mode: 'persistent', revision: saved.status === 'saved' ? saved.revision : '',
            user_parent_id: userMsgId, assistant_node_id: assistantMsgId } },
      onChunk, onDone, onError, onEvent, controller.signal, onBeforeSend, onThinking,
    );

    return true;
  }, [commit, createStreamCallbacks, registerSimHandlers, updateSession, beginProducer, getSession, getPersistence, t]);

  // ── ADR-0081：重整後接回進行中的串流 ──────────────────────────────
  const resumeStream = useCallback(async (sessionId: string, value: unknown) => {
    if (streamsRef.current[sessionId]) return; // 已有 entry（本分頁已在跑）不重複接
    const atCapacity = () => Object.values(streamsRef.current).filter(stream =>
      ['saving', 'waiting', 'streaming'].includes(stream.status)).length >= MAX_CONCURRENT_STREAMS;
    if (atCapacity()) return;
    const state = parseBufferedStreamState(value, sessionId);
    if (!state?.started) return;
    const requestScope = captureRequestScope();
    if (!isRequestScopeCurrent(requestScope)) return;
    const sess = await prepareResume(sessionId);
    if (!sess) return;
    if (!isRequestScopeCurrent(requestScope) || streamsRef.current[sessionId] || atCapacity()) return;
    const assistantMsgId = state.started.assistant_node_id;
    let tree: TreeHistory;
    try { tree = restoreAssistantNode(migrateFlatToTree(sess.messages), state.started.user_parent_id, assistantMsgId); }
    catch { return; }
    // 從頭重播只重建本回合的串流欄位；保留既有節點其餘擴充、附件及其他分支。
    tree = { ...tree, messages: { ...tree.messages, [assistantMsgId]: {
      ...tree.messages[assistantMsgId], content: '', thinking: '', steps: [], progress: {}, interjections: [], interruptions: [],
    } } };
    const producer = beginProducer(sessionId);
    if (!producer) return;
    const controller = new AbortController();

    commit(prev => ({
      ...prev,
      [sessionId]: { sessionId, controller, tree, assistantMsgId, aiText: "", aiThinking: "", status: "streaming", model: sess.agent_id || "",
        requestScope, producer, started: state.started ?? undefined, incognito: sess.incognito },
    }));

    const { onChunk, onThinking, onEvent, onDone, onError } = createStreamCallbacks(sessionId, controller);
    // 從頭重播（前端記憶體已因重整丟失，需完整重建），之後跟流至終態
    void resumeChatStream(
      requestScope!.proxyOrigin, sessionId, -1,
      onChunk, onDone, onError, onEvent, controller.signal, onThinking,
      { started: state.started, requestScope },
    );
  }, [prepareResume, beginProducer, commit, createStreamCallbacks]);

  const checkedSessions = useRef(new Set<string>());
  const checkSessionStream = useCallback(async (sessionId: string) => {
    const session = getSession(sessionId);
    if (!session || streamsRef.current[sessionId] || checkedSessions.current.has(sessionId) || session.incognito) return;
    const scope = captureRequestScope();
    if (!isRequestScopeCurrent(scope)) return;
    checkedSessions.current.add(sessionId);
    try {
      const response = await fetchTouka(scope!.proxyOrigin, `/v1/ui/sessions/${encodeURIComponent(sessionId)}/stream/state`, { requestScope: scope });
      const body: unknown = response.ok ? await response.json() : null;
      if (!isRequestScopeCurrent(scope) || !body || typeof body !== 'object' || !('state' in body)) return;
      await resumeStream(sessionId, body.state);
    } catch { /* 狀態不可用不能當作已保存，也不自動重送模型。 */ }
  }, [getSession, resumeStream]);

  const resumedRef = useRef(false);
  useEffect(() => {
    // sessions 載入後執行一次：查詢進行中串流並逐條接回（含背景多條，側欄 spinner 隨之恢復）
    if (resumedRef.current || sessions.length === 0) return;
    resumedRef.current = true;
    void (async () => {
      try {
        const scope = captureRequestScope();
        if (!isRequestScopeCurrent(scope)) return;
        const res = await fetchTouka(scope!.proxyOrigin, "/v1/ui/streams/active", { requestScope: scope });
        if (!res.ok) return;
        const data = await res.json();
        if (!isRequestScopeCurrent(scope)) return;
        for (const st of (data.streams ?? [])) {
          void resumeStream(st.session_id as string, st);
        }
      } catch (e) {
        logSSOTError(e, "StreamRegistry:resumeActiveStreams");
      }
    })();
  }, [sessions, resumeStream]);

  const stopStream = useCallback((sessionId: string) => {
    const s = streamsRef.current[sessionId];
    if (!s || s.settling || !isRequestScopeCurrent(s.requestScope)) return;
    if (!s.producer) {
      s.controller.abort();
      commit(prev => { const next = { ...prev }; delete next[sessionId]; return next; });
      return;
    }
    void (async () => {
      let started = s.started;
      if (!started) {
        const response = await fetchTouka(s.requestScope!.proxyOrigin,
          `/v1/ui/sessions/${encodeURIComponent(sessionId)}/stream/state`, { requestScope: s.requestScope });
        const body: unknown = response.ok ? await response.json() : null;
        if (!isRequestScopeCurrent(s.requestScope) || !body || typeof body !== 'object' || !('state' in body)) return;
        started = parseBufferedStreamState(body.state, sessionId)?.started ?? undefined;
      }
      if (!started || started.assistant_node_id !== s.assistantMsgId ||
          started.user_parent_id !== s.tree.messages[s.assistantMsgId]?.parentId || !isRequestScopeCurrent(s.requestScope)) return;
      await fetchTouka(s.requestScope!.proxyOrigin,
        `/v1/ui/sessions/${encodeURIComponent(sessionId)}/stop?stream_id=${encodeURIComponent(started.stream_id)}`,
        { method: 'POST', requestScope: s.requestScope });
    })().catch(() => {});
    const callbacks = createStreamCallbacks(sessionId, s.controller);
    // 先退休回調接收權，再 abort；讀取層的晚到 AbortError 不得完成第二次。
    void callbacks.onError({ name: 'AbortError' });
    s.controller.abort();
  }, [commit, createStreamCallbacks]);

  useEffect(() => () => {
    for (const stream of Object.values(streamsRef.current)) stream.controller.abort();
    streamsRef.current = {};
  }, []);

  const streamingSessionIds = Object.values(streams).filter(stream =>
    ['saving', 'waiting', 'streaming'].includes(stream.status)).map(stream => stream.sessionId);

  const value = React.useMemo(() => ({
    startStream, stopStream, getStream, checkSessionStream, streamingSessionIds, activeCount: streamingSessionIds.length,
  }), [startStream, stopStream, getStream, checkSessionStream, streamingSessionIds]);

  return (
    <StreamRegistryContext.Provider value={value}>
      {children}
    </StreamRegistryContext.Provider>
  );
}

export function useStreamRegistry() {
  const ctx = useContext(StreamRegistryContext);
  if (!ctx) throw new Error("useStreamRegistry must be used within StreamRegistryProvider");
  return ctx;
}
