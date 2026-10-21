/**
 * Design Tokens SSOT (Single Source of Truth)
 * 所有的顏色、間距、圓角與 Agent 識別規範都在此處定義。
 * 修改此檔案後，請執行 `npm run sync:design` 將變更同步至 globals.css。
 */

export const PRIMITIVES = {
    // 基礎調色盤 (Core Palette)
    touka: {
        50: '#eff6ff',
        100: '#dbeafe',
        500: '#3b82f6',
        600: '#2b8cee', // 核心 Touka Blue
        700: '#1d6fd1', // CTA 深階（白字 4.95:1，WCAG A4 決策①）
        900: '#1e3a8a',
    },
    rose: {
        50: '#fff1f2',
        500: '#f43f5e',
        600: '#e11d48', // CTA 深階（白字 4.70:1）
        700: '#be123c', // 亮底文字 ≥4.5:1（rose 600 當文字僅 4.46，ADR-0105）
    },
    amber: {
        50: '#fffbeb',
        500: '#f59e0b',
        600: '#d97706',
        700: '#b45309', // CTA 深階（白字 5.02:1）
    },
    emerald: {
        50: '#ecfdf5',
        500: '#10b981',
        600: '#059669',
        700: '#047857', // CTA 深階（白字 5.48:1）
    },
    violet: {
        50: '#f5f3ff',
        500: '#8b5cf6',
        600: '#7c3aed', // CTA 深階（白字 5.70:1）
    },
    slate: {
        50: '#f8fafc',
        400: '#7c8ba1', // 亮階鋼鐵灰（深底文字 5.46:1——原 500 當文字僅 3.97 不及格）
        500: '#64748b',
        600: '#475569', // CTA 深階（白字 7.58:1）
        900: '#0f172a',
    },
    indigo: {
        500: '#6366f1',
    },
    sky: {
        500: '#0ea5e9',
    },
    black: '#0d1117',
    white: '#ffffff',
};

export const AGENT_TOKENS = {
    Amber: {
        primary: PRIMITIVES.amber[500],
        accent: PRIMITIVES.amber[50],
        label: "Amber",
    },
    Rose: {
        primary: PRIMITIVES.rose[500],
        accent: PRIMITIVES.rose[50],
        label: "Rose",
    },
    Emerald: {
        primary: PRIMITIVES.emerald[500],
        accent: PRIMITIVES.emerald[50],
        label: "Emerald",
    },
    DebugAgent: {
        primary: PRIMITIVES.touka[600],
        accent: PRIMITIVES.touka[50],
        label: "Touka Debug",
    },
};

export const SEMANTIC_TOKENS = {
    radius: '0.625rem',
    font: {
        sans: '"Geist Sans", sans-serif',
        display: '"Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif',
        mono: '"Geist Mono", monospace',
    },
    brand: {
        discord: '#6366f1',
        telegram: '#0ea5e9',
    },
    status: {
        success: PRIMITIVES.touka[600],
        warning: PRIMITIVES.amber[500],
        error: PRIMITIVES.rose[500],
    }
};

// WCAG A4 決策②：表單控件邊框統一提亮至實效 ≥3:1（最差襯底 3.62:1）；
// 裝飾性卡片邊框（border token）維持原淡色——只有表單控件改用 inputBorder。
const FORM_INPUT_BORDER = 'rgba(255,255,255,0.4)';

// 主題定義 (與現有 ThemeContext 相容)
// primaryButton（WCAG A4 決策①）：CTA 實心底色，白字 ≥4.5:1（tests/guard/a11y_contrast 執法）。
export const THEMES = {
    touka: {
        name: 'Touka 經典',
        primary: PRIMITIVES.touka[600],
        primaryButton: PRIMITIVES.touka[700],
        bg: '#0d1117',
        surface: '#161b22',
        muted: '#1f2937',
        accent: '#21262d',
        border: 'rgba(255,255,255,0.1)',
        inputBorder: FORM_INPUT_BORDER,
    },
    rose: {
        name: '幻夢夜櫻',
        primary: PRIMITIVES.rose[500],
        primaryButton: PRIMITIVES.rose[600],
        bg: '#181114',
        surface: '#21151a',
        muted: '#2d1a1e',
        accent: '#352126',
        border: 'rgba(244,63,94,0.15)',
        inputBorder: FORM_INPUT_BORDER,
    },
    amber: {
        name: '琥珀宮殿',
        primary: PRIMITIVES.amber[500],
        primaryButton: PRIMITIVES.amber[700],
        bg: '#171410',
        surface: '#211c15',
        muted: '#2a241c',
        accent: '#352d24',
        border: 'rgba(245,158,11,0.15)',
        inputBorder: FORM_INPUT_BORDER,
    },
    emerald: {
        name: '翠玉矩陣',
        primary: PRIMITIVES.emerald[500],
        primaryButton: PRIMITIVES.emerald[700],
        bg: '#0a1411',
        surface: '#111e18',
        muted: '#1a2a24',
        accent: '#24352d',
        border: 'rgba(16,185,129,0.15)',
        inputBorder: FORM_INPUT_BORDER,
    },
    violet: {
        name: '深空星雲',
        primary: PRIMITIVES.violet[500],
        primaryButton: PRIMITIVES.violet[600],
        bg: '#0f0a1c',
        surface: '#170f2b',
        muted: '#221835',
        accent: '#2b2145',
        border: 'rgba(139,92,246,0.15)',
        inputBorder: FORM_INPUT_BORDER,
    },
    slate: {
        name: '極簡鋼鐵',
        // WCAG：500 當深底文字僅 3.97:1 → 亮一階至 400（5.46:1），鋼鐵調性不變
        primary: PRIMITIVES.slate[400],
        primaryButton: PRIMITIVES.slate[600],
        bg: '#0f1115',
        surface: '#181a1f',
        muted: '#24272e',
        accent: '#2d323b',
        border: 'rgba(148,163,184,0.1)',
        inputBorder: FORM_INPUT_BORDER,
    },
};

// ── 亮模式（ADR-0105）──────────────────────────────────────────────
// 亮模式表單邊框：黑疊層（白疊層在亮底無效），實效 ≥3:1（決策②的亮模式對應）。
const LIGHT_FORM_INPUT_BORDER = 'rgba(0,0,0,0.5)';

// 亮模式中性色（中性灰白系，6 主題共用 = mode×accent 正交，ADR-0105 決策①）。
// 刻意不做 per-accent 染色：換取單一可預測的亮底 + 對比度守衛只需認證 1 套襯底。
const LIGHT_NEUTRALS = {
    bg: '#f8fafc',       // slate-50（頁面底）
    surface: '#ffffff',  // 卡片／側欄
    muted: '#f1f5f9',    // slate-100（次層背景）
    accent: '#e2e8f0',   // slate-200（三層背景）
    border: 'rgba(0,0,0,0.1)',       // 裝飾性邊框（淡黑疊層）
    inputBorder: LIGHT_FORM_INPUT_BORDER,
};

// 亮模式主題（與 THEMES 同 key 形狀，供 applyTheme / 對比度守衛以同邏輯消費）。
// primary＝亮底可讀的文字角色 accent（≥4.5:1 on bg）；primaryButton＝白字深階 CTA（≥4.5:1）。
export const LIGHT_THEMES = {
    touka:   { name: THEMES.touka.name,   primary: PRIMITIVES.touka[700],   primaryButton: PRIMITIVES.touka[700],   ...LIGHT_NEUTRALS },
    rose:    { name: THEMES.rose.name,    primary: PRIMITIVES.rose[700],    primaryButton: PRIMITIVES.rose[600],    ...LIGHT_NEUTRALS },
    amber:   { name: THEMES.amber.name,   primary: PRIMITIVES.amber[700],   primaryButton: PRIMITIVES.amber[700],   ...LIGHT_NEUTRALS },
    emerald: { name: THEMES.emerald.name, primary: PRIMITIVES.emerald[700], primaryButton: PRIMITIVES.emerald[700], ...LIGHT_NEUTRALS },
    violet:  { name: THEMES.violet.name,  primary: PRIMITIVES.violet[600],  primaryButton: PRIMITIVES.violet[600],  ...LIGHT_NEUTRALS },
    slate:   { name: THEMES.slate.name,   primary: PRIMITIVES.slate[600],   primaryButton: PRIMITIVES.slate[600],   ...LIGHT_NEUTRALS },
};
