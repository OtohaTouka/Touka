import type { MemoryNotice } from "./SystemStatusContext";

/**
 * 提示（記憶提示或系統提示）裡有沒有警告級（唯一判斷處）：左側身分列據此顯示警告標記，
 * 讓右側欄收起時仍看得到；只有資訊級時不標記。
 */
export function hasMemoryWarning(notices: readonly MemoryNotice[] | undefined): boolean {
    return (notices ?? []).some(notice => notice.level === "warning");
}
