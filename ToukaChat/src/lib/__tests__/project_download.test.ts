/**
 * 下載專案（ADR-0332 PR B）：被拒絕時說得出原因（兩語都有），下載的檔名安全。
 */
import { describe, it, expect } from 'vitest';
import i18n from '@/lib/i18n';
import type { ErrorCode } from '@/lib/error_codes';
import { projectDownloadFailureText, projectDownloadFileName } from '../project_download';

const t = i18n.t.bind(i18n);
const GENERIC = i18n.t('projects.projects_view.download_failed');
const NOW = new Date('2026-11-25T08:00:00Z');

const VALIDATION_REASONS = [
  'not_dedicated_space',
  'no_git',
  'git_not_folder',
  'git_is_link',
  'no_index',
  'index_unsupported',
  'index_invalid',
  'protected_location',
  'outside_workspace',
  'scan_limit',
];
const TOO_LARGE_REASONS = ['index_too_large', 'too_many_files', 'too_large', 'walk_too_large'];
const SPACE_REASONS = [
  'project_space_root',
  'project_not_top_level',
  'project_name_unsafe',
  'project_linked',
  'project_space_unavailable',
];

function expectBothLanguages(key: string) {
  expect(i18n.exists(key, { lng: 'zh-TW' })).toBe(true);
  expect(i18n.exists(key, { lng: 'en' })).toBe(true);
}

describe('projectDownloadFailureText', () => {
  it.each(VALIDATION_REASONS)('驗證錯誤的原因 %s → 自己的說明', (reason) => {
    const key = `projects.projects_view.download_reasons.${reason}`;
    expectBothLanguages(key);
    const text = projectDownloadFailureText(t, { code: 'VALIDATION_ERROR', reason });
    expect(text).toBe(i18n.t(key));
    expect(text).not.toBe(GENERIC);
  });

  it.each(TOO_LARGE_REASONS)('太大的原因 %s → 自己的說明', (reason) => {
    const key = `projects.projects_view.download_reasons.${reason}`;
    expectBothLanguages(key);
    expect(projectDownloadFailureText(t, { code: 'REQUEST_TOO_LARGE', reason })).toBe(i18n.t(key));
  });

  it.each(SPACE_REASONS)('專用空間的形狀 %s → 和登記時同一句說明', (reason) => {
    const text = projectDownloadFailureText(t, { code: 'VALIDATION_ERROR', reason });
    expect(text).toBe(i18n.t(`projects.projects_view.space_reasons.${reason}`));
  });

  it.each([
    ['PROJECT_DOWNLOAD_BUSY', 'error.project.download_busy'],
    ['PROJECT_FOLDER_TAKEN', 'error.project.folder_taken'],
  ] as const)('%s → 錯誤碼自己的說明', (code, key) => {
    expectBothLanguages(key);
    expect(projectDownloadFailureText(t, { code })).toBe(i18n.t(key));
  });

  it('沒有權限 → 說只有主人能下載（不是「稍後再試」）', () => {
    expectBothLanguages('projects.projects_view.download_not_allowed');
    expect(projectDownloadFailureText(t, { code: 'AUTH_PERMISSION_DENIED' })).toBe(
      i18n.t('projects.projects_view.download_not_allowed'),
    );
  });

  it('資料夾不在 → 下載用的說明（不是登記用的）', () => {
    expectBothLanguages('projects.projects_view.download_folder_missing');
    expect(projectDownloadFailureText(t, { code: 'PROJECT_FOLDER_MISSING' })).toBe(
      i18n.t('projects.projects_view.download_folder_missing'),
    );
  });

  it.each([
    ['原因不認得', { code: 'VALIDATION_ERROR' as ErrorCode, reason: 'something_else' }],
    ['太大的原因配上別的錯誤碼', { code: 'VALIDATION_ERROR' as ErrorCode, reason: 'too_large' }],
    ['驗證的原因配上太大', { code: 'REQUEST_TOO_LARGE' as ErrorCode, reason: 'no_git' }],
    ['伺服器出錯', { code: 'SYSTEM_ERROR' as ErrorCode }],
    ['斷線', {}],
  ])('%s → 一般的「下載失敗」', (_name, failure) => {
    expect(projectDownloadFailureText(t, failure)).toBe(GENERIC);
  });
});

describe('projectDownloadFileName', () => {
  it('專案名稱加日期', () => {
    expect(projectDownloadFileName('repo', NOW)).toBe('repo-2026-11-25.zip');
  });

  it.each([
    ['my/proj:1', 'my_proj_1'],
    ['a\\b<c>d|e?f*g"h', 'a_b_c_d_e_f_g_h'],
    ['tab\there\u0000', 'tab_here_'],
    ['bidi‮txt', 'bidi_txt'],
    ['  .hidden. ', 'hidden'],
    ['', 'project'],
    ['...', 'project'],
    ['中文專案', '中文專案'],
  ])('不安全的字元換掉：%j → %j', (name, stem) => {
    expect(projectDownloadFileName(name, NOW)).toBe(`${stem}-2026-11-25.zip`);
  });

  it('太長的名稱截短', () => {
    const name = projectDownloadFileName('x'.repeat(300), NOW);
    expect(name.length).toBeLessThanOrEqual(80);
    expect(name.endsWith('-2026-11-25.zip')).toBe(true);
  });
});
