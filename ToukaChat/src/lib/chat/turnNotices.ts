/**
 * ADR-0322 PR 2b：畫面上的小提示。工具計時、攔截、工具輪額度、中斷、拒答與各種警告這類系統說明，
 * 由後端以 turn_notice 事件送來（以前混在回覆文字裡）；存在助理節點的 notices（只存種類與參數），
 * 照種類用 i18n 顯示，不送給模型。
 *
 * 常數與後端 ToukaProxy/src/utils/turn_notices.py 同名常數一字不差（pytest 讀這個檔比對，
 * 只在這裡用的常數不匯出）；
 * 舊回覆裡的日誌行組歷史時拿掉，黃金案例前後端共用（__tests__/legacyLogLines.golden.json）。
 */

export const TURN_NOTICE_EVENT = 'turn_notice';
export const MAX_NOTICES_PER_NODE = 50;
export const MAX_NOTICE_CHARS = 300;
const LEGACY_LOG_MAX_MIDDLE = 300;

const TURN_NOTICE_KINDS = [
  'tool_finished',
  'tool_failed',
  'tool_exception',
  'tool_blocked',
  'hook_denied',
  'approval_blocked',
  'rounds_exhausted',
  'cost_exhausted',
  'interrupted',
  'refusal',
  'safety_filtered',
  'tool_hallucination',
  'record_imitation',
  'memory_hallucination',
  'honesty_warning'
] as const;

export type TurnNoticeKind = (typeof TURN_NOTICE_KINDS)[number];

export interface TurnNotice {
  kind: TurnNoticeKind;
  params: Record<string, string | number>;
}

/**
 * 舊對話裡的日誌行：[開頭, 結尾]。從開頭比對到它產生時自帶的結尾為止，不要求在行首；
 * 中間不能換行、不能有結尾的第一個字、不能超過 LEGACY_LOG_MAX_MIDDLE；結尾空字串表示開頭就是整段。
 * 記的是舊對話裡實際有的格式，不跟著目前的提示文字改。
 */
const LEGACY_LOG_LINES: ReadonlyArray<readonly [string, string]> = [
  ['🚫 攔截未授權調用: ', '\n'],
  ['🚫 hook 拒絕: ', '\n'],
  ['🖐️ 待審批攔截（fail-closed）: ', '\n'],
  ['✅ `時間記錄：', '`\n'],
  ['❌ `工具執行失敗`', '\n'],
  ['❌ 執行發生例外：', '\n'],
  ['⏹️ 已由使用者中斷。', ''],
  ['⚠️ 已達總量上限（累計成本），進行最後總結。', '\n'],
  ['⚠️ 工具輪額度已盡（', '），進行最後總結。\n'],
  ['⚠️ 模型拒絕回答。', ''],
  ['⚠️ 模型拒絕回答：', ''],
  ['⚠️ 回應被廠商的安全過濾擋下。', ''],
  ['⚠️ **[工具幻覺警告]** ', '。'],
  ['⚠️ **[記憶幻覺警告]** ', '。'],
  ['⚠️ **[誠實度警報]** ', '不要憑空編造。']
];

/** 截到 MAX_NOTICE_CHARS 個字（和後端一樣以 code point 算）。 */
function capped(text: string): string {
  const chars = Array.from(text);
  return chars.length <= MAX_NOTICE_CHARS ? text : chars.slice(0, MAX_NOTICE_CHARS).join('');
}

function cleanParams(params: Record<string, unknown>): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  for (const [key, value] of Object.entries(params)) {
    if (typeof value === 'string') out[capped(key)] = capped(value);
    else if (typeof value === 'number' && Number.isInteger(value)) out[capped(key)] = value;
  }
  return out;
}

function isKind(value: unknown): value is TurnNoticeKind {
  return typeof value === 'string' && (TURN_NOTICE_KINDS as readonly string[]).includes(value);
}

/** 串流事件 → 存進節點的小提示；種類不認得或參數不是物件就不存。 */
export function parseTurnNoticeEvent(event: Record<string, unknown>): TurnNotice | null {
  const { params } = event;
  if (event.type !== TURN_NOTICE_EVENT || !isKind(event.kind) ||
      typeof params !== 'object' || params === null || Array.isArray(params)) return null;
  return { kind: event.kind, params: cleanParams(params as Record<string, unknown>) };
}

function stripOne(text: string, start: string, end: string): string {
  let out = '';
  let pos = 0;
  for (;;) {
    const found = text.indexOf(start, pos);
    if (found < 0) return out + text.slice(pos);
    const after = found + start.length;
    let stop = after;
    if (end) {
      const closing = text.indexOf(end, after);
      const middle = closing >= 0 ? text.slice(after, closing) : '';
      if (closing < 0 || Array.from(middle).length > LEGACY_LOG_MAX_MIDDLE ||
          middle.includes('\n') || middle.includes(end[0])) {
        out += text.slice(pos, after);
        pos = after;
        continue;
      }
      stop = closing + end.length;
    }
    out += text.slice(pos, found);
    pos = stop;
  }
}

/** 拿掉舊日誌行；有拿掉才去掉前後多出來的換行，沒有就原樣返回。 */
export function stripLegacyLogLines(text: string): string {
  let stripped = text;
  for (const [start, end] of LEGACY_LOG_LINES) stripped = stripOne(stripped, start, end);
  return stripped === text ? text : stripped.replace(/^\n+|\n+$/g, '');
}
