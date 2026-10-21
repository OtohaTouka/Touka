/**
 * Pure in-memory store for terminal output buffers.
 * Keeps the last `maxLength` characters per named terminal.
 * No VS Code API dependency — injectable and testable in isolation.
 *
 * Singleton `terminalOutputStore` is shared between TerminalHandlers (writer)
 * and ChatViewProvider (reader) so @terminal mentions show the last real output.
 */
export class TerminalOutputStore {
    private readonly buffers = new Map<string, string>();
    private lastWrittenTerminal = '';
    private readonly maxLength: number;

    constructor(maxLength: number = 4096) {
        this.maxLength = maxLength;
    }

    append(terminalName: string, data: string): void {
        const current = this.buffers.get(terminalName) ?? '';
        let next = current + data;
        if (next.length > this.maxLength) {
            next = next.slice(next.length - this.maxLength);
        }
        this.buffers.set(terminalName, next);
        this.lastWrittenTerminal = terminalName;
    }

    getOutput(terminalName: string): string {
        return this.buffers.get(terminalName) ?? '';
    }

    getLatestOutput(): string {
        if (!this.lastWrittenTerminal) { return ''; }
        return this.buffers.get(this.lastWrittenTerminal) ?? '';
    }

    clear(terminalName: string): void {
        this.buffers.set(terminalName, '');
    }
}


export const terminalOutputStore = new TerminalOutputStore(4096);
