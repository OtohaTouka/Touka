"use client";

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { useActivityPoll } from '@/hooks/useActivityPoll';
import { useSessionDiscovery } from '@/hooks/useSessionDiscovery';
import { useUnseenSessions } from '@/hooks/useUnseenSessions';
import { isIDE } from '@/lib/bridge';
import { logSSOTError } from '@/lib/error_handler';
import { isBusyStatus, MAX_ACTIVITY_FAILURES, mostUrgentStatus, type SessionWorkStatus } from '@/lib/session_activity';
import { getSystemEpoch } from '@/lib/time_utils';
import type { SessionActivity } from '@/lib/types';
import { useChat } from './ChatProvider';
import { useSessionList } from './SessionListProvider';
import { useStreamRegistry } from './StreamRegistryProvider';

interface SessionActivityValue {
  statusOf: (sessionId: string) => SessionWorkStatus | null;
  /** 看到這段對話在工作的最後時刻（排序用）；只放記憶體，不動對話內容。 */
  activityTimeOf: (sessionId: string) => number | undefined;
  /** 整份清單最要緊的狀態：側欄收起來時按鈕上亮的就是它。 */
  attention: SessionWorkStatus | null;
  /** 開著的這段對話被別處寫入過，畫面上的內容是舊的。 */
  staleSessionId: string | null;
  clearStale: () => void;
}

const SessionActivityContext = createContext<SessionActivityValue>({
  statusOf: () => null,
  activityTimeOf: () => undefined,
  attention: null,
  staleSessionId: null,
  clearStale: () => {},
});

const NO_BACKGROUND: SessionActivity['background'] = [];
const NO_STREAMS: string[] = [];

interface SessionActivityProviderProps {
  /** 畫面正在顯示聊天；在設定等畫面時，目前對話也算沒在看。 */
  chatVisible: boolean;
  children: React.ReactNode;
}

/** 工作狀態的唯一來源（ADR-0300）：本分頁的串流以本地登記為準，心跳與別處的串流靠查詢。 */
export function SessionActivityProvider(props: SessionActivityProviderProps) {
  const { user } = useAuth();
  // useLocalStorage 換鍵不重讀：換帳號就重掛，才不會留著上一個帳號的小點
  return <SessionActivityBody key={user?.id ?? 'anonymous'} userId={user?.id} {...props} />;
}

function SessionActivityBody({ userId, chatVisible, children }: SessionActivityProviderProps & { userId?: string }) {
  const { sessions, listLoaded, discoverSession } = useSessionList();
  const { streamingSessionIds, awaitingApprovalSessionIds } = useStreamRegistry();
  const { activeSessionId, pendingRequestSessionIds } = useChat();
  const { unseenIds, markUnseen, clearUnseen, pruneUnseen } = useUnseenSessions(userId);

  const [remoteStreams, setRemoteStreams] = useState(NO_STREAMS);
  const [background, setBackground] = useState(NO_BACKGROUND);
  const [activityTimes, setActivityTimes] = useState<Record<string, number>>({});
  const [staleSessionId, setStaleSessionId] = useState<string | null>(null);

  // 查詢與串流結束的事件都不在 render 裡，讀最新值用這份
  const latest = useRef({ activeSessionId, chatVisible, sessions, streamingSessionIds, background, remoteStreams });
  useEffect(() => {
    latest.current = { activeSessionId, chatVisible, sessions, streamingSessionIds, background, remoteStreams };
  });

  const discovery = useSessionDiscovery({ discover: discoverSession, onDropped: clearUnseen });

  const workEnded = useCallback((id: string, elsewhere: boolean) => {
    const { activeSessionId: active, chatVisible: viewing, sessions: known } = latest.current;
    if (elsewhere && id === active) setStaleSessionId(id);
    if (id === active && viewing) return;
    markUnseen(id, !known.find(session => session.id === id)?.incognito);
  }, [markUnseen]);

  const failures = useRef(0);
  const endedAtPoll = useRef(new Map<string, number>());

  const workingNow = useCallback((activity?: SessionActivity) => new Set([
    ...(activity?.streams ?? latest.current.remoteStreams),
    ...(activity?.background ?? latest.current.background).map(row => row.session_id),
  ]), []);

  const onResult = useCallback((activity: SessionActivity, sequence: number) => {
    failures.current = 0;
    const { streamingSessionIds: local, sessions: known, remoteStreams: previous } = latest.current;
    // 本分頁有紀錄的以本地為準；本地已結束的，要結束之後才發出的查詢才算數，否則呼吸燈會多亮一下、留假小點
    const streams = activity.streams.filter(id => !local.includes(id) && sequence > (endedAtPoll.current.get(id) ?? 0));
    const vanished = previous.filter(id => !streams.includes(id) && !local.includes(id));
    latest.current = { ...latest.current, remoteStreams: streams, background: activity.background };
    setRemoteStreams(streams);
    setBackground(activity.background);

    const now = getSystemEpoch();
    const times: Record<string, number> = {};
    for (const id of workingNow({ ...activity, streams })) times[id] = now;
    for (const row of activity.finished) times[row.session_id] = Math.max(times[row.session_id] ?? 0, row.finished_at);
    if (Object.keys(times).length) setActivityTimes(current => ({ ...current, ...times }));

    for (const id of Object.keys(times)) {
      if (!known.some(session => session.id === id)) discovery.enqueue(id);
    }
    for (const id of vanished) workEnded(id, true);
    for (const row of activity.finished) workEnded(row.session_id, true);
    if (listLoaded) void discovery.flush(workingNow({ ...activity, streams }));
  }, [discovery, listLoaded, workEnded, workingNow]);

  const onFailure = useCallback(() => {
    failures.current += 1;
    if (failures.current !== MAX_ACTIVITY_FAILURES) return;
    // 連續查不到：先不亮心跳，也不當成結束（不留小點）；本地串流的狀態不受影響
    latest.current = { ...latest.current, remoteStreams: NO_STREAMS, background: NO_BACKGROUND };
    setRemoteStreams(NO_STREAMS);
    setBackground(NO_BACKGROUND);
    logSSOTError(new Error('Session activity query failed'), 'SessionActivityProvider:poll');
  }, []);

  const pollSequence = useActivityPoll(!isIDE(), { onResult, onFailure });

  // 清單讀到之前排進來的，讀到就下載
  useEffect(() => {
    if (listLoaded) void discovery.flush(workingNow());
  }, [listLoaded, discovery, workingNow]);

  // 本分頁的串流結束：登記表結束就刪，呼吸燈馬上停
  const previousLocal = useRef(streamingSessionIds);
  useEffect(() => {
    const ended = previousLocal.current.filter(id => !streamingSessionIds.includes(id));
    previousLocal.current = streamingSessionIds;
    for (const id of ended) {
      endedAtPoll.current.set(id, pollSequence.current);
      workEnded(id, false);
    }
  }, [streamingSessionIds, pollSequence, workEnded]);

  // 在看的對話沒有小點；從設定回到聊天時目前對話也清掉
  useEffect(() => {
    if (chatVisible && activeSessionId) clearUnseen(activeSessionId);
  }, [chatVisible, activeSessionId, clearUnseen, unseenIds]);

  // 對話被刪、或存著的編號已經不在清單上了就不留
  useEffect(() => {
    if (!listLoaded) return;
    pruneUnseen(id => sessions.some(session => session.id === id) || discovery.isQueued(id));
  }, [listLoaded, sessions, discovery, pruneUnseen]);

  // 換對話或內容更新之後，提示就過時了
  const activeRevision = sessions.find(session => session.id === activeSessionId)?.revision;
  const staleContext = `${activeSessionId}\n${activeRevision ?? ''}`;
  const [seenStaleContext, setSeenStaleContext] = useState(staleContext);
  if (seenStaleContext !== staleContext) {
    setSeenStaleContext(staleContext);
    setStaleSessionId(null);
  }

  const clearStale = useCallback(() => setStaleSessionId(null), []);

  const value = useMemo<SessionActivityValue>(() => {
    const awaiting = new Set([...awaitingApprovalSessionIds, ...pendingRequestSessionIds]);
    const replying = new Set([...streamingSessionIds, ...remoteStreams]);
    const heartbeats = new Map(background.map(row => [row.session_id, row]));

    const statusOf = (id: string): SessionWorkStatus | null => {
      const heartbeat = heartbeats.get(id);
      return mostUrgentStatus([
        awaiting.has(id) ? { kind: 'awaiting' } : null,
        replying.has(id) ? { kind: 'replying' } : null,
        heartbeat ? { kind: 'heartbeat', current: heartbeat.current, total: heartbeat.total } : null,
        unseenIds.has(id) ? { kind: 'unseen' } : null,
      ]);
    };
    return {
      statusOf,
      activityTimeOf: id => activityTimes[id],
      attention: mostUrgentStatus(sessions.map(session => statusOf(session.id))),
      staleSessionId: staleSessionId !== null && !isBusyStatus(statusOf(staleSessionId)) ? staleSessionId : null,
      clearStale,
    };
  }, [awaitingApprovalSessionIds, pendingRequestSessionIds, streamingSessionIds, remoteStreams, background,
    unseenIds, activityTimes, sessions, staleSessionId, clearStale]);

  return <SessionActivityContext.Provider value={value}>{children}</SessionActivityContext.Provider>;
}

export function useSessionActivity() {
  return useContext(SessionActivityContext);
}
