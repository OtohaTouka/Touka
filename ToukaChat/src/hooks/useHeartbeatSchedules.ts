import { useState, useRef, useCallback, useEffect } from 'react';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { errorMsg } from '@/lib/error_handler';
import { ERRORS, type ErrorCode } from '@/lib/error_codes';
import type { HeartbeatSchedule } from '@/lib/types';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';

// Persisted schedules refine the generated optional creation id.
export type Schedule = HeartbeatSchedule & { id: string };
type MutationState = 'idle' | 'saved' | 'deleted' | 'saved_refresh_failed' | 'deleted_refresh_failed' | 'failed';
const PATH = '/v1/heartbeat/schedules';

async function safeResponseMessage(response: Response, fallback: ErrorCode): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (!body || typeof body !== 'object') return errorMsg(fallback);
    const envelope = body as Record<string, unknown>;
    const detail = envelope.detail && typeof envelope.detail === 'object'
      ? envelope.detail as Record<string, unknown> : envelope;
    const match = (Object.keys(ERRORS) as ErrorCode[]).find(code =>
      (detail.code === code && (detail.id === undefined || detail.id === ERRORS[code].id)) ||
      (detail.code === undefined && detail.id === ERRORS[code].id));
    return errorMsg(match ?? fallback);
  } catch {
    return errorMsg(fallback);
  }
}

export function useHeartbeatSchedules() {
  const { t } = useTranslation();
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [mutationState, setMutationState] = useState<MutationState>('idle');
  const revision = useRef<string | null>(null);
  const loadingGeneration = useRef(0);
  const mutating = useRef(false);

  const loadSchedules = useCallback(async (): Promise<boolean> => {
    const generation = ++loadingGeneration.current;
    revision.current = null;
    setIsLoading(true);
    let failureMessage = errorMsg('HEARTBEAT_UPSTREAM_FAILED');
    try {
      const res = await fetchTouka(getActiveProxyUrl(), PATH);
      if (!res.ok) {
        failureMessage = await safeResponseMessage(res, 'HEARTBEAT_UPSTREAM_FAILED');
        throw new Error();
      }
      const data: unknown = await res.json();
      const nextRevision = res.headers.get('ETag');
      if (!Array.isArray(data) || !nextRevision || data.some(item =>
        !item || typeof item.id !== 'string' || !item.id || typeof item.agent_id !== 'string')) {
        throw new Error(errorMsg('HEARTBEAT_UPSTREAM_FAILED'));
      }
      if (generation !== loadingGeneration.current) return false;
      setSchedules(data as Schedule[]);
      revision.current = nextRevision;
      setLoadError(null);
      return true;
    } catch {
      if (generation === loadingGeneration.current) {
        setLoadError(failureMessage);
      }
      return false;
    } finally {
      if (generation === loadingGeneration.current) setIsLoading(false);
    }
  }, []);

  const mutate = async (method: 'POST' | 'DELETE', id: string | null, payload?: Partial<HeartbeatSchedule>) => {
    if (mutating.current) return false;
    if (!revision.current) {
      const message = errorMsg('HEARTBEAT_REVISION_REQUIRED');
      setMutationError(message);
      toast.error(message);
      return false;
    }
    mutating.current = true;
    setIsSaving(true);
    setMutationError(null);
    let failureMessage = errorMsg('HEARTBEAT_OUTCOME_UNKNOWN');
    try {
      const escaped = encodeURIComponent(id ?? '').replace(/\./g, '%2E');
      const res = await fetchTouka(getActiveProxyUrl(), method === 'POST' ? PATH : `${PATH}/${escaped}`, {
        method,
        headers: { 'Content-Type': 'application/json', 'If-Match': revision.current },
        ...(method === 'POST' ? { body: JSON.stringify({ ...payload, ...(id ? { id } : {}) }) } : {}),
      });
      if (!res.ok) {
        failureMessage = await safeResponseMessage(res, 'HEARTBEAT_OUTCOME_UNKNOWN');
        throw new Error();
      }
      const refreshed = await loadSchedules();
      const state = method === 'POST' ? 'saved' : 'deleted';
      setMutationState(refreshed ? state : `${state}_refresh_failed`);
      if (!refreshed) {
        toast.warning(t('hooks.use_heartbeat_schedules.saved_refresh_failed', {
          defaultValue: '變更已完成，但清單刷新失敗；請重新載入，勿重複送出。',
        }));
      } else {
        const key = method === 'DELETE' ? 'schedule_deleted' : id ? 'schedule_updated' : 'schedule_created';
        toast.success(t(`hooks.use_heartbeat_schedules.${key}`));
      }
      return true;
    } catch {
      revision.current = null;
      setMutationState('failed');
      setMutationError(failureMessage);
      toast.error(failureMessage);
      return false;
    } finally {
      mutating.current = false;
      setIsSaving(false);
    }
  };

  const saveSchedule = (id: string | null, payload: Partial<HeartbeatSchedule>) => mutate('POST', id, payload);
  const deleteSchedule = (id: string) => mutate('DELETE', id);
  useEffect(() => { void loadSchedules(); }, [loadSchedules]);
  return { schedules, isLoading, isSaving, loadError, mutationError, mutationState, loadSchedules, saveSchedule, deleteSchedule };
}
