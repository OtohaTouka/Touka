/**
 * resolveSoulId — X-Touka-Soul-ID 由 agent SSOT 衍生（不寫死任何 agent 名稱）。
 *
 * 背景：後端 middleware/layers/monitor.py 以 `x-touka-soul-id` 為「優先權 1 身份來源」
 * 呼叫 identify_agent()。故此 header 只能對 SSOT 確認的 agent 設定，
 * 嚴禁對任意 provider model（gpt-4 等）誤設造成身份誤判。
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { resolveSoulId, setKnownAgentIds } from '../connections';

describe('resolveSoulId（agent SSOT 衍生，零硬編碼）', () => {
    beforeEach(() => {
        // SSOT：實際由 AgentProvider 從 /v1/agents 灌入；此處手動模擬
        setKnownAgentIds(['Alice', 'Bob', 'ToukaGroup', 'DebugAgent']);
    });

    it('已知 agent（大小寫不拘）→ 回傳 SSOT 正規 id', () => {
        expect(resolveSoulId('Alice')).toBe('Alice');
        expect(resolveSoulId('alice')).toBe('Alice');
        expect(resolveSoulId('toukagroup')).toBe('ToukaGroup');
    });

    it('非 agent 的 provider model → 不回傳 soul id（避免身份誤判）', () => {
        expect(resolveSoulId('gpt-4')).toBe('');
        expect(resolveSoulId('claude-3-5-sonnet')).toBe('');
    });

    it('toukasoul 前綴 → 去前綴並正規化', () => {
        expect(resolveSoulId('ToukaSoulAlice')).toBe('Alice');
        expect(resolveSoulId('toukasouldebugagent')).toBe('DebugAgent');
    });

    it('SSOT 快取未載入時 → 不誤設 soul id', () => {
        setKnownAgentIds([]);
        expect(resolveSoulId('Alice')).toBe('');
    });
});
