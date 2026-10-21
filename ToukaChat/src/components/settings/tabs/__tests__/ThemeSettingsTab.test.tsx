import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ThemeSettingsTab } from '../ThemeSettingsTab';
import { ThemeProvider } from '@/lib/ThemeContext';

// next-themes（mode 軸）以 mock 提供 theme/setTheme（ADR-0105）。
const { mockSetTheme, modeRef } = vi.hoisted(() => ({
  mockSetTheme: vi.fn(),
  modeRef: { current: 'dark' as string },
}));
vi.mock('next-themes', () => ({
  useTheme: () => ({ theme: modeRef.current, resolvedTheme: modeRef.current, setTheme: mockSetTheme }),
}));

// Mock ResizeObserver since it's not in JSDOM
class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
window.ResizeObserver = ResizeObserver;

// Mock scrollIntoView since it's not in JSDOM
window.HTMLElement.prototype.scrollIntoView = vi.fn();

describe('ThemeSettingsTab', () => {
    // 外觀偏好存共享 localStorage；每測清空避免跨測污染（shuffle 安全）。
    beforeEach(() => {
        window.localStorage.clear();
        document.documentElement.className = '';
    });

    it('should render theme selection buttons and font slider', () => {
        render(
            <ThemeProvider>
                <ThemeSettingsTab />
            </ThemeProvider>
        );

        // Check for theme buttons
        expect(screen.getByText(/介面主題選擇/i)).toBeDefined();
        
        // Check for font size slider label
        expect(screen.getByText(/文字顯示大小/i)).toBeDefined();
    });

    it('should render the live preview showcase with agent labels', () => {
        render(
            <ThemeProvider>
                <ThemeSettingsTab />
            </ThemeProvider>
        );

        expect(screen.getByText(/即時預覽/)).toBeDefined();
        expect(screen.getAllByText(/AgentAlpha/)[0]).toBeDefined();
        expect(screen.getAllByText(/AgentBeta/)[0]).toBeDefined();
        expect(screen.getAllByText(/DebugAgent/)[0]).toBeDefined();
    });

    it('字級預覽範例文字綁定 --chat-font-size（P2）', () => {
        render(
            <ThemeProvider>
                <ThemeSettingsTab />
            </ThemeProvider>
        );
        const sample = screen.getByText(/拉動字級滑桿/);
        expect(sample.getAttribute('style')).toContain('var(--chat-font-size)');
    });

    it('模式分段控件：亮/暗/跟隨系統 三鈕，點擊呼叫 next-themes setTheme（ADR-0105）', () => {
        modeRef.current = 'dark';
        mockSetTheme.mockClear();
        render(
            <ThemeProvider>
                <ThemeSettingsTab />
            </ThemeProvider>
        );
        // 區段標題
        expect(screen.getByText('外觀模式')).toBeDefined();
        // 三個模式按鈕（i18n 文案）
        const sys = screen.getByRole('button', { name: /跟隨系統/ });
        const light = screen.getByRole('button', { name: /淺色/ });
        const dark = screen.getByRole('button', { name: /深色/ });
        fireEvent.click(light);
        expect(mockSetTheme).toHaveBeenCalledWith('light');
        fireEvent.click(sys);
        expect(mockSetTheme).toHaveBeenCalledWith('system');
        fireEvent.click(dark);
        expect(mockSetTheme).toHaveBeenCalledWith('dark');
    });

    it('字級端點標籤與展示區字串走 i18n（P4）', () => {
        render(
            <ThemeProvider>
                <ThemeSettingsTab />
            </ThemeProvider>
        );
        // Small / Large 端點改顯中文
        expect(screen.getByText('小')).toBeDefined();
        expect(screen.getByText('大')).toBeDefined();
        // 展示區標題在地化（不再寫死英文 "Component Showcase"）
        expect(screen.getByText('元件展示')).toBeDefined();
        expect(screen.queryByText('Component Showcase')).toBeNull();
    });

    it('外觀打磨包：時間/密度/泡泡/減少動態 四區段標題渲染', () => {
        render(<ThemeProvider><ThemeSettingsTab /></ThemeProvider>);
        expect(screen.getByText('時間格式')).toBeDefined();
        expect(screen.getByText('介面密度')).toBeDefined();
        expect(screen.getByText('對話泡泡樣式')).toBeDefined();
        expect(screen.getByText('減少動態效果')).toBeDefined();
    });

    it('點「緊湊」→ 寫入 touka_ui_density 並 aria-pressed', () => {
        render(<ThemeProvider><ThemeSettingsTab /></ThemeProvider>);
        const btn = screen.getByRole('button', { name: '緊湊' });
        fireEvent.click(btn);
        expect(window.localStorage.getItem('touka_ui_density')).toBe('compact');
        expect(btn.getAttribute('aria-pressed')).toBe('true');
    });

    it('點「24 小時制」→ 寫入 touka_time_format', () => {
        render(<ThemeProvider><ThemeSettingsTab /></ThemeProvider>);
        fireEvent.click(screen.getByRole('button', { name: '24 小時制' }));
        expect(window.localStorage.getItem('touka_time_format')).toBe('24h');
    });

    it('點「方角」→ 寫入 touka_bubble_style', () => {
        render(<ThemeProvider><ThemeSettingsTab /></ThemeProvider>);
        fireEvent.click(screen.getByRole('button', { name: '方角' }));
        expect(window.localStorage.getItem('touka_bubble_style')).toBe('square');
    });

    it('點減少動態「開啟」→ 寫入 touka_reduce_motion 並掛 html class', () => {
        render(<ThemeProvider><ThemeSettingsTab /></ThemeProvider>);
        fireEvent.click(screen.getByRole('button', { name: '開啟' }));
        expect(window.localStorage.getItem('touka_reduce_motion')).toBe('on');
        expect(document.documentElement.classList.contains('motion-reduce')).toBe(true);
    });

    it('泡泡預覽即時反映密度與樣式（緊湊→px-3、方角→rounded-md）', () => {
        render(<ThemeProvider><ThemeSettingsTab /></ThemeProvider>);
        const preview = screen.getByTestId('bubble-preview');
        const userBubble = () => preview.firstElementChild as HTMLElement;
        // 預設舒適圓角
        expect(userBubble().className).toContain('px-4');
        expect(userBubble().className).toContain('rounded-2xl');
        // 切緊湊 → 內距變 px-3
        fireEvent.click(screen.getByRole('button', { name: '緊湊' }));
        expect(userBubble().className).toContain('px-3');
        // 切方角 → rounded-md、無 rounded-tr-none
        fireEvent.click(screen.getByRole('button', { name: '方角' }));
        expect(userBubble().className).toContain('rounded-md');
        expect(userBubble().className).not.toContain('rounded-tr-none');
    });
});
