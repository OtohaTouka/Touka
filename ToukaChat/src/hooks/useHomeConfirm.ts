import { useCallback, useState } from 'react';
import { useChat } from '@/lib/providers/ChatProvider';

export interface HomeRequest {
  id: string;
  /** true 是設為家，false 是取消家。 */
  setting: boolean;
}

/** 設家、取消家都要先確認：換家會讓原本的家變成唯讀的舊家（ADR-0306）。 */
export function useHomeConfirm() {
  const { handleSetHome, handleUnsetHome } = useChat();
  const [request, setRequest] = useState<HomeRequest | null>(null);

  const askSetHome = useCallback((id: string) => setRequest({ id, setting: true }), []);
  const askUnsetHome = useCallback((id: string) => setRequest({ id, setting: false }), []);
  const cancel = useCallback(() => setRequest(null), []);
  const confirm = useCallback(() => {
    if (!request) return;
    setRequest(null);
    void (request.setting ? handleSetHome(request.id) : handleUnsetHome(request.id));
  }, [request, handleSetHome, handleUnsetHome]);

  return { request, askSetHome, askUnsetHome, cancel, confirm };
}
