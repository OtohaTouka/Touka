import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { ConnectionItem } from '../ConnectionItem';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const conn: any = { id: 'conn-1', name: 'Alpha', url: 'http://alpha', enabled: true, protocol: 'openai' };
const base = { isActive: false, onToggle: vi.fn(), onRemove: vi.fn(), onEdit: vi.fn() };

describe('ConnectionItem 健康狀態（以 connection id 為 key）', () => {
    it('狀態為 connection_error 時顯示在地化錯誤標籤', () => {
        render(<ConnectionItem conn={conn} connectionStatus={{ 'conn-1': 'connection_error' }} {...base} />);
        expect(screen.getByText('連線錯誤')).toBeDefined();
    });

    it('狀態為 ok 時不顯示錯誤標籤', () => {
        render(<ConnectionItem conn={conn} connectionStatus={{ 'conn-1': 'ok' }} {...base} />);
        expect(screen.queryByText('連線錯誤')).toBeNull();
    });

    it('狀態以舊 name 為 key 時不再誤判（必須以 id 對應後端）', () => {
        render(<ConnectionItem conn={conn} connectionStatus={{ Alpha: 'connection_error' }} {...base} />);
        expect(screen.queryByText('連線錯誤')).toBeNull();
    });
});

describe('ConnectionItem 使用中標記與測試鈕（P2）', () => {
    it('isActive 時顯示「使用中」徽章；否則不顯示', () => {
        const { rerender } = render(<ConnectionItem conn={conn} connectionStatus={{}} {...base} isActive={true} />);
        expect(screen.getByText('使用中')).toBeDefined();
        rerender(<ConnectionItem conn={conn} connectionStatus={{}} {...base} isActive={false} />);
        expect(screen.queryByText('使用中')).toBeNull();
    });

    it('提供 onTest 且連線啟用時，點測試鈕呼叫 onTest(conn.id)', async () => {
        const onTest = vi.fn().mockResolvedValue(undefined);
        render(<ConnectionItem conn={conn} connectionStatus={{}} {...base} onTest={onTest} />);
        const testBtn = screen.getByRole('button', { name: /測試連線/i });
        fireEvent.click(testBtn);
        expect(onTest).toHaveBeenCalledWith('conn-1');
    });

    it('連線停用時不顯示測試鈕', () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const disabled: any = { ...conn, enabled: false };
        render(<ConnectionItem conn={disabled} connectionStatus={{}} {...base} onTest={vi.fn()} />);
        expect(screen.queryByRole('button', { name: /測試連線/i })).toBeNull();
    });
});
