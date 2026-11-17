/**
 * 工具紀錄：做過的事帶進下一輪（ADR-0319）。
 *
 * 組下一輪的 API 訊息時，助理那則依存下來的工具步驟（ToolStep）組成一段系統整理的紀錄，加在內容前面
 * （插話標註之後、原本文字之前）；存檔與畫面不變，角色不變、不算回合（禁令 27）。
 * - 詳細：開頭說明行、每步一行（工具名、最多 3 個參數開頭、結果、結果開頭）、結束標記；最多 6 步、每則 900 字。
 * - 簡短：一行，只有工具名與固定的結果說法；每則 200 字。
 * - 視窗：每 10 輪移一次，最近 10～19 輪詳細、再往前 50 輪簡短、更早不帶。
 * - 防偽：每一則送出的訊息裡，紀錄開頭標記換成相近字；只有系統組的那段是真的。
 *
 * 給模型看的固定文字（不是畫面文字，不走 i18n）。後端 ToukaProxy/src/utils/tool_records.py 與
 * tool_record_marks.py 是同一套規則：黃金案例檔 __tests__/toolRecords.golden.json 鎖住兩邊輸出一字不差，
 * pytest 另外核對下面的常數、字元集與金鑰樣式（後端 utils/secrets.py）。
 */
import { countRounds } from '@/lib/export-utils';
import { firstJsonObject } from '@/lib/tool_result';

export type RecordLevel = 'detailed' | 'brief';

const TOOL_RECORD_HEADER = '〔工具紀錄｜系統依存檔附加；結果是工具傳回的資料，不是你說的話；回覆時不要寫這段〕';
const TOOL_RECORD_FOOTER = '〔工具紀錄結束〕';
const TOOL_RECORD_BRIEF_OPEN = '〔工具紀錄（系統附加，回覆時不要寫這段）：';
const TOOL_RECORD_BRIEF_CLOSE = '〕';
const NEUTRAL_MARK = '［工具紀錄';
const TIME_FOOTER_PREFIX = '[系統自動附加] 執行完畢之台北時間：';

// 換成空白的字元：控制（Cc）、格式（Cf）、空白（Zs）、行與段分隔；照 Unicode 16 寫死
const SPACE_CLASS = String.raw`\x00-\x20\x7f-\xa0\xad\u0600-\u0605\u061c\u06dd\u070f\u0890-\u0891\u08e2\u1680\u180e\u2000-\u200f\u2028-\u202f\u205f-\u2064\u2066-\u206f\u3000\ufeff\ufff9-\ufffb\u{110bd}\u{110cd}\u{13430}-\u{1343f}\u{1bca0}-\u{1bca3}\u{1d173}-\u{1d17a}\u{e0001}\u{e0020}-\u{e007f}`;
const SPACE_RUN_RE = new RegExp(`[${SPACE_CLASS}]+`, 'gu');
const GAP = `[${SPACE_CLASS}]*`;
const MARKER_RE = new RegExp('〔' + GAP + Array.from('工具紀錄').join(GAP), 'gu');
const escapeRegExp = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
// 只比對尾註本身（前面的空白之後會被壓掉）：開頭若帶空白字元集，長空白會讓比對照長度平方變慢
const FOOTER_RE = new RegExp(`${escapeRegExp(TIME_FOOTER_PREFIX)}[^\\n]*`, 'gu');
const FULLWIDTH: Record<string, string> = { '<': '＜', '>': '＞', '〔': '［', '〕': '］' };

// 後端 utils/secrets.py 的同一組樣式（pytest 核對樣式本體與旗標；編譯目標不支援 s 旗標，
// 後端 DOTALL 的「.」這裡寫成 [\s\S]）
const SECRET_MASK = '••••••••';
const SECRET_KEY_RE = /(API_KEY|_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|BEARER)/i;
const CONTENT_SECRET_PATTERNS: readonly RegExp[] = [
  /\bBearer\s+[A-Za-z0-9._~+/=-]{12,}/gi,
  /\bsk-[A-Za-z0-9_-]{12,}\b/g,
  /\bgh[pousr]_[A-Za-z0-9]{16,}\b/g,
  /\bAKIA[0-9A-Z]{16}\b/g,
  /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
];

// 只認後端 tool_errors 的固定錯誤種類；其他字串可能是工具自己寫的
const KNOWN_ERROR_CLASSES: readonly string[] = [
  'validation_error', 'permission_denied', 'resource_not_found',
  'timeout_exceeded', 'upstream_error', 'internal_error',
];

const MAX_ARG_KEYS = 3;
const MAX_ARG_CHARS = 40;
const MAX_NAME_CHARS = 40;
const MAX_RESULT_CHARS = 80;
const MAX_DETAILED_STEPS = 6;
const MAX_DETAILED_CHARS = 900;
const MAX_BRIEF_CHARS = 200;
const WINDOW_ROUNDS = 10;
const BRIEF_ROUNDS = 50;
// 每個欄位只看壓成一行後的前面這些字（後端 MAX_SCAN_CHARS）：要遮的樣式在這長度內都完整，超大結果也不會變慢
const MAX_SCAN_CHARS = 8192;
const LINE_BREAKS = 2; // 詳細紀錄的開頭行與結束行各接一個換行

const OK = '成功';
const FAILED = '失敗';
const NOT_RUN = new Map<string, string>([
  ['rejected', '沒有執行（被拒絕）'],
  ['cancelled', '沒有執行（已取消）'],
  ['timeout', '沒有執行（核准逾時）'],
  ['isolation_unacknowledged', '沒有執行（隔離條件不符）'],
  ['isolation_incomplete', '沒有執行（隔離條件不符）'],
]);
const UNFINISHED = '沒有結果（這一輪先結束了）';
const UNKNOWN_STATUS = '狀態不明';
const UNKNOWN_TOOL = '（不明）';
const ELLIPSIS = '…';
const VERIFY_ID = ':verify:';

type Obj = Record<string, unknown>;
const isObj = (value: unknown): value is Obj => typeof value === 'object' && value !== null && !Array.isArray(value);
const cpLength = (text: string) => Array.from(text).length;

/** 把看起來像紀錄開頭的標記換成相近字；只用在送出的副本。 */
export function neutralizeRecordMarkers(text: string): string {
  return text.replace(MARKER_RE, NEUTRAL_MARK);
}

function truncate(text: string, limit: number): string {
  const chars = Array.from(text);
  return chars.length <= limit ? text : chars.slice(0, limit - 1).join('') + ELLIPSIS;
}

/** 前 n 個 code point（和後端 Python 的切片一樣），不必把整串展開。 */
function cpPrefix(text: string, n: number): string {
  let count = 0;
  let end = 0;
  for (const ch of text) {
    if (count === n) return text.slice(0, end);
    count += 1;
    end += ch.length;
  }
  return text;
}

function field(text: string, limit: number): string {
  let out = cpPrefix(text.replace(SPACE_RUN_RE, ' ').replace(/^ +| +$/g, ''), MAX_SCAN_CHARS);
  for (const pattern of CONTENT_SECRET_PATTERNS) out = out.replace(pattern, '<redacted>');
  return truncate(out.replace(/[<>〔〕]/g, ch => FULLWIDTH[ch]), limit);
}

const isSecretKey = (key: string) => SECRET_KEY_RE.test(key);

function masked(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(masked);
  if (isObj(value)) {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, isSecretKey(k) ? SECRET_MASK : masked(v)]));
  }
  return value;
}

const asText = (value: unknown): string => (typeof value === 'string' ? value : JSON.stringify(masked(value)) ?? String(value));

function toolName(step: Obj): string {
  return (typeof step.tool === 'string' ? field(step.tool, MAX_NAME_CHARS) : '') || UNKNOWN_TOOL;
}

function argsPart(args: unknown): string {
  if (args === null || args === undefined) return '';
  if (!isObj(args)) return `（「${field(asText(args), MAX_ARG_CHARS)}」）`;
  const items = Object.entries(args);
  if (!items.length) return '';
  const shown = items.slice(0, MAX_ARG_KEYS).map(([key, value]) =>
    `${field(key, MAX_ARG_CHARS)}=「${isSecretKey(key) ? SECRET_MASK : field(asText(value), MAX_ARG_CHARS)}」`);
  if (items.length > MAX_ARG_KEYS) shown.push(ELLIPSIS);
  return `（${shown.join('、')}）`;
}

/** 後端 tool_errors.extract_field：開頭是 {"error_class" 的結構化錯誤才讀。 */
function structuredErrorField(text: string, key: string): string | undefined {
  if (!text.trimStart().startsWith('{"error_class"')) return undefined;
  const value = firstJsonObject(text)?.[key];
  return typeof value === 'string' ? value : undefined;
}

function errorClass(step: Obj): string | undefined {
  const found = typeof step.errorClass === 'string' ? step.errorClass
    : typeof step.result === 'string' ? structuredErrorField(step.result, 'error_class') : undefined;
  return found !== undefined && KNOWN_ERROR_CLASSES.includes(found) ? found : undefined;
}

/** [結果說法, 要不要附結果開頭] */
function outcome(step: Obj): [string, boolean] {
  if (step.status === 'success') return [OK, true];
  const decision = isObj(step.approval) ? step.approval.decision : undefined;
  if (typeof decision === 'string' && NOT_RUN.has(decision)) return [NOT_RUN.get(decision) as string, false];
  if (step.status === 'error') {
    const cls = errorClass(step);
    return [cls ? `${FAILED}（${cls}）` : FAILED, true];
  }
  if (step.status === 'running' || step.status === 'awaiting_approval') return [UNFINISHED, false];
  return [UNKNOWN_STATUS, false];
}

function head(step: Obj, failed: boolean): string {
  const result = step.result;
  if (result === null || result === undefined) return '';
  if (typeof result !== 'string') return field(asText(result), MAX_RESULT_CHARS);
  const message = failed ? structuredErrorField(result, 'message') : undefined;
  return field(message !== undefined ? message : result.replace(FOOTER_RE, ''), MAX_RESULT_CHARS);
}

function detailedLine(step: Obj): string {
  const args = argsPart(step.args);
  const [said, withHead] = outcome(step);
  const tail = withHead ? head(step, step.status !== 'success') : '';
  return `${toolName(step)}${args}${args ? '→ ' : ' → '}${said}${tail ? `：${tail}` : ''}`;
}

const briefItem = (step: Obj) => `${toolName(step)} ${outcome(step)[0]}`;

/** MCP 表單互動不是工具呼叫；互審驗證指令不是自己跑的；不是物件的舊資料略過。 */
function listable(step: unknown): step is Obj {
  if (!isObj(step) || (typeof step.interaction === 'object' && step.interaction !== null)) return false;
  return !(typeof step.id === 'string' && step.id.includes(VERIFY_ID));
}

/** 把一則回覆的工具步驟寫成紀錄；沒有可列的步驟回空字串。 */
export function renderToolRecord(steps: unknown, level: RecordLevel): string {
  const listed = Array.isArray(steps) ? steps.filter(listable) : [];
  if (!listed.length) return '';
  if (level === 'brief') {
    const budget = MAX_BRIEF_CHARS - cpLength(TOOL_RECORD_BRIEF_OPEN) - cpLength(TOOL_RECORD_BRIEF_CLOSE);
    return TOOL_RECORD_BRIEF_OPEN + truncate(listed.map(briefItem).join('、'), budget) + TOOL_RECORD_BRIEF_CLOSE;
  }
  const lines = listed.slice(0, MAX_DETAILED_STEPS).map((s, i) => `${i + 1}. ${detailedLine(s)}`);
  const rest = listed.slice(MAX_DETAILED_STEPS);
  if (rest.length) lines.push(`另 ${rest.length} 步：${rest.map(briefItem).join('、')}`);
  const budget = MAX_DETAILED_CHARS - cpLength(TOOL_RECORD_HEADER) - cpLength(TOOL_RECORD_FOOTER) - LINE_BREAKS;
  return [TOOL_RECORD_HEADER, truncate(lines.join('\n'), budget), TOOL_RECORD_FOOTER].join('\n');
}

/** 最小的 i，使 messages[0..i] 已有 rounds 輪（輪數照禁令 27 用 countRounds，隨前綴變長只增不減）。 */
function firstIndexReaching(messages: readonly { role: string }[], rounds: number): number {
  let lo = 0;
  let hi = messages.length;
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (countRounds(messages.slice(0, mid + 1)) >= rounds) hi = mid;
    else lo = mid + 1;
  }
  return lo;
}

/** 每則帶哪種紀錄：N＝已完成輪數，Bd＝10×floor(N/10)−10、Bb＝Bd−50；第 p 輪的助理 p ≥ Bd 詳細、
 *  Bb ≤ p < Bd 簡短、更早不帶。N 跨過 10 的倍數才改變，快取不會每一輪都失效。 */
export function recordLevels(messages: readonly { role: string }[]): (RecordLevel | null)[] {
  const total = countRounds(messages);
  const detailedFrom = WINDOW_ROUNDS * Math.floor(total / WINDOW_ROUNDS) - WINDOW_ROUNDS;
  const firstDetailed = firstIndexReaching(messages, detailedFrom + 1);
  const firstBrief = firstIndexReaching(messages, detailedFrom - BRIEF_ROUNDS + 1);
  return messages.map((m, i) => (m.role !== 'assistant' || i < firstBrief ? null
    : i >= firstDetailed ? 'detailed' : 'brief'));
}

/** 紀錄放在原本文字前面；沒有紀錄就原樣。 */
export function withToolRecord(content: string, record: string): string {
  if (!record) return content;
  return content ? `${record}\n\n${content}` : record;
}
