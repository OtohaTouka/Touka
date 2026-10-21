/**
 * 匯入對話摘要（純函式，SRP/SSOT）。
 *
 * 比對「即將匯入的 session id」與「現有 session id」，算出總數、全新數、覆蓋數，
 * 供匯入前的確認提示使用（覆蓋＝同 id 將被取代，屬不可復原操作）。
 * 不含任何 I/O 或 React 依賴，便於單元測試。
 */
export interface ImportSummary {
    /** 即將匯入的對話總數。 */
    total: number;
    /** 不與現有對話衝突、屬全新加入的數量。 */
    fresh: number;
    /** 與現有對話 id 相同、將被覆蓋取代的數量。 */
    overwrite: number;
}

export function summarizeImport(incomingIds: string[], existingIds: string[]): ImportSummary {
    const existing = new Set(existingIds);
    let overwrite = 0;
    for (const id of incomingIds) {
        if (existing.has(id)) overwrite += 1;
    }
    return {
        total: incomingIds.length,
        fresh: incomingIds.length - overwrite,
        overwrite,
    };
}
