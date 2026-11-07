import { useEffect, useState } from 'react';
import type { ReviewerModelOption } from '@/lib/types';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError } from '@/lib/error_handler';

const isOption = (value: unknown): value is ReviewerModelOption => {
  const item = value as Record<string, unknown> | null;
  return !!item && typeof item.id === 'string' && typeof item.name === 'string' && typeof item.provider === 'string';
};

/**
 * 已設定金鑰、啟用中的一般模型（後端 `/v1/autonomy/reviewer-models`，ADR-0246）。
 * Auto 審查員與摘要模型（ADR-0305）的選單共用這一份；讀不到或格式不對就回 loadFailed。
 */
export function useKeyedModels() {
  const [models, setModels] = useState<ReviewerModelOption[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetchTouka(getActiveProxyUrl(), '/v1/autonomy/reviewer-models');
        const body: unknown = res.ok ? await res.json() : null;
        const list = (body as { models?: unknown } | null)?.models;
        if (!alive) return;
        if (Array.isArray(list) && list.every(isOption)) setModels(list);
        else setLoadFailed(true);
      } catch (err) {
        logSSOTError(err, 'useKeyedModels:fetch');
        if (alive) setLoadFailed(true);
      }
    };
    void load();
    return () => { alive = false; };
  }, []);

  return { models, loadFailed };
}
