// src/lib/ThemeContext.tsx
"use client";

import React, { createContext, useContext, useState, useEffect } from 'react';
import { MotionConfig } from 'framer-motion';
import { useTheme as useNextTheme } from 'next-themes';
import { THEMES, LIGHT_THEMES } from './design-system/tokens';
import { setTimeFormat12h } from './time_utils';

export type ThemeColor = keyof typeof THEMES;
type ColorMode = 'light' | 'dark';

// 外觀打磨包偏好（純前端、存 localStorage、即時生效）。
export type TimeFormat = '12h' | '24h';
export type Density = 'comfortable' | 'compact';
export type BubbleStyle = 'rounded' | 'square';
export type ReduceMotion = 'system' | 'on' | 'off';

interface ThemeContextType {
  themeColor: ThemeColor;
  setThemeColor: (color: ThemeColor) => void;
  fontSize: number;
  setFontSize: (size: number) => void;
  timeFormat: TimeFormat;
  setTimeFormat: (value: TimeFormat) => void;
  density: Density;
  setDensity: (value: Density) => void;
  bubbleStyle: BubbleStyle;
  setBubbleStyle: (value: BubbleStyle) => void;
  reduceMotion: ReduceMotion;
  setReduceMotion: (value: ReduceMotion) => void;
}

// 預設值（外觀偏好皆有安全預設，setter 為 no-op）：讓唯讀消費者（如 MessageItem/ChatArea）
// 在無 Provider 情境（例如獨立單元測試）也能安全取用預設外觀，不致拋錯。正式 App 一律由
// 根層 ThemeProvider 提供真值。
const DEFAULT_THEME: ThemeContextType = {
  themeColor: 'touka', setThemeColor: () => {},
  fontSize: 14, setFontSize: () => {},
  timeFormat: '12h', setTimeFormat: () => {},
  density: 'comfortable', setDensity: () => {},
  bubbleStyle: 'rounded', setBubbleStyle: () => {},
  reduceMotion: 'system', setReduceMotion: () => {},
};

const ThemeContext = createContext<ThemeContextType>(DEFAULT_THEME);

/**
 * ThemeProvider
 * 負責運行時的主題切換邏輯。
 * 核心數值來自 src/lib/design-system/tokens.ts (SSOT)。
 */
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [themeColor, setThemeColorState] = useState<ThemeColor>(() => {
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem('touka_theme_color') as ThemeColor; // noqa: LOCALSTORAGE_SSOT — Theme SSOT 初始化，SSR 相容純字串存取
      return (saved && THEMES[saved]) ? saved : 'touka';
    }
    return 'touka';
  });

  const [fontSize, setFontSizeState] = useState<number>(() => {
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem('touka_font_size'); // noqa: LOCALSTORAGE_SSOT — Theme SSOT 初始化，SSR 相容純字串存取
      return saved ? parseInt(saved) : 14;
    }
    return 14;
  });

  const [timeFormat, setTimeFormatState] = useState<TimeFormat>(() => {
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem('touka_time_format'); // noqa: LOCALSTORAGE_SSOT — 外觀偏好初始化，SSR 相容純字串
      const value: TimeFormat = saved === '24h' ? '24h' : '12h';
      // 初始化即同步全域時間制偏好，使首屏渲染的時間戳無閃爍。
      setTimeFormat12h(value === '12h');
      return value;
    }
    return '12h';
  });

  const [density, setDensityState] = useState<Density>(() => {
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem('touka_ui_density'); // noqa: LOCALSTORAGE_SSOT — 外觀偏好初始化，SSR 相容純字串
      return saved === 'compact' ? 'compact' : 'comfortable';
    }
    return 'comfortable';
  });

  const [bubbleStyle, setBubbleStyleState] = useState<BubbleStyle>(() => {
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem('touka_bubble_style'); // noqa: LOCALSTORAGE_SSOT — 外觀偏好初始化，SSR 相容純字串
      return saved === 'square' ? 'square' : 'rounded';
    }
    return 'rounded';
  });

  const [reduceMotion, setReduceMotionState] = useState<ReduceMotion>(() => {
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem('touka_reduce_motion'); // noqa: LOCALSTORAGE_SSOT — 外觀偏好初始化，SSR 相容純字串
      return saved === 'on' || saved === 'off' ? saved : 'system';
    }
    return 'system';
  });

  // 明暗模式（mode 軸）由 next-themes 管理（ADR-0105）；accent 軸仍由本 Context 管。
  // resolvedTheme 在首次水合前為 undefined → 退回 'dark'（與 defaultTheme="dark" 一致）。
  const { resolvedTheme } = useNextTheme();
  const mode: ColorMode = resolvedTheme === 'light' ? 'light' : 'dark';

  const applyTheme = (color: ThemeColor, themeMode: ColorMode) => {
    const theme = (themeMode === 'light' ? LIGHT_THEMES : THEMES)[color];
    const root = document.documentElement;
    
    // 將 Tokens 注入 CSS Variables
    root.style.setProperty('--primary', theme.primary);
    // WCAG A4 決策①②：CTA 深階按鈕底色（白字 ≥4.5）與表單控件邊框（≥3:1）
    root.style.setProperty('--primary-button', theme.primaryButton);
    root.style.setProperty('--input', theme.inputBorder);
    root.style.setProperty('--background', theme.bg);
    root.style.setProperty('--sidebar', theme.surface);
    root.style.setProperty('--card', theme.surface);
    root.style.setProperty('--border', theme.border);
    
    // 語意化變數與輔助色
    root.style.setProperty('--muted', theme.muted);
    root.style.setProperty('--accent', theme.accent);
    root.style.setProperty('--popover', theme.surface);
    root.style.setProperty('--secondary', theme.surface);
    root.style.setProperty('--surface-hover', theme.accent);
    
    // 生成輔助設計物件 (例如泡泡背景)
    root.style.setProperty('--bubble-bg', theme.muted + '40'); // 25% opacity
  };

  const applyFontSize = (size: number) => {
    document.documentElement.style.setProperty('--chat-font-size', `${size}px`);
  };

  // 手動「減少動態」：在 <html> 掛 class，與 globals.css 配合——
  // .motion-reduce＝強制降動畫；.motion-full＝忽略 OS 偏好強制保留動畫；無 class＝跟隨 OS。
  const applyReduceMotion = (value: ReduceMotion) => {
    const root = document.documentElement;
    root.classList.toggle('motion-reduce', value === 'on');
    root.classList.toggle('motion-full', value === 'off');
  };

  useEffect(() => {
    applyTheme(themeColor, mode);
    applyFontSize(fontSize);
    applyReduceMotion(reduceMotion);
    setTimeFormat12h(timeFormat === '12h');
  }, [themeColor, fontSize, mode, reduceMotion, timeFormat]);

  const setThemeColor = (color: ThemeColor) => {
    setThemeColorState(color);
    localStorage.setItem('touka_theme_color', color); // noqa: LOCALSTORAGE_SSOT — Theme SSOT，CSS 變數同步前必須先持久化，純字串
    applyTheme(color, mode);
  };

  const setFontSize = (size: number) => {
    setFontSizeState(size);
    localStorage.setItem('touka_font_size', size.toString()); // noqa: LOCALSTORAGE_SSOT — Theme SSOT，純字串數值存取
    applyFontSize(size);
  };

  const setTimeFormat = (value: TimeFormat) => {
    setTimeFormatState(value);
    localStorage.setItem('touka_time_format', value); // noqa: LOCALSTORAGE_SSOT — 外觀偏好，純字串
    setTimeFormat12h(value === '12h');
  };

  const setDensity = (value: Density) => {
    setDensityState(value);
    localStorage.setItem('touka_ui_density', value); // noqa: LOCALSTORAGE_SSOT — 外觀偏好，純字串
  };

  const setBubbleStyle = (value: BubbleStyle) => {
    setBubbleStyleState(value);
    localStorage.setItem('touka_bubble_style', value); // noqa: LOCALSTORAGE_SSOT — 外觀偏好，純字串
  };

  const setReduceMotion = (value: ReduceMotion) => {
    setReduceMotionState(value);
    localStorage.setItem('touka_reduce_motion', value); // noqa: LOCALSTORAGE_SSOT — 外觀偏好，純字串
    applyReduceMotion(value);
  };

  // framer-motion：'on'→always、'off'→never（忽略 OS）、'system'→user（跟隨 OS）。
  const motionMode = reduceMotion === 'on' ? 'always' : reduceMotion === 'off' ? 'never' : 'user';

  return (
    <ThemeContext.Provider value={{
      themeColor, setThemeColor, fontSize, setFontSize,
      timeFormat, setTimeFormat, density, setDensity,
      bubbleStyle, setBubbleStyle, reduceMotion, setReduceMotion,
    }}>
      {/* WCAG 2.2.2（A4）：framer-motion 預設尊重 OS「減少動態」偏好；
          使用者可於外觀設定手動覆寫（CSS 動畫由 globals.css 對應 class 處理）。 */}
      <MotionConfig reducedMotion={motionMode}>
        {children}
      </MotionConfig>
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  // 預設 context 已含安全外觀預設（見 DEFAULT_THEME）；唯讀消費者於無 Provider 時取得預設而不拋錯。
  return useContext(ThemeContext);
}

export { THEMES as themes };
