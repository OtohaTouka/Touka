import { useState, useCallback, useEffect, useRef } from 'react';
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Session } from '@/lib/types';
import { useSessionList } from '@/lib/providers/SessionListProvider';
import { useChat } from '@/lib/providers/ChatProvider';
import { useModels } from '@/lib/providers/ModelProvider';
import { formatToOpenWebUI, triggerDownload, splitSessionMessages } from '@/lib/export-utils';
import { getActiveProxyUrl, fetchTouka, captureRequestScope, isRequestScopeCurrent } from '@/lib/connections';
import { migrateFlatToTree, MessageNode } from '@/lib/tree_history';
import { getSystemEpoch, formatToTaipeiDate } from '@/lib/time_utils';
import { logSSOTError, getErrorMessage, errorMsg } from '@/lib/error_handler';
import { isSaved } from '@/lib/session_persistence';

/**
 * Hook for session operations (SOLID: SRP, DIP)
 */
export function useSessionActions() {
  const { t } = useTranslation();
  const { deleteSession: handleDeleteSession, updateSessionTitle: handleUpdateSessionTitle, sessions } = useSessionList();
  const { handleUpdateSessionMessages } = useChat();
  const { selectedModel } = useModels();
  const requestScope = captureRequestScope();

  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const pendingDeletes = useRef(new Set<string>());
  const pendingTitles = useRef(new Set<string>());
  const deleteConfirmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cancelDeleteConfirmTimer = useCallback(() => {
    if (deleteConfirmTimer.current !== null) {
      clearTimeout(deleteConfirmTimer.current);
      deleteConfirmTimer.current = null;
    }
  }, []);
  useEffect(() => cancelDeleteConfirmTimer, [cancelDeleteConfirmTimer]);

  const handleDelete = useCallback(async (id: string) => {
    const session = sessions.find(s => s.id === id);
    if (session?.is_home) {
      toast.warning(t('hooks.use_session_actions.home_session_cannot_delete'));
      return;
    }
    if (pendingDeletes.current.has(id)) return;
    
    if (deleteConfirmId === id) {
      // ref 在第一個 await 前設好，包含同一次事件迴圈內的連按。
      pendingDeletes.current.add(id);
      cancelDeleteConfirmTimer();
      try {
        if (await handleDeleteSession(id)) {
          setDeleteConfirmId(current => current === id ? null : current);
        }
      } catch {
        toast.error(errorMsg("DB_DELETE_FAILED"), { id: "session-delete-failed" });
      } finally {
        pendingDeletes.current.delete(id);
      }
    } else {
      cancelDeleteConfirmTimer();
      setDeleteConfirmId(id);
      deleteConfirmTimer.current = setTimeout(() => {
        setDeleteConfirmId(current => current === id ? null : current);
        deleteConfirmTimer.current = null;
      }, 3000);
    }
  }, [sessions, deleteConfirmId, handleDeleteSession, cancelDeleteConfirmTimer, t]);

  const handleStartEdit = useCallback((session: Session) => {
    setEditingSessionId(session.id);
    setEditTitle(session.title);
  }, []);

  const handleSaveTitle = useCallback(async (id: string) => {
    if (pendingTitles.current.has(id)) return;
    pendingTitles.current.add(id);
    try {
      const result = await handleUpdateSessionTitle(id, editTitle);
      if (isSaved(result)) setEditingSessionId(current => current === id ? null : current);
    } catch {
      toast.error(errorMsg('SESSION_SAVE_FAILED'), { id: 'session-sync-failed' });
    } finally {
      pendingTitles.current.delete(id);
    }
  }, [editTitle, handleUpdateSessionTitle]);

  const handleExport = useCallback((session: Session) => {
    const openWebUIExport = [formatToOpenWebUI(session, selectedModel)];
    triggerDownload(openWebUIExport, `chat-export-${session.title}-${formatToTaipeiDate(getSystemEpoch())}.json`);
  }, [selectedModel]);

  const handleSplitExport = useCallback(async (session: Session) => {
    if (session.incognito) {
      toast.info(t('hooks.use_session_actions.incognito_split_export_only'));
      return;
    }
    const splitResult = splitSessionMessages(session, 30);
    if (!splitResult) {
      toast.info(t('hooks.use_session_actions.session_too_small_to_split'));
      return;
    }

    const { legacyMessages, keepMessages, legacyUserCount } = splitResult;
    const expectedBase = structuredClone(session);
    if (!requestScope || !isRequestScopeCurrent(requestScope)) return;
    const legacyExport = formatToOpenWebUI({ ...session, messages: legacyMessages }, selectedModel);
    const openWebUIExport = [legacyExport];

    const agentID = session.agent_id || session.home_for_agent || selectedModel || "ToukaGroup";
    const dateStr = formatToTaipeiDate(getSystemEpoch());
    const filename = `${agentID}_${dateStr}.json`;

    const proxyUrl = getActiveProxyUrl();
    try {
      const res = await fetchTouka(proxyUrl, '/v1/ui/sessions/legacy/export', {
        method: "POST",
        requestScope,
        body: JSON.stringify({
          agent_id: agentID,
          filename: filename,
          json_content: openWebUIExport
        })
      });
      if (!isRequestScopeCurrent(requestScope)) return;

      if (res.ok) {
        const newTree = migrateFlatToTree(keepMessages);
        const result = await handleUpdateSessionMessages(session.id, newTree as unknown as MessageNode[], expectedBase);
        if (!isRequestScopeCurrent(requestScope)) return;
        if (isSaved(result)) toast.success(t('hooks.use_session_actions.split_success', { legacyUserCount }));
        else toast.warning(t('hooks.use_session_actions.split_exported_save_unconfirmed'));
      } else {
        const err = await res.json();
        const errMsg = await getErrorMessage(err, "EXPORT_FAILED");
        if (!isRequestScopeCurrent(requestScope)) return;
        toast.error(errMsg);
      }
    } catch (err) {
      if (!isRequestScopeCurrent(requestScope)) return;
      logSSOTError(err, "useSessionActions:handleSplitExport");
      toast.error(errorMsg("EXPORT_FAILED"));
    }
  }, [selectedModel, handleUpdateSessionMessages, t, requestScope]);

  const clearDeleteConfirm = useCallback(() => {
    cancelDeleteConfirmTimer();
    setDeleteConfirmId(null);
  }, [cancelDeleteConfirmTimer]);

  return {
    deleteConfirmId,
    editingSessionId,
    editTitle,
    setEditTitle,
    handleDelete,
    handleStartEdit,
    handleSaveTitle,
    handleExport,
    handleSplitExport,
    clearDeleteConfirm
  };
}
