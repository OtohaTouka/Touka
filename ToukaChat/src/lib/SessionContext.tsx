"use client";

import React from 'react';
import { ProjectProvider } from './providers/ProjectProvider';
import { ModelProvider, useModels } from './providers/ModelProvider';
import { SessionListProvider, useSessionList } from './providers/SessionListProvider';
import { StreamRegistryProvider } from './providers/StreamRegistryProvider';
import { ChatProvider, useChat } from './providers/ChatProvider';
import { AgentProvider } from './providers/AgentProvider';
import { DebugProvider } from './providers/DebugProvider';
import ImportAgentModal from '@/components/modals/ImportAgentModal';
import { AskUserModal } from '@/components/modals/AskUserModal';
import { useSessionEvents } from '@/hooks/useSessionEvents';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';

const McpInstallModal = React.lazy(() => import('@/components/modals/EvolutionModal'));

function GlobalSessionEvents({ children }: { children: React.ReactNode }) {
  const { availableModels } = useModels();
  const { sessions, setSessions, syncSession } = useSessionList();
  const chat = useChat();

  const { importPending } = useSessionEvents(sessions, setSessions, syncSession);
  // 彈窗各自記著自己是哪一筆：安裝在 await 之後才關，那時排第一的可能已經換人
  const install = chat.pendingInstall;
  const ask = chat.pendingAsk;

  return (
    <>
      {children}
      {importPending && (
        <ImportAgentModal 
          pendingSessions={importPending.sessions}
          availableModels={availableModels}
          onConfirm={(agentId) => importPending.onConfirm(agentId)}
          onCancel={() => importPending.onCancel()}
        />
      )}
      
      {install && (
        <React.Suspense fallback={null}>
          {/* key：排隊跳下一筆時彈窗內部的狀態（填到一半的欄位）不帶過去 */}
          <McpInstallModal
            key={install.id}
            installRequest={install}
            onClose={() => chat.dismissPendingInstall(install.id)}
          />
        </React.Suspense>
      )}

      {/* HAX G10（ADR-0085）：AI 詢問使用者 → 問卷彈窗；作答後 POST 回 ask_user 答案端點解除等待。 */}
      {ask && (
        <AskUserModal
          key={ask.id}
          request={ask}
          onCancel={() => chat.dismissPendingAsk(ask.id)}
          onSubmit={async (answers) => {
            chat.dismissPendingAsk(ask.id);
            try {
              await fetchTouka(getActiveProxyUrl(), `/v1/ask_user/answer/${ask.id}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ answers }),
              });
            } catch (e) {
              logSSOTError(e, 'SessionContext:askUserAnswer');
            }
          }}
        />
      )}
    </>
  );
}

export * from './providers/ProjectProvider';
export * from './providers/ModelProvider';
export * from './providers/SessionListProvider';
export * from './providers/ChatProvider';

export function SessionProvider({ children }: { children: React.ReactNode }) {
  return (
    <ProjectProvider>
        <AgentProvider>
          <ModelProvider>
            <SessionListProvider>
              <DebugProvider>
              <StreamRegistryProvider>
                <ChatProvider>
                  <GlobalSessionEvents>
                    {children}
                  </GlobalSessionEvents>
                </ChatProvider>
              </StreamRegistryProvider>
              </DebugProvider>
            </SessionListProvider>
          </ModelProvider>
        </AgentProvider>
    </ProjectProvider>
  );
}
