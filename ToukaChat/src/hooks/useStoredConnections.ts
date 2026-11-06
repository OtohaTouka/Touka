import { useLocalStorage } from '@/hooks/useLocalStorage';
import { CONNECTIONS_STORAGE_KEY, maskConnectionKeys } from '@/lib/connections';
import type { Connection } from '@/lib/types';

/**
 * 瀏覽器裡的連線清單（LOCALSTORAGE_SSOT）。讀進來與寫回去都只有遮罩，不留第三方金鑰；
 * 這次輸入的真值只在記憶體裡，送後端時才用。
 */
export function useStoredConnections() {
    return useLocalStorage<Connection[]>(CONNECTIONS_STORAGE_KEY, [], maskConnectionKeys);
}
