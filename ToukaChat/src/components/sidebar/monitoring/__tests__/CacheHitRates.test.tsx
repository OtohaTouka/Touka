import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import en from '@/locales/en/translation.json';
import zhTW from '@/locales/zh-TW/translation.json';
import { CacheHitRates } from '../CacheHitRates';

/** 近 7 天的快取命中率（ADR-0311）：後端算好，畫面照「靈魂 · 用途」分列。 */
describe('CacheHitRates', () => {
  it('每個靈魂的每個用途各一列，比例換成百分比', () => {
    render(<CacheHitRates rates={{ Alice: { Anthropic: 0.625 }, Bob: { Gemini: 0.1 } }} />);
    expect(screen.getByTestId('cache-hit-Alice-Anthropic')).toHaveTextContent('63%');
    expect(screen.getByTestId('cache-hit-Bob-Gemini')).toHaveTextContent('10%');
  });

  it('0% 是真的數字，不是沒有資料', () => {
    render(<CacheHitRates rates={{ Alice: { Anthropic: 0 } }} />);
    expect(screen.getByTestId('cache-hit-Alice-Anthropic')).toHaveTextContent('0%');
  });

  it.each([[null], ['x'], [undefined]])('這段時間沒有輸入（%s）顯示 —', (value) => {
    render(<CacheHitRates rates={{ Alice: { Anthropic: value as number | null } }} />);
    expect(screen.getByTestId('cache-hit-Alice-Anthropic')).toHaveTextContent('—');
  });

  it.each([
    ['summarizer', '摘要員'],
    ['critic', '互審'],
    ['judge', '創建評分'],
    ['auto_reviewer', 'Auto 審查員'],
    ['inline_completion', '行內補全'],
  ])('已知的用途給好懂的名字：%s', (label, name) => {
    render(<CacheHitRates rates={{ Alice: { [label]: 0.5 } }} />);
    expect(screen.getByTestId(`cache-hit-Alice-${label}`)).toHaveTextContent(name);
  });

  it('廠商和自訂連線照鍵名顯示', () => {
    render(<CacheHitRates rates={{ Alice: { 'my-relay': 0.5 } }} />);
    expect(screen.getByTestId('cache-hit-Alice-my-relay')).toHaveTextContent('my-relay');
  });

  it('物件的內建屬性名不會被當成已知用途', () => {
    render(<CacheHitRates rates={{ Alice: { constructor: 0.5 } }} />);
    expect(screen.getByTestId('cache-hit-Alice-constructor')).toHaveTextContent('constructor');
  });

  it.each([
    [{}],
    [{ Alice: {} }],
    [{ Alice: null } as unknown as Record<string, Record<string, number>>],
    [undefined as unknown as Record<string, Record<string, number>>],
  ])(
    '沒有資料就不佔位置',
    (rates) => {
      const { container } = render(<CacheHitRates rates={rates} />);
      expect(container).toBeEmptyDOMElement();
    },
  );

  it('中英文的標題和用途名稱都有', () => {
    for (const locale of [en, zhTW]) {
      const copy = locale.sidebar.cache_hit;
      expect(copy.title).toBeTruthy();
      expect(Object.keys(copy.labels).sort()).toEqual(
        ['auto_reviewer', 'critic', 'inline_completion', 'judge', 'summarizer'],
      );
    }
  });
});
