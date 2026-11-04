/**
 * ADR-0292：比對規則的提示看事件——Notification 比的是通知類型，工具事件比的是工具名稱
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { HookMatcherGroup } from '@/lib/types';
import { HookEventSection } from '../HookEventSection';

const groups: HookMatcherGroup[] = [
    {
        matcher: '',
        hooks: [{ type: 'http', url: 'https://example.test/', enabled: true, timeout: 30, headers: {} }],
    },
];

function matcherPlaceholder(event: string): string | null {
    render(<HookEventSection event={event} usesMatcher groups={groups} onChange={vi.fn()} />);
    return screen.getByLabelText('比對規則').getAttribute('placeholder');
}

describe('HookEventSection（ADR-0292）', () => {
    it('Notification 的提示列出通知類型', () => {
        const placeholder = matcherPlaceholder('Notification');
        expect(placeholder).toContain('permission_prompt');
        expect(placeholder).not.toContain('工具名稱');
    });

    it.each(['PreToolUse', 'PostToolUseFailure', 'PermissionRequest', 'PermissionDenied'])(
        '%s 的提示說要寫工具名稱',
        (event) => {
            expect(matcherPlaceholder(event)).toContain('工具名稱');
        },
    );

    it.each(['PostToolUseFailure', 'PermissionRequest', 'PermissionDenied', 'Notification'])(
        '%s 有事件說明，不是翻譯鍵',
        (event) => {
            render(<HookEventSection event={event} usesMatcher={false} groups={[]} onChange={vi.fn()} />);
            expect(screen.queryByText(`settings.hooks_tab.events.${event}`)).not.toBeInTheDocument();
        },
    );

    it('不看比對規則的事件沒有比對欄位', () => {
        render(<HookEventSection event="Stop" usesMatcher={false} groups={groups} onChange={vi.fn()} />);
        expect(screen.queryByLabelText('比對規則')).not.toBeInTheDocument();
    });
});
