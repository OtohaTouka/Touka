import type { CommandHook, HookHandler, HookMatcherGroup, HooksConfig, HttpHook } from '@/lib/types';

/** 和後端 DEFAULT_HOOK_TIMEOUT_SECONDS 相同（秒）。 */
const DEFAULT_HOOK_TIMEOUT_S = 30;
export const MAX_HOOK_TIMEOUT_S = 300;

export function newHook(type: HookHandler['type']): HookHandler {
    const base = { enabled: true, timeout: DEFAULT_HOOK_TIMEOUT_S };
    if (type === 'command') return { ...base, type, command: '', args: [] } satisfies CommandHook;
    return { ...base, type, url: '', headers: {} } satisfies HttpHook;
}

export function newGroup(type: HookHandler['type']): HookMatcherGroup {
    return { matcher: '', hooks: [newHook(type)] };
}

/** 參數一行一個、原樣交給程式（不經 shell）；只略過空行。 */
export function argsToText(args: string[] | undefined): string {
    return (args ?? []).join('\n');
}

export function textToArgs(text: string): string[] {
    return text.split('\n').map((line) => line.replace(/\r$/, '')).filter((line) => line !== '');
}

/** 標頭一行一個「名稱: 值」，照字面送出。 */
export function headersToText(headers: Record<string, string> | undefined): string {
    return Object.entries(headers ?? {}).map(([name, value]) => `${name}: ${value}`).join('\n');
}

export function textToHeaders(text: string): Record<string, string> {
    const headers: Record<string, string> = {};
    for (const raw of text.split('\n')) {
        const line = raw.replace(/\r$/, '');
        if (!line.trim()) continue;
        const colon = line.indexOf(':');
        const name = (colon < 0 ? line : line.slice(0, colon)).trim();
        headers[name] = colon < 0 ? '' : line.slice(colon + 1).trim();
    }
    return headers;
}

/** 換掉一個事件的設定；改成空清單時把事件拿掉。 */
export function withGroups(config: HooksConfig, event: string, groups: HookMatcherGroup[]): HooksConfig {
    const hooks = { ...(config.hooks ?? {}) };
    if (groups.length > 0) hooks[event] = groups;
    else delete hooks[event];
    return { ...config, hooks };
}

export function replaceAt<T>(items: T[], index: number, item: T): T[] {
    return items.map((current, i) => (i === index ? item : current));
}

export function removeAt<T>(items: T[], index: number): T[] {
    return items.filter((_, i) => i !== index);
}
