import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import '@/lib/i18n';
import { McpServerItem } from '../McpServerItem';
import type { McpServerStatus } from '@/lib/types';
import { useIsolationReasonText } from '@/hooks/useIsolationReasonText';

vi.mock('@/hooks/useIsolationReasonText', async importOriginal => {
  const actual = await importOriginal<typeof import('@/hooks/useIsolationReasonText')>();
  return { ...actual, useIsolationReasonText: vi.fn(actual.useIsolationReasonText) };
});

const base = { onToggle: vi.fn(), onEdit: vi.fn(), onDelete: vi.fn(), onRestart: vi.fn(), onManageTools: vi.fn() };

describe('McpServerItem 隔離狀態', () => {
  const isolation: NonNullable<McpServerStatus['isolation']> = {
    class: 'third_party',
    origin: 'manual',
    record_state: 'ok',
    state: 'waiting_probe',
    level: 'none',
    features: [],
    reason: 'probe_pending',
  };
  const mount = (change: Partial<typeof isolation> = {}, enabled = true) =>
    render(
      <McpServerItem
        serverKey="files"
        serverCfg={{ command: 'x', enabled }}
        isRunning={{
          name: 'files',
          status: 'not_started',
          tools_count: 0,
          url: '',
          isolation: { ...isolation, ...change },
        }}
        {...base}
      />,
    );

  it('顯示等待自測，不誤稱 MCP 故障或承諾十分鐘後啟動', () => {
    mount();
    expect(screen.getByText('等待隔離')).toBeInTheDocument();
    expect(screen.getByText(/正在檢查隔離環境/)).toBeInTheDocument();
    expect(screen.queryByText(/10 分鐘/)).toBeNull();
  });

  it.each(['selftest_failed', 'selftest_error', 'fs_9p'])(
    '暫停 %s 說明自測與定期恢復',
    (reason) => {
      mount({ state: 'suspended', reason });
      expect(
        screen.getByText('已暫停：隔離自測失敗（不是 MCP 壞了）'),
      ).toBeInTheDocument();
      expect(
        screen.getByText(/約每 10 分鐘重新自測.*通過後自動啟動/),
      ).toBeInTheDocument();
    },
  );

  it.each([
    'sandbox_state_unconfigured',
    'kernel_unreadable',
    'unknown_new_reason',
  ])('%s 不承諾十分鐘重試', (reason) => {
    mount({ state: 'suspended', reason });
    expect(screen.getByText(/檢查.*隔離/)).toBeInTheDocument();
    expect(screen.queryByText(/10 分鐘/)).toBeNull();
    expect(screen.queryByText(reason)).toBeNull();
  });

  it('已啟動但沒有隔離時說明平台原因', () => {
    mount({ state: 'running', reason: 'native_platform' });
    expect(screen.getByText('未隔離')).toBeInTheDocument();
    expect(screen.getByText(/這個環境沒有隔離功能/)).toBeInTheDocument();
  });

  it.each(['native_platform', 'selftest_failed', 'probe_pending'])(
    '自建等待 %s 時說明自動恢復與手動入口', reason => {
      mount({ class: 'self', state: 'waiting_isolation', reason });
      expect(screen.getByText(/隔離恢復後會自動啟動；也可以按「啟動…」確認這次的執行方式/)).toBeInTheDocument();
      expect(screen.queryByText(/請檢查並修正隔離環境/)).toBeNull();
    },
  );

  it('MCP 檢查失敗不誤稱會以未隔離方式執行', () => {
    mount({ state: 'suspended', reason: 'isolation_check_failed' });
    expect(screen.getByText(/隔離檢查失敗，這台伺服器目前不會啟動/)).toBeInTheDocument();
    expect(screen.queryByText(/這次當成未隔離處理/)).toBeNull();
  });

  it('MCP 專用原因優先，其他原因沿用共用 hook', () => {
    const reasonText = vi.fn(() => '共用隔離原因');
    vi.mocked(useIsolationReasonText).mockReturnValue(reasonText);
    try {
      const first = mount({ state: 'suspended', reason: 'kernel_unreadable' });
      expect(screen.getByText(/讀不到隔離程式/)).toBeInTheDocument();
      expect(reasonText).not.toHaveBeenCalled();
      first.unmount();
      mount({ state: 'suspended', reason: 'interpreter_untrusted' });
      expect(screen.getByText(/共用隔離原因/)).toBeInTheDocument();
      expect(reasonText).toHaveBeenCalledWith('interpreter_untrusted', expect.objectContaining({ reason: 'interpreter_untrusted' }));
    } finally {
      vi.mocked(useIsolationReasonText).mockRestore();
    }
  });

  it.each([
    { state: 'running' as const, level: 'landlock' as const },
    { state: 'not_started' as const },
    { class: 'remote' as const, state: 'running' as const },
  ])('正常隔離、未啟動與遠端不顯示未隔離徽章：%s', (change) => {
    mount(change);
    expect(screen.queryByText('未隔離')).toBeNull();
  });

  it('停用條目不顯示舊隔離狀態', () => {
    mount({ state: 'suspended' }, false);
    expect(screen.queryByText(/已暫停：/)).toBeNull();
  });
});

describe('McpServerItem remote 條目（ADR-0194）', () => {
  it('顯示端點 URL 而非 undefined', () => {
    const { container } = render(
      <McpServerItem
        serverKey="ms-learn"
        serverCfg={{ url: 'https://learn.microsoft.com/api/mcp', enabled: true }}
        isRunning={{ name: 'ms-learn', status: 'connected', tools_count: 3, url: '' }}
        {...base}
      />,
    );
    expect(screen.getByText('https://learn.microsoft.com/api/mcp')).toBeDefined();
    expect(container.textContent).not.toMatch(/undefined/);
  });
});

describe('McpServerItem 健康狀態與重啟', () => {
  it('將 stdio 認證錯誤碼顯示為原因及修正方式', () => {
    render(
      <McpServerItem
        serverKey="invalid-stdio"
        serverCfg={{ command: 'x', args: [], enabled: true }}
        isRunning={{ name: 'invalid-stdio', status: 'error', tools_count: 0, url: '', last_error: 'MCP_STDIO_PRINCIPAL_UNSUPPORTED' }}
        {...base}
      />,
    );
    expect(screen.getByText(/stdio 伺服器不支援個別使用者認證.*移除 auth_mode/)).toBeDefined();
    expect(screen.queryByText('MCP_STDIO_PRINCIPAL_UNSUPPORTED')).toBeNull();
  });

  it('connected → 顯示已連線狀態與工具數徽章', () => {
    render(
      <McpServerItem
        serverKey="memory"
        serverCfg={{ command: 'npx', args: [], enabled: true }}
        isRunning={{ name: 'memory', status: 'connected', tools_count: 4, url: '' }}
        {...base}
      />,
    );
    expect(screen.getByText('已連線')).toBeDefined();
    expect(screen.getByText(/4 個工具/)).toBeDefined();
  });

  it('error → 顯示脫敏錯誤訊息', () => {
    render(
      <McpServerItem
        serverKey="bad"
        serverCfg={{ command: 'x', args: [], enabled: true }}
        isRunning={{ name: 'bad', status: 'error', tools_count: 0, url: '', last_error: 'command not found' }}
        {...base}
      />,
    );
    expect(screen.getByText(/command not found/)).toBeDefined();
  });

  it('not_started（isRunning undefined、enabled）→ 顯示未啟動', () => {
    render(
      <McpServerItem
        serverKey="ns"
        serverCfg={{ command: 'x', args: [], enabled: true }}
        isRunning={undefined}
        {...base}
      />,
    );
    expect(screen.getByText('未啟動')).toBeDefined();
  });

  it('disabled（enabled:false）→ 已停用、重啟鈕停用', () => {
    render(
      <McpServerItem
        serverKey="off"
        serverCfg={{ command: 'x', args: [], enabled: false }}
        isRunning={undefined}
        {...base}
      />,
    );
    expect(screen.getByText('已停用')).toBeDefined();
    const btn = screen.getByRole('button', { name: /重啟 MCP 伺服器 off/ });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });

  it('isRestarting → 重啟鈕停用（防重複送出）', () => {
    const onRestart = vi.fn();
    render(
      <McpServerItem
        serverKey="memory"
        serverCfg={{ command: 'npx', args: [], enabled: true }}
        isRunning={{ name: 'memory', status: 'connected', tools_count: 1, url: '' }}
        {...base}
        onRestart={onRestart}
        isRestarting
      />,
    );
    const btn = screen.getByRole('button', { name: /重啟 MCP 伺服器 memory/ });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(btn);
    expect(onRestart).not.toHaveBeenCalled();
  });

  it('點重啟鈕 → 呼叫 onRestart(serverKey)', () => {
    const onRestart = vi.fn();
    render(
      <McpServerItem
        serverKey="memory"
        serverCfg={{ command: 'npx', args: [], enabled: true }}
        isRunning={{ name: 'memory', status: 'connected', tools_count: 1, url: '' }}
        {...base}
        onRestart={onRestart}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /重啟 MCP 伺服器 memory/ }));
    expect(onRestart).toHaveBeenCalledWith('memory');
  });

  it('subscription stale 與 cache counters 可見', () => {
    render(
      <McpServerItem
        serverKey="modern"
        serverCfg={{ command: 'mcp', args: [], enabled: true }}
        isRunning={{
          name: 'modern',
          status: 'connected',
          tools_count: 2,
          url: '',
          cache: {
            enabled: true,
            entries: 3,
            public_entries: 2,
            private_entries: 1,
            hits: 7,
            misses: 1,
            writes: 3,
            evictions: 0,
            max_entries: 1024,
          },
          subscription: {
            enabled: true,
            status: 'stale',
            stale: true,
            reconnect_attempts: 1,
            handle_count: 1,
          },
        }}
        {...base}
      />,
    );
    expect(screen.getByText('資料待更新')).toBeDefined();
    expect(screen.getByText(/MCP 快取 3 筆 · 命中 7 次/)).toBeDefined();
  });
});
