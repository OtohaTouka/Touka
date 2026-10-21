import { describe, it, expect } from 'vitest';
import { extractMentionPaths } from '@/lib/mentions';

describe('extractMentionPaths — 去尾標點不吞、保留路徑內部符號與中文', () => {
  it.each([
    ['@Alice.', 'Alice'],
    ['@Bob, hi', 'Bob'],
    ['@Carl!', 'Carl'],
    ['@Dan?', 'Dan'],
    ['看 @Eve 結尾', 'Eve'],
    ['(@Fay)', 'Fay'],
    ['@config.json。', 'config.json'], // 全形句號
    ['@小愛！', '小愛'], // 中文別名 + 全形驚嘆號
  ])('「%s」應解析出「%s」', (input, expected) => {
    expect(extractMentionPaths(input)).toEqual([expected]);
  });

  it('保留檔案路徑內部的 . 與 /（不誤切副檔名）', () => {
    expect(extractMentionPaths('看 @src/lib/utils.ts 這檔')).toEqual(['src/lib/utils.ts']);
  });

  it('尾端為副檔名的路徑不被剝除（句末標點才剝）', () => {
    expect(extractMentionPaths('@README.md')).toEqual(['README.md']);
  });

  it('多個 mention 去重且保序', () => {
    expect(extractMentionPaths('@git @terminal @git.')).toEqual(['git', 'terminal']);
  });

  it('無 mention 時回空陣列', () => {
    expect(extractMentionPaths('沒有任何標記')).toEqual([]);
  });

  it('裸 @ 或只有標點的 mention 不產生空白項', () => {
    expect(extractMentionPaths('@ @. @!')).toEqual([]);
  });
});
