/**
 * ADR-0292：最近執行清單——新的在前；無痕只顯示時間、事件與結果；耗時經翻譯鍵顯示。
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import type { HookRun } from '@/lib/types';
import { HookRunsList } from '../HookRunsList';

vi.mock('react-i18next', () => ({
    useTranslation: () => ({
        t: (key: string, values?: Record<string, unknown>) => (values ? `${key}:${JSON.stringify(values)}` : key),
    }),
}));

const run = (extra: Partial<HookRun>): HookRun => ({
    at: 1_760_000_000, event: 'PreToolUse', hook: 'guard', hook_type: 'command', status: 'ok',
    outcome: 'blocked', duration_ms: 42, exit_code: 2, http_status: null, detail: 'no tests on Fridays',
    incognito: false, ...extra,
});

describe('HookRunsList（ADR-0292）', () => {
    it('顯示 hook 名稱、結果、耗時與說明', () => {
        render(<HookRunsList runs={[run({})]} onRefresh={vi.fn()} />);
        expect(screen.getByText('guard')).toBeInTheDocument();
        expect(screen.getByText('settings.hooks_tab.outcomes.blocked')).toBeInTheDocument();
        expect(screen.getByText('settings.hooks_tab.runs.duration:{"ms":42}')).toBeInTheDocument();
        expect(screen.getByText('no tests on Fridays')).toBeInTheDocument();
    });

    it('無痕的紀錄不顯示 hook 名稱與說明', () => {
        render(<HookRunsList runs={[run({ incognito: true, hook: '', detail: null })]} onRefresh={vi.fn()} />);
        expect(screen.getByText('settings.hooks_tab.runs.incognito')).toBeInTheDocument();
        expect(screen.queryByText('guard')).not.toBeInTheDocument();
    });

    it('沒有紀錄時顯示空狀態；按重新整理會呼叫 onRefresh', () => {
        const onRefresh = vi.fn();
        render(<HookRunsList runs={[]} onRefresh={onRefresh} />);
        expect(screen.getByText('settings.hooks_tab.runs.empty')).toBeInTheDocument();
        fireEvent.click(screen.getByText('settings.hooks_tab.runs.refresh'));
        expect(onRefresh).toHaveBeenCalledTimes(1);
    });
});
