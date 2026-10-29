"use client";

import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from "react";
import { TreeHistory, MessageNode, migrateFlatToTree } from "../tree_history";
import { getActiveProxyUrl, fetchTouka, captureRequestScope, isRequestScopeCurrent } from "../connections";
import { useArtifacts } from "@/hooks/useArtifacts";
import { useModels, ModelInfo } from "./ModelProvider";
import { pickDefaultModel } from "../default_model";
import { useSessionList } from "./SessionListProvider";
import { Session, SkillInstallRequest, AskUserRequest, SaveResult } from "../types";
import { getSystemEpoch } from "../time_utils";
import { useArtifactExtractor } from "@/hooks/useArtifactExtractor";
import { useChatStream } from "@/hooks/useChatStream";
import { useStreamRegistry } from "./StreamRegistryProvider";
import { logSSOTError } from "@/lib/error_handler";
import { toast } from "sonner";
import { useTranslation } from "react-i18next";

import type { ChatAttachment as Attachment } from "@/lib/chat/attachment";

interface ChatContextType {
  activeSessionId: string;
  setActiveSessionId: (id: string) => void;
  history: TreeHistory;
  setHistory: React.Dispatch<React.SetStateAction<TreeHistory>>;
  isSending: boolean;
  isWaiting: boolean;
  inputValue: string;
  setInputValue: (val: string) => void;
  attachment: Attachment | null;
  setAttachment: (val: Attachment | null) => void;
  handleSend: (customText?: string, parentIdOverride?: string | null) => Promise<void>;
  handleStopGeneration: () => void;
  handleNewSession: (projectId?: string, incognito?: boolean) => void;
  handleSwitchSession: (id: string) => void;
  handleHomeSession: () => void;
  handleSetHome: (id: string) => Promise<void>;
  handleUnsetHome: (id: string) => Promise<void>;
  handleDeleteMessage: (id: string) => void;
  handleSwitchMessage: (id: string) => void;
  handleRegenerate: (id: string) => void;
  handleEditMessage: (id: string, newContent: string) => void;
  handleUpdateSessionMessages: (id: string, newMessages: MessageNode[], expectedBase?: Session) => Promise<SaveResult>;
  handleSetProject: (sessionId: string, projectId: string | null) => Promise<void>;
  authLevel: string;
  /** 無痕對話無法追溯，後端一律以 Manual 執行（ADR-0246 U2c-9）。 */
  isIncognito: boolean;
  handleSetAuthLevel: (level: string) => void;
  pendingInstall: SkillInstallRequest | null;
  setPendingInstall: (req: SkillInstallRequest | null) => void;
  /** HAX G10（ADR-0085）：ask_user 工具的待答提問（觸發 AskUserModal）。 */
  pendingAsk: AskUserRequest | null;
  setPendingAsk: (req: AskUserRequest | null) => void;
  /** ADR-0080：進行中（含背景）串流的 session id 清單，供側欄顯示生成指示。 */
  streamingSessionIds: string[];
}

const ChatContext = createContext<ChatContextType | undefined>(undefined);

/** 還沒有任何訊息的對話（第一句前換模型要算數，R-2c ②）。 */
const isEmptySession = (sess?: Session) => Object.keys(migrateFlatToTree(sess?.messages).messages).length === 0;

export function ChatProvider({ children }: { children: React.ReactNode }) {
  const { t } = useTranslation();
  const { availableModels, selectedModel, setSelectedModel } = useModels();
  const { sessions, setSessions, syncSession, updateSession, refreshSessions } = useSessionList();

  const { getStream, streamingSessionIds, checkSessionStream } = useStreamRegistry();

  const [activeSessionId, setActiveSessionId] = useState<string>("");

  useEffect(() => {
    if (typeof window !== 'undefined') {
      const params = new URLSearchParams(window.location.search);
      const session = params.get('session');
      // eslint-disable-next-line react-hooks/set-state-in-effect -- mount-once URL 初始化
      if (session) setActiveSessionId(session);
    }
  }, []);
  // staticHistory：無進行中串流時的顯示樹，亦為 edit/delete/branch 結構操作的來源（SSOT）。
  // 進行中串流的顯示樹由 StreamRegistry 衍生（見下方 history）。
  const [staticHistory, setStaticHistory] = useState<TreeHistory>({ messages: {}, currentId: null });
  const setHistory = setStaticHistory; // 結構操作沿用既有 setHistory 介面，實際作用於 staticHistory
  const [authLevel, setAuthLevel] = useState<string>("Manual");

  // ── 顯示衍生（ADR-0080）：active session 若有進行中串流 → 顯示登錄表即時樹；否則靜態樹 ──
  const activeStream = getStream(activeSessionId);
  const storedSession = sessions.find(session => session.id === activeSessionId);
  const storedSessionId = storedSession?.id;
  const isIncognito = !!storedSession?.incognito;
  const history = activeStream ? activeStream.tree : storedSession ? migrateFlatToTree(storedSession.messages) : staticHistory;
  const isSending = !!activeStream && ['saving', 'waiting', 'streaming'].includes(activeStream.status);
  const isWaiting = !!activeStream && activeStream.status === 'waiting';
  useEffect(() => {
    if (activeSessionId && storedSessionId) void checkSessionStream(activeSessionId);
  }, [activeSessionId, storedSessionId, checkSessionStream]);

  const [syncedId, setSyncedId] = useState("");
  
  // 當 session ID 確定且 session 清單載入後，進行狀態同步
  // 使用 React 18+ 推薦的 "Adjusting state during render" 模式來避免 useEffect 的連鎖渲染
  if (activeSessionId && activeSessionId !== syncedId && sessions.length > 0) {
    const sess = sessions.find(s => s.id === activeSessionId);
    if (sess) {
      setSyncedId(activeSessionId);
      setHistory(migrateFlatToTree(sess.messages));
      setAuthLevel(sess.auth_level || "Manual");
    }
  }

  // 僅保留外部 Provider 的連動 (如 ModelProvider)，因為在渲染階段修改外部狀態是不安全的
  useEffect(() => {
    if (activeSessionId && sessions.length > 0) {
      const sess = sessions.find(s => s.id === activeSessionId);
      // 還沒有訊息的對話不拉回：第一句前換模型要算數（R-2c ②）
      if (sess?.agent_id && !isEmptySession(sess)) {
        setSelectedModel(sess.agent_id);
      }
    }
  }, [activeSessionId, sessions, setSelectedModel]);
  const [inputValue, _setInputValue] = useState("");
  const inputValueRef = useRef("");
  const selectedModelRef = useRef(selectedModel);
  useEffect(() => {
    selectedModelRef.current = selectedModel;
  }, [selectedModel]);
  const draftsRef = useRef<Map<string, { text: string; modelId: string }>>(new Map());
  const setInputValue = useCallback((val: string) => {
    _setInputValue(val);
    inputValueRef.current = val;
  }, []);

  const [attachment, setAttachment] = useState<Attachment | null>(null);
  const [pendingInstall, setPendingInstall] = useState<SkillInstallRequest | null>(null);
  const [pendingAsk, setPendingAsk] = useState<AskUserRequest | null>(null);

  const { processStreamChunk, finalizeStream } = useArtifactExtractor(activeSessionId, isSending, history);

  // HAX G17 加固（批2 ⑤a）：無痕 session「誕生即登記」。無痕 session 僅存前端記憶體
  // （syncSession 依 ADR-0086 永不落盤），refreshSessions 整批覆蓋會把它洗出 sessions；
  // 發送時的旗標若只回查清單，wipe 競態下會以一般模式送出（後端零殘留閘門失守）。
  // 此 ref 保存本分頁建立過的無痕 id（壽命與分頁一致、id 為 epoch-ms 不重用），供
  // useChatStream 兜底查詢。
  const incognitoIdsRef = useRef<Set<string>>(new Set());
  const isIncognitoSession = useCallback((id: string) => incognitoIdsRef.current.has(id), []);

  const { handleSendStream, handleStopGeneration } = useChatStream({
    staticHistory, activeSessionId, setActiveSessionId, setPendingInstall, setPendingAsk,
    isIncognitoSession,
  });

  // ── ADR-0080 artifact 協調：active session 串流即時更新 + 完成回填 staticHistory ──
  // 即時 artifact 只對 active session 生效（背景串流不打擾面板）；串流完成（active 的 entry
  // 消失）時 finalize（開面板）並把已寫回 sessions 的完成樹回填 staticHistory，使顯示無縫接續。
  const prevStreamRef = useRef<{ had: boolean; aiText: string }>({ had: false, aiText: '' });
  useEffect(() => {
    if (activeStream) {
      if (activeStream.aiText) processStreamChunk(activeStream.aiText);
      prevStreamRef.current = { had: true, aiText: activeStream.aiText };
    } else if (prevStreamRef.current.had && activeSessionId) {
      finalizeStream(prevStreamRef.current.aiText);
      const sess = sessions.find(s => s.id === activeSessionId);
      // eslint-disable-next-line react-hooks/set-state-in-effect -- 同步串流完成結果回 staticHistory
      if (sess) setStaticHistory(migrateFlatToTree(sess.messages));
      prevStreamRef.current = { had: false, aiText: '' };
    }
  }, [activeStream, activeSessionId, sessions, processStreamChunk, finalizeStream]);

  const activeIdRef = useRef(activeSessionId);
  useEffect(() => { activeIdRef.current = activeSessionId; }, [activeSessionId]);

  const handleSend = useCallback(async (customText?: string, parentIdOverride?: string | null) => {
    const textToSend = customText ?? inputValueRef.current;
    if ((!textToSend.trim() && !attachment) || isSending) return;
    
    useArtifacts.getState().setManuallyClosed(false);

    // 空對話第一句前換了模型：先重綁 agent_id，讓後端照新的 agent 重驗模式，畫面顯示存下的模式（R-2c ②）
    const sess = sessions.find(s => s.id === activeSessionId);
    if (sess?.agent_id && selectedModel && sess.agent_id !== selectedModel && !isIncognito && isEmptySession(sess)) {
      const updatedAt = getSystemEpoch();
      const saved = await updateSession(activeSessionId, current => ({ ...current, agent_id: selectedModel, updatedAt }));
      if (saved.status !== 'saved') return;
      setAuthLevel((await refreshSessions()).find(s => s.id === activeSessionId)?.auth_level || 'Manual');
    }
    return handleSendStream(textToSend, attachment, parentIdOverride, () => {
        if (!customText) setInputValue("");
        setAttachment(null);
    }, sessionId => {
        // hook 擋下（ADR-0292）：文字放回還在這個對話、而且還空著的輸入框
        if (!customText && activeIdRef.current === sessionId && !inputValueRef.current) setInputValue(textToSend);
    });
  }, [isSending, attachment, sessions, activeSessionId, selectedModel, isIncognito, updateSession,
      refreshSessions, setAuthLevel, handleSendStream, setInputValue]);

  // 後端只讓 admin 或靈魂擁有者存成 Auto／YOLO，其他人會被存成 Manual（ADR-0246）。
  // 存檔後重讀清單，顯示後端實際存下的模式，不等重新載入。
  const authRequestRef = useRef(0);
  const handleSetAuthLevel = useCallback((level: string) => {
    if (isIncognito && level !== 'Manual') { // 無痕無法追溯，後端一律 Manual（U2c-9）
      setAuthLevel('Manual');
      toast.warning(t('chat.auth_switcher.incognito_manual'));
      return;
    }
    setAuthLevel(level);
    if (!activeSessionId) return;
    const sessionId = activeSessionId;
    const request = ++authRequestRef.current; // 之後又換模式時，舊的回覆不覆蓋顯示
    const updatedAt = getSystemEpoch();
    // 新對話還沒發第一句就切模式：選的是靈魂才一起帶上，後端才能驗擁有權（R-2c ②）。一般模型
    // 沒有工具、模式對它沒有意義，不綁，免得之後選靈魂時被拉回預設模型。
    const agentId = availableModels.find(m => m.id === selectedModel)?.is_agent ? selectedModel : '';
    void (async () => {
      const result = await updateSession(sessionId, current => ({
        ...current, auth_level: level, updatedAt,
        ...(!current.agent_id && agentId ? { agent_id: agentId } : {}),
      }));
      if (result.status !== 'saved' || level === 'Manual') return;
      const actual = (await refreshSessions()).find(s => s.id === sessionId)?.auth_level;
      if (actual !== 'Manual' || authRequestRef.current !== request || activeIdRef.current !== sessionId) return;
      setAuthLevel(actual);
      toast.warning(t('chat.auth_switcher.downgraded', { requested: level, actual }));
    })().catch(e => logSSOTError(e, "ChatProvider:setAuthLevel"));
  }, [activeSessionId, isIncognito, availableModels, selectedModel, updateSession, refreshSessions, setAuthLevel, t]);

  const handleNewSession = useCallback((projectId?: string, incognito?: boolean) => {
    // ADR-0080：開新對話不再中止進行中的串流（背景續跑）；防污染由 per-session 隔離達成
    // 如果目前選取的是靈魂代理人，開啟新對話時自動切換回預設模型，避免污染
    const currentModelInfo = availableModels.find((m: ModelInfo) => m.id === selectedModel);
    // 找不到可用的一般模型就不切換、不提示（不寫死模型名，ADR-0250）。
    const defaultM = currentModelInfo?.is_agent || selectedModel.startsWith("ToukaSoul")
        ? pickDefaultModel(availableModels) : undefined;
    if (defaultM) {
        setSelectedModel(defaultM.id);
        // HAX G14（H3）：離開代理人對話、自動重設模型時也明示，避免靜默的行為變動。
        toast.info(t('chat.provider.model_switched_to_default', { model: defaultM.name || defaultM.id }));
    }

    const newId = `session-${getSystemEpoch()}`;
    if (incognito) incognitoIdsRef.current.add(newId); // HAX G17 加固（⑤a）：誕生即登記
    const newSess: Session = {
      id: newId, title: incognito ? "無痕對話" : "新對話", messages: [], updatedAt: getSystemEpoch(),
      project_id: projectId, incognito
      // 不在此時寫死 agent_id，留待第一次發言時決定
    };
    setSessions(prev => [newSess, ...prev]);
    setActiveSessionId(newId);
    setHistory({ messages: {}, currentId: null });
    setInputValue("");
    draftsRef.current.delete(newId);
    setAttachment(null);
    void syncSession(newSess);
  }, [t, availableModels, selectedModel, setSelectedModel, setSessions, syncSession, setInputValue, setHistory]);

  const switchGeneration = useRef(0);
  const handleSwitchSession = useCallback(async (id: string) => {
    const scope = captureRequestScope();
    const generation = ++switchGeneration.current;
    // ADR-0080：切換對話不再中止串流——離開的對話在背景續跑，切回時從登錄表衍生顯示。
    // 防污染由 per-session 隔離達成（chunk 只寫進自己 sid 的樹）。
    // Save current draft and model before switching
    if (activeSessionId) {
      draftsRef.current.set(activeSessionId, { 
        text: inputValueRef.current, 
        modelId: selectedModelRef.current 
      });
    }
    setActiveSessionId(id);
    const targetSess = sessions.find(s => s.id === id);
    if (targetSess) {
      setHistory(migrateFlatToTree(targetSess.messages));
      
      // Restore draft for target session if exists
      const draft = draftsRef.current.get(id);
      if (draft) {
        setInputValue(draft.text);
        setSelectedModel(draft.modelId);
      } else if (targetSess.agent_id) {
        setSelectedModel(targetSess.agent_id);
      }
      setAuthLevel(targetSess.auth_level || "Manual");
    }

    // 進行背景刷新以取得可能由 Heartbeat 在背景生成的最新訊息
    try {
      const freshSessions = await refreshSessions();
      if (!isRequestScopeCurrent(scope) || generation !== switchGeneration.current) return;
      const freshSess = freshSessions.find(s => s.id === id);
      if (freshSess && JSON.stringify(freshSess.messages) !== JSON.stringify(targetSess?.messages)) {
        setHistory(migrateFlatToTree(freshSess.messages));
      }
    } catch (e) {
      logSSOTError(e, "ChatProvider:refreshSession");
    }
  }, [sessions, setSelectedModel, activeSessionId, setInputValue, setAuthLevel, refreshSessions, setHistory]);

  const handleHomeSession = useCallback(async () => {
    const homeSess = sessions.find(s => s.is_home);
    if (homeSess) {
      await handleSwitchSession(homeSess.id);
    } else {
      // ADR-0080：建立家對話不再中止進行中的串流（背景續跑）
      const newId = `session-home-${getSystemEpoch()}`;
      const newSess: Session = { id: newId, title: "妳的家", messages: [], updatedAt: getSystemEpoch(), is_home: true };
      setSessions(prev => [newSess, ...prev]);
      setActiveSessionId(newId);
      setHistory({ messages: {}, currentId: null });
      setInputValue("");
      draftsRef.current.delete(newId);
      setAttachment(null);
      void syncSession(newSess);
    }
  }, [sessions, handleSwitchSession, setSessions, syncSession, setInputValue, setHistory]);

  const handleSetHome = useCallback(async (id: string) => {
    const sess = sessions.find(s => s.id === id);
    if (!sess) return;
    const agentId = sess.agent_id || "ToukaGroup";
    const proxyUrl = getActiveProxyUrl();
    try {
      const res = await fetchTouka(proxyUrl, '/v1/ui/sessions/set_home', {
        method: "POST",
        body: JSON.stringify({ session_id: id, agent_id: agentId, is_home: true })
      });
      if (res.ok) {
        setSessions(prev => prev.map(s => (s.agent_id === agentId && s.is_home) ? { ...s, is_home: false } : (s.id === id ? { ...s, is_home: true, home_for_agent: agentId } : s)));
      }
    } catch (err) { logSSOTError(err, "ChatProvider:setHome"); }
  }, [sessions, setSessions]);

  const handleUnsetHome = useCallback(async (id: string) => {
    const sess = sessions.find(s => s.id === id);
    if (!sess) return;
    const agentId = sess.agent_id || "ToukaGroup";
    const proxyUrl = getActiveProxyUrl();
    try {
      const res = await fetchTouka(proxyUrl, '/v1/ui/sessions/set_home', {
        method: "POST",
        body: JSON.stringify({ session_id: id, agent_id: agentId, is_home: false })
      });
      if (res.ok) {
        setSessions(prev => prev.map(s => s.id === id ? { ...s, is_home: false, home_for_agent: undefined } : s));
      }
    } catch (err) { logSSOTError(err, "ChatProvider:unsetHome"); }
  }, [sessions, setSessions]);

  const handleUpdateSessionMessages = useCallback(async (id: string, newMessages: MessageNode[], expectedBase?: Session) => {
    const updatedAt = getSystemEpoch();
    const pending = updateSession(id, current => ({ ...current, messages: newMessages, updatedAt }), {}, expectedBase);
    if (expectedBase === undefined && id === activeSessionId) {
      setHistory(migrateFlatToTree(newMessages));
    }
    return pending;
  }, [updateSession, activeSessionId, setHistory]);

  const handleSetProject = useCallback(async (sessionId: string, projectId: string | null) => {
    const updatedAt = getSystemEpoch();
    await updateSession(sessionId, current => ({ ...current, project_id: projectId, updatedAt }));
  }, [updateSession]);

  // 遞迴收集所有子孫 ID (含分支)
  const getAllDescendantIds = useCallback((messages: Record<string, MessageNode>, id: string): string[] => {
    const ids: string[] = [];
    const queue = [...(messages[id]?.childrenIds || [])];
    while (queue.length > 0) {
      const cid = queue.shift()!;
      ids.push(cid);
      if (messages[cid]?.childrenIds) queue.push(...messages[cid].childrenIds);
    }
    return ids;
  }, []);

  const handleDeleteMessage = useCallback((id: string) => {
    const changeTree = (prev: TreeHistory): TreeHistory => {
      const msg = prev.messages[id];
      if (!msg) return prev;

      const parent = msg.parentId ? prev.messages[msg.parentId] : null;

      // 安全刪除條件：
      // 1. 葉節點 (沒有子訊息) — 類似退格鍵撤回
      // 2. 有平行分支 (父節點有多個孩子) — 可安全剪取此分支
      const isLeaf = msg.childrenIds.length === 0;
      const hasSiblings = parent ? parent.childrenIds.length > 1 : false;

      if (!isLeaf && !hasSiblings) {
        // 中間的唯一鏈結節點，刪除會造成對話斷裂，拒絕操作
        return prev;
      }

      const newMessages = { ...prev.messages };
      const descendants = getAllDescendantIds(newMessages, id);

      // 刪除目標節點及其所有後代
      [id, ...descendants].forEach(targetId => {
        delete newMessages[targetId];
      });

      // currentId 切換回父節點
      let newCurrentId = prev.currentId;
      if (id === prev.currentId || descendants.includes(prev.currentId as string)) {
        newCurrentId = msg.parentId;
      }

      // 從父節點的子清單中移除自己
      if (msg.parentId && newMessages[msg.parentId]) {
        newMessages[msg.parentId] = {
          ...newMessages[msg.parentId],
          childrenIds: newMessages[msg.parentId].childrenIds.filter(cid => cid !== id)
        };
      }

      return { messages: newMessages, currentId: newCurrentId };
    };
    const updatedAt = getSystemEpoch();
    setHistory(changeTree);
    if (activeSessionId) void updateSession(activeSessionId, current => ({ ...current,
      messages: changeTree(migrateFlatToTree(current.messages)), updatedAt }));
  }, [activeSessionId, updateSession, getAllDescendantIds, setHistory]);

  const handleSwitchMessage = useCallback((id: string) => {
    const changeTree = (previous: TreeHistory) => previous.messages[id] ? { ...previous, currentId: id } : previous;
    const updatedAt = getSystemEpoch();
    setHistory(changeTree);
    if (activeSessionId) void updateSession(activeSessionId, current => ({ ...current,
      messages: changeTree(migrateFlatToTree(current.messages)), updatedAt }));
  }, [activeSessionId, updateSession, setHistory]);

  const handleRegenerate = useCallback((id: string) => {
    const msg = history.messages[id];
    if (!msg || !msg.parentId) return;
    const parentMsg = history.messages[msg.parentId];
    if (!parentMsg) return;
    handleSend(parentMsg.content, msg.parentId);
  }, [history, handleSend]);

  const handleEditMessage = useCallback((id: string, newContent: string) => {
    const msg = history.messages[id];
    if (!msg) return;
    if (msg.content === newContent) return;
    handleSend(newContent, msg.parentId);
  }, [history, handleSend]);

  const value = React.useMemo(() => ({
    activeSessionId, setActiveSessionId, history, setHistory, 
    isSending, isWaiting, inputValue, setInputValue, attachment, setAttachment,
    handleSend, handleStopGeneration, handleNewSession, handleSwitchSession,
    handleHomeSession, handleSetHome, handleUnsetHome,
    handleDeleteMessage, handleSwitchMessage, handleRegenerate, handleEditMessage,
    handleUpdateSessionMessages, handleSetProject,
    authLevel, isIncognito, handleSetAuthLevel, pendingInstall, setPendingInstall,
    pendingAsk, setPendingAsk,
    streamingSessionIds,
  }), [
    activeSessionId, history, isSending, isWaiting, inputValue, attachment, setInputValue, setHistory,
    setAttachment, handleSend, handleStopGeneration, handleNewSession, handleSwitchSession,
    handleHomeSession, handleSetHome, handleUnsetHome,
    handleDeleteMessage, handleSwitchMessage, handleRegenerate, handleEditMessage,
    handleUpdateSessionMessages, handleSetProject,
    authLevel, isIncognito, handleSetAuthLevel, pendingInstall, setPendingInstall,
    pendingAsk, setPendingAsk,
    streamingSessionIds,
  ]);

  return (
    <ChatContext.Provider value={value}>
      {children}
    </ChatContext.Provider>
  );
}

export function useChat() {
  const context = useContext(ChatContext);
  if (!context) throw new Error("useChat must be used within ChatProvider");
  return context;
}
