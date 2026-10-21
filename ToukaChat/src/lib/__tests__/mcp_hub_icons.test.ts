/**
 * 跨層守衛（ADR-0191）：後端 Hub 種子用到的每個 `icon` 值都必須在前端 `IconMap` 裡。
 *
 * 缺鍵不會報錯——`McpHubItem` 會靜默 fallback 成 `Box`，卡片照樣渲染、只是圖示悄悄
 * 變成通用方塊（無聲失效家族，同 ADR-0187 的 response_model 剝除、ADR-0188 的
 * pydantic extra='ignore' 靜默吞）。落地時實際抓到兩個既有缺鍵：`FileText`（curated
 * mcp-docgen）與 `ShieldCheck`（官方三條）——出廠貨架四條全數圖示失效卻無人察覺。
 *
 * 讀後端種子檔而非硬編清單＝真 SSOT 比對：策展人新增條目時選了 IconMap 沒有的名字，
 * 本測試立刻紅。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { IconMap } from '@/components/settings/tabs/mcp/McpHubItem';

const SEED_PATH = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../../../ToukaProxy/config/mcp_hub.json.example',
);

interface SeedEntry {
  id: string;
  icon?: string;
}

function seedEntries(): SeedEntry[] {
  return JSON.parse(readFileSync(SEED_PATH, 'utf-8')).hub as SeedEntry[];
}

describe('MCP Hub 種子 icon 跨層涵蓋', () => {
  it('種子檔讀得到且非空（路徑漂移即紅，避免本守衛空轉成假綠）', () => {
    const entries = seedEntries();
    expect(entries.length).toBeGreaterThan(0);
    expect(entries.every((e) => typeof e.id === 'string')).toBe(true);
  });

  it('每個種子條目的 icon 都能在 IconMap 查到元件', () => {
    const missing = seedEntries()
      .filter((e) => e.icon && !IconMap[e.icon])
      .map((e) => `${e.id} → ${e.icon}`);
    expect(missing).toEqual([]);
  });
});
