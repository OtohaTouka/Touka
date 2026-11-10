import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import en from '@/locales/en/translation.json';
import zhTW from '@/locales/zh-TW/translation.json';
import { PricingNote } from '../PricingNote';

/** 費用估算的依據（ADR-0311）：價目表查證日期、自己改過的價格、沒有具體價格的模型。 */
describe('PricingNote', () => {
  const note = () => screen.getByTestId('pricing-note');

  it('顯示價目表的查證日期', () => {
    render(<PricingNote pricing={{ verified_at: '2026-11-10', has_override: false, estimated_models: [] }} />);
    expect(note()).toHaveTextContent('2026-11-10');
    expect(note()).not.toHaveTextContent('不含');
  });

  it('有自己改過的價格時說明日期不含那些', () => {
    render(<PricingNote pricing={{ verified_at: '2026-11-10', has_override: true, estimated_models: [] }} />);
    expect(note()).toHaveTextContent('不含');
  });

  it('列出沒有具體價格、用該家最高價估的模型', () => {
    render(<PricingNote pricing={{
      verified_at: '2026-11-10', has_override: false, estimated_models: ['gpt-9', 'grok-9'],
    }} />);
    expect(screen.getByTestId('pricing-estimated')).toHaveTextContent('gpt-9、grok-9');
  });

  it('都有具體價格時不出現那一行', () => {
    render(<PricingNote pricing={{ verified_at: '2026-11-10', has_override: false, estimated_models: [] }} />);
    expect(screen.queryByTestId('pricing-estimated')).toBeNull();
  });

  it.each([[null], [{}], [{ verified_at: 7 }]])('看不懂的資料就不顯示（%j）', (pricing) => {
    const { container } = render(<PricingNote pricing={pricing as never} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('estimated_models 不是清單時當成沒有', () => {
    render(<PricingNote pricing={{ verified_at: '2026-11-10', estimated_models: 'x' } as never} />);
    expect(note()).toHaveTextContent('2026-11-10');
    expect(screen.queryByTestId('pricing-estimated')).toBeNull();
  });

  it('中英文都有這幾句', () => {
    for (const locale of [en, zhTW]) {
      expect(locale.sidebar.pricing_note.verified_at).toContain('{{date}}');
      expect(locale.sidebar.pricing_note.override).toBeTruthy();
      expect(locale.sidebar.pricing_note.estimated).toContain('{{models}}');
    }
  });
});
