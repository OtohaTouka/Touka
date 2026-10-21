import { describe, it, expect } from 'vitest';
import { TerminalOutputStore } from '../utils/TerminalOutputStore';

describe('TerminalOutputStore', () => {
    it('appends output for a named terminal', () => {
        const store = new TerminalOutputStore(1000);
        store.append('myTerminal', 'hello ');
        store.append('myTerminal', 'world');
        expect(store.getOutput('myTerminal')).toBe('hello world');
    });

    it('returns empty string for unknown terminal', () => {
        const store = new TerminalOutputStore(1000);
        expect(store.getOutput('unknown')).toBe('');
    });

    it('truncates buffer to maxLength (keeps tail)', () => {
        const store = new TerminalOutputStore(10);
        store.append('t', 'hello world'); // 11 chars → truncated to last 10
        const output = store.getOutput('t');
        expect(output.length).toBeLessThanOrEqual(10);
        expect(output).toBe('ello world');
    });

    it('keeps separate output per terminal', () => {
        const store = new TerminalOutputStore(1000);
        store.append('t1', 'from t1');
        store.append('t2', 'from t2');
        expect(store.getOutput('t1')).toBe('from t1');
        expect(store.getOutput('t2')).toBe('from t2');
    });

    it('returns most recent output across all terminals when no name given', () => {
        const store = new TerminalOutputStore(1000);
        store.append('t1', 'old');
        store.append('t2', 'new');
        expect(store.getLatestOutput()).toBe('new');
    });

    it('clears output for a specific terminal', () => {
        const store = new TerminalOutputStore(1000);
        store.append('t1', 'some output');
        store.clear('t1');
        expect(store.getOutput('t1')).toBe('');
    });
});
