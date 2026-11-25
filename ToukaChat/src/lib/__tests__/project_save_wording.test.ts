/**
 * 專用空間的登記被拒絕時（ADR-0332），設定頁說得出原因：
 * 原因（detail.reason）優先，再來是專用空間的錯誤碼，其他一律是一般的「存不了」。
 * 清不掉 HOME 時，不會自己好的原因（掃描、執行器設定）說原因，其他說稍後再試。
 */
import { describe, it, expect } from 'vitest';
import i18n from '@/lib/i18n';
import type { ErrorCode } from '@/lib/error_codes';
import { projectSaveFailureText } from '../project_save_wording';

const t = i18n.t.bind(i18n);
const GENERIC = i18n.t('projects.projects_view.save_failed');
const REASONS = [
  'project_space_root',
  'project_not_top_level',
  'project_name_unsafe',
  'project_linked',
  'project_space_unavailable',
  'project_path_form',
];
const HOME_REASONS = [
  'protected_location',
  'scan_limit',
  'executor_no_project_root',
  'executor_project_root_mismatch',
];
const CODES: Array<[ErrorCode, string]> = [
  ['PROJECT_FOLDER_TAKEN', 'error.project.folder_taken'],
  ['PROJECT_FOLDER_MISSING', 'error.project.folder_missing'],
  ['PROJECT_HOME_NOT_CLEARED', 'error.project.home_not_cleared'],
];

describe('projectSaveFailureText（專用空間的登記，ADR-0332）', () => {
  it.each(REASONS)('被拒原因 %s → 自己的說明（兩語都有）', (reason) => {
    const key = `projects.projects_view.space_reasons.${reason}`;
    expect(i18n.exists(key, { lng: 'zh-TW' })).toBe(true);
    expect(i18n.exists(key, { lng: 'en' })).toBe(true);
    const text = projectSaveFailureText(t, { code: 'VALIDATION_ERROR', reason });
    expect(text).toBe(i18n.t(key));
    expect(text).not.toBe(GENERIC);
  });

  it.each(CODES)('錯誤碼 %s → 錯誤碼自己的說明（兩語都有，原因不影響）', (code, key) => {
    expect(i18n.exists(key, { lng: 'zh-TW' })).toBe(true);
    expect(i18n.exists(key, { lng: 'en' })).toBe(true);
    expect(projectSaveFailureText(t, { code, reason: 'executor_unreachable' })).toBe(i18n.t(key));
  });

  it.each(HOME_REASONS)('清不掉 HOME 的原因 %s → 自己的說明（兩語都有，不說稍後再試）', (reason) => {
    const key = `projects.projects_view.home_reasons.${reason}`;
    expect(i18n.exists(key, { lng: 'zh-TW' })).toBe(true);
    expect(i18n.exists(key, { lng: 'en' })).toBe(true);
    const text = projectSaveFailureText(t, { code: 'PROJECT_HOME_NOT_CLEARED', reason });
    expect(text).toBe(i18n.t(key));
    expect(text).not.toBe(i18n.t('error.project.home_not_cleared'));
  });

  it.each([
    ['驗證錯誤但原因不認得', { code: 'VALIDATION_ERROR' as ErrorCode, reason: 'something_else' }],
    ['驗證錯誤沒有原因', { code: 'VALIDATION_ERROR' as ErrorCode }],
    ['專用空間的原因但不是驗證錯誤', { code: 'AUTH_PERMISSION_DENIED' as ErrorCode, reason: 'project_linked' }],
    ['專用空間的原因但沒有錯誤碼', { reason: 'project_linked' }],
    ['清不掉 HOME 的原因但是別的錯誤碼', { code: 'AUTH_PERMISSION_DENIED' as ErrorCode, reason: 'scan_limit' }],
    ['其他錯誤碼', { code: 'AUTH_PERMISSION_DENIED' as ErrorCode }],
    ['斷線（什麼都沒有）', {}],
  ])('%s → 一般的「存不了」', (_name, failure) => {
    expect(projectSaveFailureText(t, failure)).toBe(GENERIC);
  });
});
