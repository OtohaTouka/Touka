import type { IsolationLevelName, ToolStepIsolation } from './tree_history';
import type { components } from '@/types/api';

/**
 * iso-c2 C2-2（設計 2.3 版 §2.8）：隔離說明要用哪幾句（純函式，審批卡與側欄橫幅共用）。
 * 只回翻譯 key 的片段；文字在 `chat.tool_approval_card.isolation` 底下。
 */

/** 開頭的原因子句：決定降級去處的那個原因。generic 是 C2-1 的「這次無法使用完整隔離」。 */
export type IsolationClause =
  | 'generic' | 'scope' | 'executor_down' | 'net' | 'ports_incomplete' | 'ports_overflow';

const SCOPE_REASONS = new Set(['project_not_in_executor', 'agent_dir_not_in_executor']);
const EXECUTOR_DOWN_REASONS = new Set([
  'executor_unreachable', 'executor_auth_failed', 'executor_version_mismatch',
  'executor_roots_mismatch', 'executor_selftest_failed', 'executor_restarted', 'executor_rejected',
]);
const NET_REASONS = new Set(['net_tcp_fastopen', 'net_mptcp']);
// 擋不住任何 TCP（網路設定或核心不夠新）：結尾說擋不住，不說「Touka 自己的埠照樣擋」
const TCP_OPEN_REASONS = new Set([...NET_REASONS, 'landlock_abi_insufficient']);
const PORT_CLAUSES: Record<string, IsolationClause> = {
  block_ports_incomplete: 'ports_incomplete',
  block_ports_overflow: 'ports_overflow',
};

/** 原因子句（去處是 executor／landlock 時）：本機原因 → 範圍 → 執行器掛了 → 埠擋不全。 */
export function isolationClause(isolation: ToolStepIsolation): IsolationClause {
  const reason = isolation.reason ?? '';
  if (SCOPE_REASONS.has(reason)) return 'scope';
  const tcpOnly = NET_REASONS.has(reason) || reason in PORT_CLAUSES;
  // 預期執行器時，其他原因都是「執行器不能用」（含還在檢查、kernel 和映像不一致）
  if (EXECUTOR_DOWN_REASONS.has(reason) || (isolation.expected === 'executor' && reason && !tcpOnly)) {
    return 'executor_down';
  }
  if (NET_REASONS.has(reason)) return isolation.fallback === 'executor' ? 'net' : 'generic';
  return PORT_CLAUSES[reason] ?? 'generic';
}

/** 結尾（去處是 executor／landlock 時）：有 tcp 就擋得住；沒有時網路設定與封鎖埠各一句。 */
export function isolationTcpTails(isolation: ToolStepIsolation): string[] {
  if (isolation.features.includes('tcp')) return ['degraded_tcp_blocked'];
  const reason = isolation.reason ?? '';
  const open = Boolean(isolation.net) || TCP_OPEN_REASONS.has(reason);
  const ports = Boolean(isolation.blockPortsIssue) || reason in PORT_CLAUSES;
  if (!ports) return ['degraded_tcp_open'];
  return open ? ['degraded_tcp_open', 'ports_tail_also'] : ['ports_tail'];
}

/** 原因那幾行（都寫補救方式）：去處是未隔離時本機原因在前，再補網路設定與封鎖埠；去重。 */
export function isolationReasonCodes(isolation: ToolStepIsolation): string[] {
  const local = isolation.fallback === 'direct' ? isolation.localIssue : undefined;
  const codes = [local, isolation.reason, isolation.net, isolation.blockPortsIssue];
  return [...new Set(codes.filter((code): code is string => Boolean(code)))];
}

/** 網路設定的覆寫要加在哪些服務：預期執行器時兩個都要加（MPTCP 是整台主機的設定）。 */
export function netServicesKey(expected: IsolationLevelName | undefined): string {
  return expected === 'executor' ? 'executor' : 'proxy';
}

const LEVEL_RANK: Record<IsolationLevelName, number> = { none: 0, landlock: 1, executor: 2 };
// 執行器控制通道（ipc）的權限被反覆改時會出現的原因：連不上或權杖讀不到
const CONTROL_CHANNEL_REASONS = new Set(['executor_unreachable', 'executor_auth_failed']);

/** 側欄橫幅要顯示哪幾列（GET /isolation）；三個旗標都是 false 就不顯示橫幅。 */
export interface IsolationBannerView {
  below: boolean;          // 層級低於部署預期
  tcpMissing: boolean;     // 層級照預期，但埠沒擋（例如執行器的核心是 ABI 3）
  relock: boolean;         // 自癒這一分鐘的額度用完（有代理人反覆鎖權限）
  reasons: string[];       // 低於預期時的原因代碼（寫補救方式）
  controlChannel: boolean; // 補一句：可能是執行器控制通道的權限被反覆改
}

/** denied_burst（大量被拒）只記 log，不上橫幅。 */
export function isolationBannerView(
  status: components['schemas']['IsolationStatusResponse'],
): IsolationBannerView {
  const below = LEVEL_RANK[status.level] < LEVEL_RANK[status.expected];
  const reasons = below ? [status.reason, status.block_ports_issue] : [];
  return {
    below,
    tcpMissing: !below && status.level !== 'none' && !status.features.includes('tcp'),
    relock: status.repairs?.permission_relock === true,
    reasons: [...new Set(reasons.filter((code): code is string => Boolean(code)))],
    controlChannel: below && status.expected === 'executor' && CONTROL_CHANNEL_REASONS.has(status.reason ?? ''),
  };
}
