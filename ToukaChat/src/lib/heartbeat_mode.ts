import type { components } from '@/types/api';

type AuthMode = components['schemas']['SessionDocument']['auth_level'];

/** 心跳排程 security_level → 模式；和後端 heartbeat 的對應一致（ADR-0246）。 */
const HEARTBEAT_MODES: Record<string, AuthMode> = { safe: 'Auto', full: 'YOLO' };

/** 看不懂的值一律顯示最嚴的 Manual，和後端的失敗預設相同。 */
export function heartbeatMode(securityLevel: string | null | undefined): AuthMode {
  return securityLevel && Object.hasOwn(HEARTBEAT_MODES, securityLevel)
    ? HEARTBEAT_MODES[securityLevel]
    : 'Manual';
}
