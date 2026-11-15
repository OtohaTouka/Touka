import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryNoticeBar } from '../MemoryNoticeBar';
import { DegradedBanner } from '../DegradedBanner';
import zhTW from '@/locales/zh-TW/translation.json';
import en from '@/locales/en/translation.json';

// 記憶庫結構的警告代碼：四個失敗狀態＋新庫旁有升級備份（後端 observability.py 的 MEMORY_NOTICE_*）。
// 後端送出前端不認得的警告代碼時，左側會亮警告標記、右側卻沒有內容，所以每個都要有對應。
const SCHEMA_CODES = [
  'memory_upgrade_no_space',
  'memory_upgrade_locked',
  'memory_store_unreadable',
  'memory_upgrade_failed',
  'memory_new_store_beside_backups',
] as const;
// 嵌入模型對不上（ADR-0315）：依判斷依據分三種句子，每種都要有中英文。
const EMBEDDING_KEYS = [
  'embedding_model_mismatch_name',
  'embedding_model_mismatch_length',
  'embedding_model_mismatch_sample',
] as const;

const colorTokens = (element: HTMLElement) =>
  element.className.split(/\s+/).filter(token => token.startsWith('bg-') || token.startsWith('text-')).sort();

const messages = (catalog: unknown): Record<string, string> =>
  (catalog as { sidebar: { memory_notice: Record<string, string> } }).sidebar.memory_notice;

describe('MemoryNoticeBar schema notices', () => {
  it.each(SCHEMA_CODES)('shows %s as a translated alert as prominent as the degraded banner', code => {
    render(
      <>
        <DegradedBanner degraded reason="synthetic reason" />
        <MemoryNoticeBar notices={[{ code, level: 'warning' }]} />
      </>,
    );
    const notice = screen.getByTestId('memory-notice');
    expect(notice).toHaveAttribute('role', 'alert');
    expect(colorTokens(notice)).toEqual(colorTokens(screen.getByTestId('degraded-banner')));
    expect(notice.textContent).toBe(messages(zhTW)[code]);
    expect(notice.textContent).not.toContain('memory_notice');
  });

  it.each(SCHEMA_CODES)('has an English message for %s', code => {
    expect(messages(en)[code]?.length ?? 0).toBeGreaterThan(0);
  });

  it.each(EMBEDDING_KEYS)('has Chinese and English messages for %s', key => {
    expect(messages(zhTW)[key]?.length ?? 0).toBeGreaterThan(0);
    expect(messages(en)[key]?.length ?? 0).toBeGreaterThan(0);
  });

  it('shows the embedding model warning as an alert as prominent as the degraded banner', () => {
    render(
      <>
        <DegradedBanner degraded reason="synthetic reason" />
        <MemoryNoticeBar notices={[{ code: 'embedding_model_mismatch', level: 'warning', count: 1,
          basis: 'length', configured_model: 'intfloat/multilingual-e5-base', index_dim: 384 }]} />
      </>,
    );
    const notice = screen.getByTestId('memory-notice');
    expect(notice).toHaveAttribute('role', 'alert');
    expect(colorTokens(notice)).toEqual(colorTokens(screen.getByTestId('degraded-banner')));
  });
});
