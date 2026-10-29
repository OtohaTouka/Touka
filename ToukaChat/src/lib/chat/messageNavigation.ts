/**
 * 對話左側的訊息導覽列：哪些訊息畫線、預覽文字、分段、目前那則（純函式）。
 *
 * 一條線對應線性路徑上的一則使用者訊息；心跳補的「請繼續」也是 user 角色，但後端寫入時
 * name 為 SYSTEM_SPEAKER，這裡依它排除。index 一律是在 ChatArea 同一份 linearMessages 裡的
 * 位置，和 Virtuoso 的 data index 對得上。
 */
import type { MessageNode } from '@/lib/tree_history';

/** 雙端常數：後端 ToukaProxy/src/utils/user_intent.py 的 SYSTEM_SPEAKER（tests/unit/test_system_speaker_contract.py 核對）。 */
export const SYSTEM_SPEAKER = "System";

/** 至少幾條線才顯示導覽列（比的是線數，不是對話回合數）。 */
export const MIN_NAV_TICKS = 2;
/** 線多於這個數目時平均分段（導覽區夠高時的上限）。 */
const MAX_NAV_TICKS = 120;
/** 每條線大約佔的高度（px，含間隔；工具列內距沒扣，實際略小）；導覽區排不下時減少條數、改成分段。 */
const MIN_TICK_PITCH_PX = 8;
/** 預覽最多取幾個字（畫面上另由 line-clamp 限 3 行）。 */
const NAV_PREVIEW_MAX_CHARS = 200;

export type NavAttachment = { kind: 'image' } | { kind: 'file'; name: string | null };

export interface NavEntry {
  /** 在線性路徑裡的位置。 */
  index: number;
  id: string;
  preview: string;
  attachment: NavAttachment | null;
}

export interface NavTick {
  key: string;
  /** 點擊時跳到的位置（該段第一則）。 */
  index: number;
  /** 這條涵蓋的線性路徑範圍（含頭尾），用來判斷目前那則是否在段內。 */
  startIndex: number;
  endIndex: number;
  /** 第幾則到第幾則（從 1 算，沒分段時兩者相同）。 */
  firstOrdinal: number;
  lastOrdinal: number;
  entry: NavEntry;
}

export interface NavRange {
  startIndex: number;
  endIndex: number;
}

function previewOf(content: string | undefined): string {
  // 先截一段再處理，串流中每個 chunk 都會重算
  const head = (content ?? '').trimStart().slice(0, NAV_PREVIEW_MAX_CHARS + 1);
  return head.length > NAV_PREVIEW_MAX_CHARS
    ? `${head.slice(0, NAV_PREVIEW_MAX_CHARS).trimEnd()}…`
    : head.trimEnd();
}

function attachmentOf(node: MessageNode): NavAttachment | null {
  if (!node.attachment) return null;
  if (node.attachment.type === 'image') return { kind: 'image' };
  return { kind: 'file', name: node.attachment.name || null };
}

export function buildNavEntries(linear: MessageNode[]): NavEntry[] {
  const entries: NavEntry[] = [];
  linear.forEach((node, index) => {
    if (node.role !== 'user' || node.name === SYSTEM_SPEAKER) return;
    entries.push({ index, id: node.id, preview: previewOf(node.content), attachment: attachmentOf(node) });
  });
  return entries;
}

export function groupNavEntries(entries: NavEntry[], max: number = MAX_NAV_TICKS): NavTick[] {
  const n = entries.length;
  const buckets = Math.min(n, max);
  const ticks: NavTick[] = [];
  for (let k = 0; k < buckets; k++) {
    const first = Math.floor((k * n) / buckets);
    const last = Math.floor(((k + 1) * n) / buckets) - 1;
    const entry = entries[first];
    ticks.push({
      key: entry.id,
      index: entry.index,
      startIndex: entry.index,
      endIndex: entries[last].index,
      firstOrdinal: first + 1,
      lastOrdinal: last + 1,
      entry,
    });
  }
  return ticks;
}

/** 導覽區高度能排幾條線；還沒量到高度時用上限。 */
export function maxTicksFor(height: number | null): number {
  if (height === null || height <= 0) return MAX_NAV_TICKS;
  const fit = Math.floor(height / MIN_TICK_PITCH_PX);
  return Math.min(MAX_NAV_TICKS, Math.max(MIN_NAV_TICKS, fit));
}

/** 目前所在的那一則：範圍內最上面的使用者訊息；範圍內沒有時取範圍之前最近的那則。 */
export function pickCurrentIndex(entries: NavEntry[], range: NavRange | null): number | null {
  if (!range) return null;
  let before: number | null = null;
  for (const entry of entries) {
    if (entry.index > range.endIndex) break;
    if (entry.index >= range.startIndex) return entry.index;
    before = entry.index;
  }
  return before;
}

export function isTickCurrent(tick: NavTick, currentIndex: number | null): boolean {
  return currentIndex !== null && tick.startIndex <= currentIndex && currentIndex <= tick.endIndex;
}
