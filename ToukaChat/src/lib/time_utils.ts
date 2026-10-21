/**
 * Touka Time SSOT Utility
 * 所有前端時間呈現必須通過此工具，以確保時區與格式的一致性。
 */

// 全域「12/24 小時制」偏好（外觀打磨包）：由 ThemeContext 依使用者設定同步呼叫 setTimeFormat12h。
// 呼叫端未顯式指定 hour12 時，formatToTaipeiTime 採用此偏好；預設 true（12 小時制，與原行為一致）。
let _prefHour12 = true;

/**
 * 設定全域時間制偏好（true=12 小時制／false=24 小時制）。
 * 集中於本 SSOT，使 26 處呼叫端無需各自傳參即可一致套用。
 */
export function setTimeFormat12h(use12h: boolean): void {
  _prefHour12 = use12h;
}

/**
 * 將日期轉換為台北時間 (Asia/Taipei, UTC+8)
 * 格式範例: 2026/5/6 下午 12:00:00（12 制）或 2026/5/6 12:00:00（24 制）
 */
export function formatToTaipeiTime(
  date: Date | number | string,
  options: Intl.DateTimeFormatOptions = {
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
    hour: 'numeric',
    minute: 'numeric',
    second: 'numeric',
  }
): string {
  const d = date instanceof Date ? date : new Date(date);

  // 如果日期無效，回傳空字串或原始輸入
  if (isNaN(d.getTime())) return String(date);

  // 呼叫端顯式指定優先；否則採用全域偏好。僅日期欄位（無 hour）時 hour12 無影響。
  const hour12 = options.hour12 ?? _prefHour12;

  return new Intl.DateTimeFormat('zh-TW', {
    ...options,
    hour12,
    timeZone: 'Asia/Taipei'
  }).format(d);
}

/**
 * 取得僅包含日期的台北時間格式
 * 格式範例: 2026/5/6
 */
export function formatToTaipeiDate(date: Date | number | string): string {
  return formatToTaipeiTime(date, {
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
  });
}

/**
 * 獲取系統當前時間
 * 這是全前端唯一允許呼叫 new Date() 獲取「現在」的地方
 */
export function getSystemNow(): Date {
  return new Date();
}

/**
 * 獲取當前台北時間的 Epoch (ms)
 */
export function getSystemEpoch(): number {
  return getSystemNow().getTime();
}

/** 小於此值的數字時間戳視為秒（與後端 legacy_time.SECONDS_CUTOFF 同一規則）。 */
const SECONDS_CUTOFF = 100_000_000_000;

/**
 * 訊息時間戳統一成毫秒：前端蓋的是毫秒、匯入的 Open WebUI 檔是秒；
 * 非正數、非數字或無法判讀回 undefined。
 */
export function toEpochMs(value: unknown): number | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return undefined;
  return Math.round(value < SECONDS_CUTOFF ? value * 1000 : value);
}
