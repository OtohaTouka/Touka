import { describe, it, expect } from 'vitest';
import { summarizeImport } from '../import_utils';

describe('summarizeImport（匯入摘要純函式）', () => {
    it('無重疊時全為全新', () => {
        expect(summarizeImport(['a', 'b'], ['c'])).toEqual({ total: 2, fresh: 2, overwrite: 0 });
    });

    it('id 重疊時正確計算覆蓋數', () => {
        expect(summarizeImport(['a', 'b', 'c'], ['b', 'c', 'd'])).toEqual({ total: 3, fresh: 1, overwrite: 2 });
    });

    it('空輸入歸零', () => {
        expect(summarizeImport([], ['a'])).toEqual({ total: 0, fresh: 0, overwrite: 0 });
    });

    it('全部覆蓋', () => {
        expect(summarizeImport(['a', 'b'], ['a', 'b'])).toEqual({ total: 2, fresh: 0, overwrite: 2 });
    });
});
