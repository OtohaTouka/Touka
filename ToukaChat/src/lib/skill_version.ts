import { Skill, CatalogSkill } from '@/lib/types';

/** 真技能目錄條目相對於已安裝技能的版本狀態（ADR-0074 版本追蹤）。 */
export type CatalogStatus = 'not_installed' | 'up_to_date' | 'update_available';

/**
 * 比對策展目錄條目與已安裝技能，判定版本狀態。
 *
 * - `not_installed`：尚未安裝。
 * - `up_to_date`：已安裝且 commit 與目錄一致；或已安裝但無 provenance
 *   （舊版/URL 安裝，無法判定）→ 視為最新、不催更新。
 * - `update_available`：已安裝、有 provenance，但 commit 與目錄釘的新版不同。
 *
 * 衍生自兩個 SSOT：已安裝技能的 `source_commit`（後端 sidecar）與目錄條目的
 * `commit`（策展釘版）。本函式不持有狀態，純比對、易測。
 */
export function computeCatalogStatus(
  entry: CatalogSkill,
  installed: Skill[],
): CatalogStatus {
  const match = installed.find((s) => s.id === entry.id);
  if (!match) return 'not_installed';
  if (!match.source_commit) return 'up_to_date';
  return match.source_commit === entry.commit ? 'up_to_date' : 'update_available';
}
