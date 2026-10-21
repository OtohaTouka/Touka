/**
 * Session 欄位持久化 parity 守衛（四功能區調查 P4／ADR-0161）。
 *
 * 病史：deepResearch 前端存在 Session 上並經 syncSession 送出，但後端 SessionModel
 * 無此欄＝POST 被靜默丟棄、切 session/重整歸零（與已修的 auth_level H1 同 bug 類）。
 * 本守衛把「前端 Session 欄位 ⇄ 後端 SessionModel」升為分類台帳：
 * - PERSISTED：需跨重整持久 → 必須在 SessionModel（漏接即紅，第一天就抓 deepResearch 這類）。
 * - TRANSIENT：純前端/逐則、刻意不持久 → 必須**不在** SessionModel（語意鎖：若有人給
 *   deepResearch 加後端欄位＝偷偷變持久化，這裡紅提醒違背 ADR-0087/0161 逐則決策）。
 * - READ_ONLY：後端 GET 回傳、前端不經 SessionModel POST 寫（不強制在/不在）。
 * 前端 Session 每個欄位都必須被分類——新增未分類即紅，杜絕漏接後端。
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const TYPES = path.resolve(__dirname, '../../src/lib/types.ts');
const OPENAPI = path.resolve(__dirname, '../../../openapi.json');

// 前端 Session 欄位分類台帳（新增欄位須落其一）
const CORE = ['id', 'title', 'messages', 'updatedAt'];
const PERSISTED = ['is_home', 'home_for_agent', 'agent_id', 'project_id', 'auth_level', 'active_skills', 'active_mcp_servers', 'context_summary'];
const READ_ONLY = ['round_count'];
// POST 前置版本，必須進 SessionModel；不是 DB 欄位或使用者資料。
const TRANSPORT = ['revision'];
const TRANSIENT = ['incognito', 'deepResearch', 'mcpDispatcher'];
const CLASSIFIED = new Set([...CORE, ...PERSISTED, ...READ_ONLY, ...TRANSIENT, ...TRANSPORT]);

function frontendSessionFields(): string[] {
  const src = readFileSync(TYPES, 'utf-8');
  const m = src.match(/export interface Session \{([\s\S]*?)\n\}/);
  if (!m) throw new Error('找不到 export interface Session');
  return [...m[1].matchAll(/^\s+([a-zA-Z_]\w*)\??:/gm)].map(x => x[1]);
}

function sessionModelProps(): Set<string> {
  const oa = JSON.parse(readFileSync(OPENAPI, 'utf-8'));
  const props = oa.components?.schemas?.SessionModel?.properties ?? {};
  return new Set(Object.keys(props));
}

describe('Session 欄位持久化 parity（P4/ADR-0161 分類台帳）', () => {
  it('前端 Session 每個欄位都必須被分類（新增未分類即紅）', () => {
    for (const f of frontendSessionFields()) {
      expect(CLASSIFIED.has(f), `Session 欄位「${f}」未分類——請落入 CORE/PERSISTED/READ_ONLY/TRANSIENT/TRANSPORT`).toBe(true);
    }
  });

  it('PERSISTED＋CORE 欄位必須在後端 SessionModel（漏接＝POST 被靜默丟棄）', () => {
    const model = sessionModelProps();
    for (const f of [...CORE, ...PERSISTED]) {
      expect(model.has(f), `需持久化欄位「${f}」不在 SessionModel——前端送出會被 Pydantic 靜默丟棄`).toBe(true);
    }
  });

  it('TRANSPORT 必須被公開 SessionModel 接收，但不是持久內容欄位', () => {
    const model = sessionModelProps();
    const oa = JSON.parse(readFileSync(OPENAPI, 'utf-8'));
    const content = oa.components.schemas.SessionDocument.properties;
    for (const field of TRANSPORT) {
      expect(model.has(field)).toBe(true);
      expect(content).not.toHaveProperty(field);
      expect(PERSISTED).not.toContain(field);
    }
  });

  it('TRANSIENT 逐則欄位必須不在 SessionModel（語意鎖：deepResearch/mcpDispatcher 不持久化）', () => {
    const model = sessionModelProps();
    for (const f of TRANSIENT) {
      expect(model.has(f), `逐則欄位「${f}」出現在 SessionModel——若刻意改為持久化，請移出 TRANSIENT 並更新 ADR`).toBe(false);
    }
  });
});
