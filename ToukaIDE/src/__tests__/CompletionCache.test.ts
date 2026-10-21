import { describe, it, expect } from 'vitest';
import { CompletionCache } from '../utils/CompletionCache';

describe('CompletionCache', () => {
    it('stores and retrieves values', () => {
        const cache = new CompletionCache(5);
        cache.set('a', 'value-a');
        expect(cache.get('a')).toBe('value-a');
    });

    it('returns undefined for missing keys', () => {
        const cache = new CompletionCache(5);
        expect(cache.get('missing')).toBeUndefined();
    });

    it('evicts the least-recently-used entry (true LRU, not FIFO)', () => {
        const cache = new CompletionCache(3);
        cache.set('a', '1');
        cache.set('b', '2');
        cache.set('c', '3');

        // Access 'a' to make it recently used
        cache.get('a');

        // Adding 'd' should evict 'b' (oldest unaccessed), not 'a'
        cache.set('d', '4');

        expect(cache.get('a')).toBe('1');  // still present (recently accessed)
        expect(cache.get('b')).toBeUndefined();  // evicted (LRU)
        expect(cache.get('c')).toBe('3');
        expect(cache.get('d')).toBe('4');
    });

    it('updating an existing key moves it to most-recently-used position', () => {
        const cache = new CompletionCache(3);
        cache.set('a', '1');
        cache.set('b', '2');
        cache.set('c', '3');

        // Update 'a' — should become most recently used
        cache.set('a', 'updated');

        // Adding 'd' should evict 'b', not 'a'
        cache.set('d', '4');

        expect(cache.get('a')).toBe('updated');
        expect(cache.get('b')).toBeUndefined();
    });

    it('does not exceed maxItems', () => {
        const cache = new CompletionCache(3);
        cache.set('a', '1');
        cache.set('b', '2');
        cache.set('c', '3');
        cache.set('d', '4');  // should evict 'a'

        expect(cache.size()).toBe(3);
    });
});
