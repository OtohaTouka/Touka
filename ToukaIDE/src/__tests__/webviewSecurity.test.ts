import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { runInNewContext } from 'node:vm';

import { describe, expect, it } from 'vitest';

type EventListener = (event: Record<string, unknown>) => void;

class FakeClassList {
    public constructor(private readonly element: FakeElement) {}

    public add(...tokens: string[]): void {
        const classes = new Set(this.element.className.split(/\s+/).filter(Boolean));
        tokens.forEach((token) => classes.add(token));
        this.element.className = Array.from(classes).join(' ');
    }

    public remove(...tokens: string[]): void {
        const removed = new Set(tokens);
        this.element.className = this.element.className
            .split(/\s+/)
            .filter((token) => token && !removed.has(token))
            .join(' ');
    }

    public contains(token: string): boolean {
        return this.element.className.split(/\s+/).includes(token);
    }
}

class FakeElement {
    public readonly children: FakeElement[] = [];
    public readonly classList = new FakeClassList(this);
    public readonly dataset: Record<string, string> = {};
    public readonly style: Record<string, string> = {};
    public value = '';
    public selectionStart = 0;
    public scrollHeight = 0;
    public scrollTop = 0;
    private readonly listeners = new Map<string, EventListener[]>();
    private classNameValue = '';
    private idValue = '';
    private textContentValue = '';

    public constructor(
        public readonly tagName: string,
        private readonly document: FakeDocument,
    ) {}

    public get className(): string {
        return this.classNameValue;
    }

    public set className(value: string) {
        this.classNameValue = String(value);
    }

    public get id(): string {
        return this.idValue;
    }

    public set id(value: string) {
        this.idValue = String(value);
        this.document.registerId(this);
    }

    public get innerHTML(): string {
        return '';
    }

    public set innerHTML(value: string) {
        this.document.innerHtmlWrites.push({ element: this, value: String(value) });
        this.children.splice(0);
        this.textContentValue = '';
    }

    public get innerText(): string {
        return this.textContent;
    }

    public set innerText(value: string) {
        this.textContent = value;
    }

    public get options(): FakeElement[] {
        return this.children.filter((child) => child.tagName === 'OPTION');
    }

    public get textContent(): string {
        return this.textContentValue + this.children.map((child) => child.textContent).join('');
    }

    public set textContent(value: string) {
        this.children.splice(0);
        this.textContentValue = String(value);
    }

    public addEventListener(type: string, listener: EventListener): void {
        const listeners = this.listeners.get(type) ?? [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    public appendChild<T extends FakeElement>(child: T): T {
        this.children.push(child);
        if (this.tagName === 'SELECT' && this.children.length === 1) {
            this.value = child.value;
        }
        return child;
    }

    public click(): void {
        this.dispatch('click');
    }

    public dispatch(type: string, event: Record<string, unknown> = {}): void {
        const payload = { target: this, ...event };
        for (const listener of this.listeners.get(type) ?? []) {
            listener(payload);
        }
    }

    public focus(): void {}

    public querySelector(selector: string): FakeElement | null {
        return this.querySelectorAll(selector)[0] ?? null;
    }

    public querySelectorAll(selector: string): FakeElement[] {
        const descendants = this.children.flatMap((child) => [child, ...child.descendants()]);
        return descendants.filter((element) => element.matches(selector));
    }

    public replaceChildren(...children: FakeElement[]): void {
        this.children.splice(0);
        this.textContentValue = '';
        children.forEach((child) => this.appendChild(child));
    }

    public setSelectionRange(start: number): void {
        this.selectionStart = start;
    }

    private descendants(): FakeElement[] {
        return this.children.flatMap((child) => [child, ...child.descendants()]);
    }

    private matches(selector: string): boolean {
        if (!selector.startsWith('.')) {
            return this.tagName.toLowerCase() === selector.toLowerCase();
        }
        const requiredClasses = selector.split('.').filter(Boolean);
        return requiredClasses.every((token) => this.classList.contains(token));
    }
}

class FakeDocument {
    public readonly innerHtmlWrites: Array<{ element: FakeElement; value: string }> = [];
    private readonly elements = new Set<FakeElement>();
    private readonly elementsById = new Map<string, FakeElement>();

    public constructor() {
        this.createRoot('select', 'agent-select');
        this.createRoot('select', 'session-select');
        this.createRoot('div', 'chat-container');
        this.createRoot('textarea', 'message-input');
        this.createRoot('button', 'send-button');
        this.createRoot('div', 'protocol-status');
        this.createRoot('div', 'mention-menu');
    }

    public createElement(tagName: string): FakeElement {
        const element = new FakeElement(tagName.toUpperCase(), this);
        this.elements.add(element);
        return element;
    }

    public getElementById(id: string): FakeElement {
        const existing = this.elementsById.get(id);
        if (existing) {
            return existing;
        }
        const fallback = this.createElement('div');
        fallback.id = id;
        return fallback;
    }

    public querySelectorAll(selector: string): FakeElement[] {
        return Array.from(this.elements).filter((element) => element['matches'](selector));
    }

    public registerId(element: FakeElement): void {
        if (element.id) {
            this.elementsById.set(element.id, element);
        }
    }

    public firstByClass(className: string): FakeElement {
        const element = Array.from(this.elements).find((candidate) =>
            candidate.classList.contains(className),
        );
        if (!element) {
            throw new Error(`Missing element with class ${className}`);
        }
        return element;
    }

    private createRoot(tagName: string, id: string): void {
        const element = this.createElement(tagName);
        element.id = id;
    }
}

class FakeWindow {
    private readonly listeners = new Map<string, EventListener[]>();

    public addEventListener(type: string, listener: EventListener): void {
        const listeners = this.listeners.get(type) ?? [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    public dispatchMessage(data: unknown): void {
        for (const listener of this.listeners.get('message') ?? []) {
            listener({ data });
        }
    }
}

interface WebviewHarness {
    document: FakeDocument;
    window: FakeWindow;
}

const WEBVIEW_ROOT = resolve(process.cwd(), 'src', 'webview');
const MAIN_SOURCE = readFileSync(resolve(WEBVIEW_ROOT, 'main.js'), 'utf8');
const INDEX_SOURCE = readFileSync(resolve(WEBVIEW_ROOT, 'index.html'), 'utf8');
const STYLE_SOURCE = readFileSync(resolve(WEBVIEW_ROOT, 'style.css'), 'utf8');
const HOSTILE_TEXT = '<img src=x onerror="globalThis.pwned=true"><script>pwned()</script>**bold**\nsecond line';

function createHarness(): WebviewHarness {
    const document = new FakeDocument();
    const window = new FakeWindow();

    runInNewContext(
        MAIN_SOURCE,
        {
            acquireVsCodeApi: () => ({ postMessage: () => undefined }),
            console,
            document,
            window,
        },
        { filename: 'src/webview/main.js' },
    );

    return { document, window };
}

describe('ToukaIDE Webview plain-text security boundary', () => {
    it('renders hostile history content literally and normalizes the message role', () => {
        const harness = createHarness();

        harness.window.dispatchMessage({
            type: 'session_history',
            messages: [{ role: 'assistant attacker-class', content: HOSTILE_TEXT }],
        });

        const message = harness.document.firstByClass('message');
        expect(message.textContent).toBe(HOSTILE_TEXT);
        expect(message.className).toBe('message assistant');
        expect(harness.document.innerHtmlWrites).toEqual([]);
    });

    it('accumulates streamed hostile content as literal text', () => {
        const harness = createHarness();
        const splitAt = HOSTILE_TEXT.indexOf('<script>');
        const firstChunk = HOSTILE_TEXT.slice(0, splitAt);
        const secondChunk = HOSTILE_TEXT.slice(splitAt);

        harness.window.dispatchMessage({ type: 'stream', content: firstChunk });
        harness.window.dispatchMessage({ type: 'stream', content: secondChunk });

        const message = harness.document.firstByClass('message');
        expect(message.textContent).toBe(HOSTILE_TEXT);
        expect(harness.document.innerHtmlWrites).toEqual([]);
    });

    it('renders hostile plan titles and steps as literal text', () => {
        const harness = createHarness();

        harness.window.dispatchMessage({
            type: 'plan_created',
            title: HOSTILE_TEXT,
            steps: [HOSTILE_TEXT],
        });

        const plan = harness.document.firstByClass('plan-card');
        expect(plan.textContent).toContain(HOSTILE_TEXT);
        expect(harness.document.innerHtmlWrites).toEqual([]);
    });

    it('renders hostile agent and workspace mention labels as literal text', () => {
        const harness = createHarness();
        const input = harness.document.getElementById('message-input');

        harness.window.dispatchMessage({
            type: 'agents_data',
            agents: [{ id: 'agent-id', name: HOSTILE_TEXT }],
        });
        harness.window.dispatchMessage({ type: 'workspace_files', files: [HOSTILE_TEXT] });
        input.value = '@';
        input.selectionStart = 1;
        input.dispatch('input');

        const mentionMenu = harness.document.getElementById('mention-menu');
        expect(mentionMenu.textContent).toContain(HOSTILE_TEXT);
        expect(harness.document.innerHtmlWrites).toEqual([]);
    });

    it('ships only local scripts and forbids HTML rendering sinks', () => {
        expect(INDEX_SOURCE).not.toMatch(/https?:\/\//i);
        expect(INDEX_SOURCE).toMatch(
            /content="default-src 'none'; style-src \$\{cspSource\}; script-src \$\{cspSource\}; connect-src 'none';"/,
        );
        expect(INDEX_SOURCE).not.toMatch(/\sstyle\s*=/i);
        expect(MAIN_SOURCE).not.toMatch(/\bmarked\b/);
        expect(MAIN_SOURCE).not.toMatch(/\b(?:innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b/);
        expect(MAIN_SOURCE).not.toMatch(/(?:\.style\b|\.cssText\b|setAttribute\(\s*['"]style['"])/);
        expect(STYLE_SOURCE).toMatch(/\.message\s*\{[^}]*white-space:\s*pre-wrap;/s);
        expect(STYLE_SOURCE).toMatch(
            /\.system-notice,\s*\.plan-title,\s*\.step-text,\s*\.step-details,\s*\.mention-item \.name,\s*\.mention-item \.type-label\s*\{[^}]*white-space:\s*pre-wrap;/s,
        );
    });
});
