/**
 * Least-Recently-Used (LRU) cache backed by a JavaScript Map.
 *
 * JavaScript's Map preserves insertion order. LRU is implemented by
 * deleting and re-inserting the key on every access, so the tail of
 * the Map is always the most-recently-used entry and the head is the
 * least-recently-used one to evict.
 */
export class CompletionCache {
    private readonly cache = new Map<string, string>();
    private readonly maxItems: number;

    constructor(maxItems: number = 20) {
        this.maxItems = maxItems;
    }

    get(key: string): string | undefined {
        const value = this.cache.get(key);
        if (value !== undefined) {
            // Promote to MRU position
            this.cache.delete(key);
            this.cache.set(key, value);
        }
        return value;
    }

    set(key: string, value: string): void {
        if (this.cache.has(key)) {
            // Update existing: delete first so re-insert goes to MRU position
            this.cache.delete(key);
        } else if (this.cache.size >= this.maxItems) {
            // Evict LRU (first entry in Map)
            const lruKey = this.cache.keys().next().value as string;
            this.cache.delete(lruKey);
        }
        this.cache.set(key, value);
    }

    size(): number {
        return this.cache.size;
    }
}
