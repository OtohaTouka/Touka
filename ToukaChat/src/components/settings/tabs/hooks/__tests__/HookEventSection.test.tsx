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

    it.each([
        ['PostToolUseFailure', '工具開始執行之後失敗時'],
        ['PermissionRequest', '審批卡跳出之前'],
        ['PermissionDenied', '工具呼叫被拒時'],
        ['Notification', '跳審批卡、代理人問問題'],
        ['StopFailure', '一輪回覆因錯誤結束時'],
        ['SubagentStart', '子代理開始前'],
        ['SubagentStop', '子代理結束後'],
        ['PreCompact', '滑動視窗剪裁前'],
        ['PostCompact', '滑動視窗剪裁存好後'],
        ['TaskCreated', '任務看板建立時'],
        ['TaskCompleted', '任務變成完成時'],
        ['Elicitation', 'MCP 工具用表單向人要資料'],
        ['ConfigChange', '設定存好之後'],
        ['HeartbeatStart', '心跳開始時'],
        ['HeartbeatStop', '心跳結束時'],
    ])('%s 的事件說明看得到', (event, text) => {
        render(<HookEventSection event={event} usesMatcher={false} groups={[]} onChange={vi.fn()} />);
        expect(screen.getByText(text, { exact: false })).toBeInTheDocument();
    });

    it.each([
        ['StopFailure', 'rate_limit'],
        ['SubagentStart', 'subagent'],
        ['PreCompact', 'auto'],
        ['Elicitation', 'MCP 伺服器名稱'],
        ['ConfigChange', 'autonomy'],
    ])('%s 的提示列出它比對的東西，不是工具名稱', (event, text) => {
        const placeholder = matcherPlaceholder(event);
        expect(placeholder).toContain(text);
        expect(placeholder).not.toContain('工具名稱');
    });

    it('不看比對規則的事件沒有比對欄位', () => {
        render(<HookEventSection event="Stop" usesMatcher={false} groups={groups} onChange={vi.fn()} />);
        expect(screen.queryByLabelText('比對規則')).not.toBeInTheDocument();
    });
});
