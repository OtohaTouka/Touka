import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { getSystemEpoch } from '../lib/time_utils';
import { toast } from 'sonner';
import { getActiveConnection, getActiveProxyUrl, captureRequestScope, isRequestScopeCurrent } from '../lib/connections';
import { resolveMentions } from '../lib/mentions';
import { TreeHistory } from '../lib/tree_history';
import type { ChatAttachment } from '../lib/chat/attachment';
import { Session, BackendProtocol, SkillInstallRequest, AskUserRequest } from '../lib/types';
import { useModels } from '../lib/providers/ModelProvider';
import { useSessionList } from '../lib/providers/SessionListProvider';
import { useDebug } from '../lib/providers/DebugProvider';
import { useStreamRegistry } from '../lib/providers/StreamRegistryProvider';
import { isSaved } from '../lib/session_persistence';
import { errorMsg } from '../lib/error_handler';

interface UseChatStreamOptions {
  /** 當前顯示樹（建節點基底）；無進行中串流時等同 staticHistory。 */
  staticHistory: TreeHistory;
  activeSessionId: string;
  setActiveSessionId: (id: string) => void;
  setPendingInstall: React.Dispatch<React.SetStateAction<SkillInstallRequest | null>>;
  setPendingAsk: React.Dispatch<React.SetStateAction<AskUserRequest | null>>;
  /**
   * HAX G17 加固（批2 ⑤a）：無痕 id 兜底查詢（ChatProvider 誕生登記）。
   * 無痕 session 僅存前端記憶體，refreshSessions 整批覆蓋會把它洗出清單；
   * 發送時若只回查 sessions 會在 wipe 競態下遺失旗標 → 以一般模式送出（隱私缺口）。
   */
  isIncognitoSession?: (id: string) => boolean;
}

/**
 * ADR-0080 背景續跑：useChatStream 瘦身為「發送前準備 + 委派 StreamRegistry」。
 *
 * 串流的生命週期、累積、完成寫回、debug 模擬皆下沉至 StreamRegistry（per-session SSOT）。
 * 本 hook 只負責：解析 mention、建立新對話（無 active 時）、組裝啟動參數、委派 startStream。
 * 切換對話不再中止串流（背景續跑）；停止改為 per-session 的 stopStream(activeSessionId)。
 */
export function useChatStream({
  staticHistory, activeSessionId, setActiveSessionId, setPendingInstall, setPendingAsk,
  isIncognitoSession,
}: UseChatStreamOptions) {
  const { t } = useTranslation();
  const { selectedModel } = useModels();
  const { getSession, syncSession, updateSession } = useSessionList();
  const { setLastRequestPayload, setIsSimulationMode } = useDebug();
  const { startStream, stopStream } = useStreamRegistry();

  const handleStopGeneration = useCallback(() => {
    if (activeSessionId) stopStream(activeSessionId);
  }, [activeSessionId, stopStream]);

  const handleSendStream = useCallback(async (
    textToSend: string,
    attachment: ChatAttachment | null,
    parentIdOverride?: string | null,
    onSuccessResetInput?: () => void,
  ) => {
    const scope = captureRequestScope();
    let sessionId = activeSessionId;
    const baseTree: TreeHistory = sessionId ? staticHistory : { messages: {}, currentId: null };
    const finalParentId = parentIdOverride !== undefined ? parentIdOverride : baseTree.currentId;

    // 無 active session → 建立新對話
    if (!sessionId) {
      if (!selectedModel) { toast.warning(t('hooks.use_chat_stream.select_agent_first')); return; }
      const newId = `session-${getSystemEpoch()}`;
      const newSess: Session = {
        id: newId, title: textToSend.slice(0, 20) || t('hooks.use_chat_stream.new_conversation_title'), messages: [],
        updatedAt: getSystemEpoch(), agent_id: selectedModel,
      };
      setActiveSessionId(newId);
      if (!isSaved(await syncSession(newSess)) || !isRequestScopeCurrent(scope)) return;
      sessionId = newId;
    } else {
      let current = getSession(sessionId);
      if (!current && isIncognitoSession?.(sessionId)) {
        // 誕生登記只可重建記憶體無痕，不能據缺席一般 session 自動重建遠端資料。
        const restored: Session = { id: sessionId, title: '無痕對話', messages: baseTree,
          updatedAt: getSystemEpoch(), incognito: true, agent_id: selectedModel };
        if (!isSaved(await syncSession(restored))) return;
        current = getSession(sessionId);
      }
      if (!current) { toast.error(errorMsg('SESSION_SAVE_FAILED')); return; }
      if (!current.agent_id) {
        const updatedAt = getSystemEpoch();
        if (!isSaved(await updateSession(sessionId, session => ({ ...session, agent_id: selectedModel, updatedAt })))) return;
      }
    }
    if (!isRequestScopeCurrent(scope)) return;

    const conn = getActiveConnection();
    const proxyUrl = getActiveProxyUrl();
    const protocol = (conn?.protocol as BackendProtocol) || 'openai';
    const resolvedInput = await resolveMentions(textToSend);
    if (!isRequestScopeCurrent(scope)) return;

    const currentSess = getSession(sessionId);
    const projectId = currentSess?.project_id;
    const authLevel = currentSess?.auth_level || "Manual";
    const activeSkills = currentSess?.active_skills ?? [];
    // HAX G17：無痕旗標出口＝清單 ∪ 誕生登記（⑤a：清單被 refreshSessions 整批覆蓋
    // 洗掉無痕 session 時，登記兜底確保旗標不遺失、後端零殘留閘門不失守）。
    const incognito = (currentSess?.incognito || isIncognitoSession?.(sessionId)) || undefined;
    const deepResearch = currentSess?.deepResearch; // ADR-0087：深入研究旗標出口
    const mcpDispatcher = currentSess?.mcpDispatcher; // ADR-0161：MCP 動態掛載旗標出口

    const onBeforeSend = async (payload: Record<string, unknown>) => {
      setLastRequestPayload(payload);
      // DebugAgent：攔截真實連線、改走模擬模式
      if (selectedModel === "DebugAgent") {
        setIsSimulationMode(true);
        return false;
      }
    };

    const started = await startStream({
      sessionId, baseTree, userText: textToSend, resolvedInput, attachment, parentId: finalParentId,
      model: selectedModel, protocol, proxyUrl, projectId: projectId ?? undefined, authLevel, activeSkills,
      onBeforeSend, onSkillInstallRequest: setPendingInstall, onAskUser: setPendingAsk, incognito, deepResearch, mcpDispatcher,
    });

    if (!isRequestScopeCurrent(scope)) return;
    // 額滿提示屬 registry 的 admission；保存失敗由保存狀態顯示，不能誤稱額滿。
    if (started) onSuccessResetInput?.();
  }, [
    selectedModel, getSession, activeSessionId, staticHistory,
    updateSession, syncSession, setActiveSessionId, setPendingInstall, setPendingAsk,
    setLastRequestPayload, setIsSimulationMode, startStream, isIncognitoSession, t,
  ]);

  return { handleSendStream, handleStopGeneration };
}
