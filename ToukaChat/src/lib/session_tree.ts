import type { AgentInfo, AgentsStatus } from '@/lib/providers/AgentProvider';
import type { Session } from '@/lib/types';

const SOUL_PREFIX = 'ToukaSoul';

/** 資料夾展開時先列幾筆；其餘收在「還有 N 個」。 */
export const FOLDER_PREVIEW_COUNT = 5;

export interface SoulFolder {
  /** 不分大小寫、去掉前綴後的靈魂 ID：同一個靈魂的舊寫法都算同一個資料夾，也是收合狀態的鍵。 */
  key: string;
  name: string;
  home: Session | null;
  /** 新到舊，不含家對話。 */
  children: Session[];
}

export interface SessionTree {
  /** 依底下最新的對話排序，只由對話產生：不列空資料夾。 */
  folders: SoulFolder[];
  /** 沒有靈魂的對話（一般模型、自訂模型、群聊），新到舊。 */
  others: Session[];
}

/** 正式靈魂 ID。規則和後端 `agent_service.identify_agent` 相同：只去掉大小寫完全相符的前綴。 */
export function canonicalSoulId(agentId: string): string {
  return agentId.startsWith(SOUL_PREFIX) ? agentId.slice(SOUL_PREFIX.length) : agentId;
}

/** 這段對話能不能設為家。後端只收正式靈魂 ID，其他的按了只會被拒絕，所以不給選。 */
export function canSetHome(session: Session, agents: AgentInfo[]): boolean {
  if (session.incognito || !session.agent_id) return false;
  const id = canonicalSoulId(session.agent_id);
  return agents.some(agent => agent.id === id && agent.type === 'soul');
}

/** 分組比對從寬，比照後端 `agent_detector`：不分大小寫，處理 `toukasoul:` 與舊前綴。 */
function parseAgentRef(ref: string): { key: string; name: string; prefixed: boolean } {
  const parts = ref.split(':');
  const clean = parts[parts.length - 1];
  const prefix = SOUL_PREFIX.toLowerCase();
  const inline = clean.toLowerCase().startsWith(prefix) && clean.length > prefix.length;
  const name = inline ? clean.slice(prefix.length) : clean;
  return { key: name.toLowerCase(), name, prefixed: inline || parts[parts.length - 2]?.toLowerCase() === prefix };
}

function soulsByKey(agents: AgentInfo[]): Map<string, AgentInfo> {
  const souls = agents.filter(agent => agent.type === 'soul' || agent.type === 'soul_placeholder');
  const map = new Map<string, AgentInfo>();
  for (const soul of souls) for (const alias of soul.aliases ?? []) map.set(alias.toLowerCase(), soul);
  // 正式 ID 排後面：別名不能蓋掉另一個靈魂
  for (const soul of souls) map.set(soul.id.toLowerCase(), soul);
  return map;
}

/** 排序與列上日期用的時間：存檔時間和看到它在工作的時刻取較新的（不動對話內容）。 */
export function sessionSortTime(session: Session, activityTimeOf?: (sessionId: string) => number | undefined): number {
  return Math.max(session.updatedAt || 0, activityTimeOf?.(session.id) ?? 0);
}

export function buildSessionTree(sessions: Session[], agents: AgentInfo[], status: AgentsStatus,
  activityTimeOf?: (sessionId: string) => number | undefined): SessionTree {
  const souls = soulsByKey(agents);
  const folders = new Map<string, SoulFolder>();
  const others: Session[] = [];
  const newestFirst = [...sessions].sort((a, b) => sessionSortTime(b, activityTimeOf) - sessionSortTime(a, activityTimeOf));

  for (const session of newestFirst) {
    const ref = session.agent_id || session.home_for_agent;
    const parsed = ref ? parseAgentRef(ref) : null;
    const soul = parsed && souls.get(parsed.key);
    // 靈魂清單沒讀到時，純 ID 分不出是靈魂還是模型，只信得過前綴與家對話
    const guessed = parsed && (parsed.prefixed || (status === 'failed' && session.is_home));
    // 冒號後面沒有名字（`toukasoul:`）不開空白名稱的資料夾
    if (!parsed?.key || (!soul && !guessed)) {
      others.push(session);
      continue;
    }
    const key = soul ? soul.id.toLowerCase() : parsed.key;
    const folder = folders.get(key) ?? { key, name: soul ? soul.name : parsed.name, home: null, children: [] };
    folders.set(key, folder);
    if (session.is_home && !session.incognito && !folder.home) folder.home = session;
    else folder.children.push(session);
  }
  return { folders: [...folders.values()], others };
}

/** 先列幾筆，其餘收起來；`pinned`（開著的、工作中的、等您處理的對話）不收。 */
export function previewChildren(children: Session[], showAll: boolean, pinned: ReadonlySet<string>):
  { visible: Session[]; hiddenCount: number } {
  if (showAll) return { visible: children, hiddenCount: 0 };
  const visible = children.filter((session, index) => index < FOLDER_PREVIEW_COUNT || pinned.has(session.id));
  return { visible, hiddenCount: children.length - visible.length };
}
