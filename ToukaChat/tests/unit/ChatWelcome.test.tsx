/**
 * HAX G1（講清楚系統能做什麼）— 首屏能力揭露＋可點擊範例提示。
 * ChatWelcome 為純呈現元件（無 provider 依賴），可獨立驗證。
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ChatWelcome, WELCOME_PROMPT_KEYS } from '@/components/chat/ChatWelcome';
import i18n from '@/lib/i18n';

// i18n 後 ChatWelcome 渲染 t(key)（固定 zh-TW）。解析為來源中文供斷言比對按鈕名稱。
const WELCOME_PROMPTS = WELCOME_PROMPT_KEYS.map((k) => i18n.t(k));

describe('ChatWelcome（HAX G1 首屏能力揭露）', () => {
  it('至少提供 3 則範例提示，且全部以按鈕渲染', () => {
    render(<ChatWelcome isIDE={false} onPickPrompt={vi.fn()} />);
    expect(WELCOME_PROMPTS.length).toBeGreaterThanOrEqual(3);
    for (const prompt of WELCOME_PROMPTS) {
      expect(screen.getByRole('button', { name: prompt })).toBeInTheDocument();
    }
  });

  it('渲染能力說明文字（讓新使用者知道系統能做什麼）', () => {
    render(<ChatWelcome isIDE={false} onPickPrompt={vi.fn()} />);
    // 能力說明應提到「記住」（跨對話記憶＝ Touka 招牌能力）
    expect(screen.getByText(/記住/)).toBeInTheDocument();
  });

  it('點擊範例提示會以該提示文字回呼 onPickPrompt', () => {
    const onPick = vi.fn();
    render(<ChatWelcome isIDE={false} onPickPrompt={onPick} />);
    fireEvent.click(screen.getByRole('button', { name: WELCOME_PROMPTS[0] }));
    expect(onPick).toHaveBeenCalledWith(WELCOME_PROMPTS[0]);
  });
});
