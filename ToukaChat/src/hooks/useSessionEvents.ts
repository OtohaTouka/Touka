import { useState, useEffect } from 'react';
import { useTranslation } from "react-i18next";
import { toast } from 'sonner';
import { Session, SaveResult } from '../lib/types';
import { migrateFlatToTree } from '../lib/tree_history';
import { getSystemEpoch } from '../lib/time_utils';
import { summarizeImport } from '../lib/import_utils';
import { useConfirmToast } from './useConfirmToast';
import { isSaved, unknownSave } from '../lib/session_persistence';
import { captureRequestScope, isRequestScopeCurrent } from '../lib/connections';

export function useSessionEvents(
  sessions: Session[],
  setSessions: React.Dispatch<React.SetStateAction<Session[]>>,
  syncSession: (session: Session) => Promise<SaveResult>
) {
  const { t } = useTranslation();
  const { confirmToast } = useConfirmToast();
  const [importPending, setImportPending] = useState<{
    sessions: { id: string; title: string; data: Session }[];
    onConfirm: (agentId: string) => void;
    onCancel: () => void;
  } | null>(null);

  useEffect(() => {
    const handleImport = async (e: Event) => {
      const scope = captureRequestScope();
      let data = (e as CustomEvent).detail;
      if (data.history && Array.isArray(data.history)) data = data.history;
      if (!Array.isArray(data)) {
        if (data && (data.id || data.chat)) data = [data];
        else return;
      }

      interface RawImportSession {
        id?: string;
        title?: string;
        agent_id?: string;
        messages?: unknown[];
        chat?: {
          id?: string;
          history?: unknown[];
          models?: string[];
        };
      }

      const processed: Session[] = data.map((item: unknown) => {
        const i = item as RawImportSession;
        const sessId = i.id || (i.chat?.id) || `import-${getSystemEpoch()}`;
        const rawMessages = i.chat?.history || i.messages || [];
        const processedHistory = migrateFlatToTree(rawMessages);
        const detectedAgentId = i.agent_id || i.chat?.models?.[0];
        return {
          id: sessId, title: i.title || t('hooks.use_session_events.imported_conversation_title'),
          messages: { messages: processedHistory.messages, currentId: processedHistory.currentId },
          agent_id: detectedAgentId, updatedAt: getSystemEpoch()
        } as Session;
      });

      // 匯入前確認 + 覆蓋警告：同 id 對話將被取代（不可復原），先讓使用者核對則數再決定。
      const summary = summarizeImport(processed.map(s => s.id), sessions.map(s => s.id));
      if (summary.total === 0) return;
      const confirmed = await new Promise<boolean>(resolve => {
        const msg = summary.overwrite > 0
          ? t('settings.general_tab.import_confirm_overwrite', { total: summary.total, overwrite: summary.overwrite })
          : t('settings.general_tab.import_confirm', { total: summary.total });
        confirmToast(msg, () => resolve(true), undefined, () => resolve(false));
      });
      if (!confirmed || !isRequestScopeCurrent(scope)) return;

      const missing = processed.filter(s => !s.agent_id);
      const finalAgentIdForMissing: string | null = await (() => {
        if (missing.length === 0) return Promise.resolve(null);
        return new Promise<string>(resolve => setImportPending({
          sessions: missing.map(s => ({ id: s.id, title: s.title, data: s })),
          onConfirm: (id) => resolve(id),
          onCancel: () => { setImportPending(null); resolve(""); }
        }));
      })();

      setImportPending(null);
      if (!isRequestScopeCurrent(scope)) return;
      const finalList = processed.map(s => ({ ...s, agent_id: s.agent_id ?? (finalAgentIdForMissing || undefined) }));

      setSessions(prev => {
        const next = [...prev];
        finalList.forEach(sess => {
          const idx = next.findIndex(s => s.id === sess.id);
          if (idx !== -1) next[idx] = sess; else next.push(sess);
        });
        return next.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
      });
      const results = await Promise.all(finalList.map(async session => {
        try { return await syncSession(session); } catch { return unknownSave(); }
      }));
      if (!isRequestScopeCurrent(scope)) return;
      const saved = results.filter(isSaved).length;
      if (saved === results.length) toast.success(t('settings.general_tab.toast_import_success'));
      else toast.warning(t('settings.general_tab.toast_import_partial', {
        saved, failed: results.filter(result => result.status === 'failed').length,
        unknown: results.filter(result => result.status === 'unknown').length,
      }));
    };

    const handleExport = () => {
      // HAX G17（ADR-0086）：無痕對話永不進匯出。
      const sanitizedSessions = sessions.filter(session => !session.incognito).map(session => {
        // Deep clone to avoid mutating active state
        const s = JSON.parse(JSON.stringify(session));
        
        // 1. 過濾潛在敏感欄位
        delete s.auth_level;
        
        // 2. 過濾 messages 中的大體積 base64 附件
        if (s.messages) {
          if (s.messages.messages && typeof s.messages.messages === 'object') {
            // 樹狀結構
            Object.keys(s.messages.messages).forEach(id => {
              const msg = s.messages.messages[id];
              if (msg && msg.attachment && msg.attachment.url && msg.attachment.url.startsWith('data:')) {
                msg.attachment.url = "(image data truncated)";
              }
            });
          } else if (Array.isArray(s.messages)) {
            // 扁平陣列結構
            s.messages.forEach((msg: { attachment?: { url?: string } }) => {
              if (msg && msg.attachment && msg.attachment.url && msg.attachment.url.startsWith('data:')) {
                msg.attachment.url = "(image data truncated)";
              }
            });
          }
        }
        return s;
      });

      const dataStr = JSON.stringify(sanitizedSessions, null, 2);
      const blob = new Blob([dataStr], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url; link.download = `touka-export-${getSystemEpoch()}.json`;
      document.body.appendChild(link); link.click(); document.body.removeChild(link);
    };

    window.addEventListener("touka_import_chats", handleImport as EventListener);
    window.addEventListener("touka_request_export", handleExport as EventListener);
    return () => {
      window.removeEventListener("touka_import_chats", handleImport as EventListener);
      window.removeEventListener("touka_request_export", handleExport as EventListener);
    };
  }, [sessions, setSessions, syncSession, confirmToast, t]);

  return { importPending };
}
