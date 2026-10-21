/**
 * 對比度守衛（WCAG A4 批）— 以 WCAG 相對亮度公式實算全部主題的關鍵配對，
 * 把 1.4.3（文字 ≥4.5:1）與 1.4.11（非文字 ≥3:1）變成長期執法：
 * 未來任何調色（新增主題、改 primary、改邊框）不達標即紅。
 *
 * 決策（2026-06-12 擁有者拍板）：
 *   ① 各主題加深階按鈕底色 primaryButton（白字 CTA ≥4.5）
 *   ② 只提亮表單控件邊框 inputBorder（≥3:1；裝飾性卡片邊框維持）
 *
 * 雙模式擴充（ADR-0105，2026-06-21）：每個不變式對 **暗（THEMES）+ 亮（LIGHT_THEMES）**
 *   兩套權杖各跑一次（describe.each(['dark','light'])）。四不變式只寫一次、永不在模式間漂移。
 *   亮/暗皆採「深色實心 CTA + 白字」(--primary-foreground 兩模式皆白)，故白字不變式維持模式無關。
 *   muted-foreground 從 globals.css 對應模式區塊（.dark / .light）就地解析（block-scoped）。
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { THEMES, LIGHT_THEMES, PRIMITIVES } from '../../src/lib/design-system/tokens';

type Rgb = [number, number, number];
type Mode = 'dark' | 'light';

function hexToRgb(hex: string): Rgb {
  const h = hex.replace('#', '');
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
  ];
}

/** 解析 rgba(255,255,255,a) → {rgb, alpha}；亦容忍 hex（alpha=1）。 */
function parseColor(value: string): { rgb: Rgb; alpha: number } {
  const m = value.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\)/);
  if (m) return { rgb: [+m[1], +m[2], +m[3]], alpha: m[4] !== undefined ? +m[4] : 1 };
  return { rgb: hexToRgb(value), alpha: 1 };
}

function channelLin(c: number): number {
  const s = c / 255;
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

function luminance([r, g, b]: Rgb): number {
  return 0.2126 * channelLin(r) + 0.7152 * channelLin(g) + 0.0722 * channelLin(b);
}

function contrast(a: Rgb, b: Rgb): number {
  const [y1, y2] = [luminance(a), luminance(b)];
  const [hi, lo] = [Math.max(y1, y2), Math.min(y1, y2)];
  return (hi + 0.05) / (lo + 0.05);
}

/** 半透明前景在底色上的實效色（瀏覽器合成）。 */
function blendOver(fg: Rgb, alpha: number, bg: Rgb): Rgb {
  return [0, 1, 2].map((i) => alpha * fg[i] + (1 - alpha) * bg[i]) as Rgb;
}

/** 無彩度 oklch(L 0 0) → sRGB 灰（lms 立方 + sRGB 編碼）。 */
function oklchGreyToRgb(L: number): Rgb {
  const linear = L ** 3;
  const s = linear <= 0.0031308 ? 12.92 * linear : 1.055 * linear ** (1 / 2.4) - 0.055;
  const v = Math.round(s * 255);
  return [v, v, v];
}

/**
 * muted-foreground 的 SSOT 在 globals.css 對應模式的區塊（.dark / .light，oklch 無彩度灰）——
 * 從源頭就地解析，不寫死。先框出 `.<mode> { ... }` 區塊，再於其內 match，
 * 避免雙區塊並存時 regex 誤配第一個。
 */
function mutedForegroundRgb(mode: Mode): Rgb {
  const css = readFileSync(join(__dirname, '../../src/app/globals.css'), 'utf-8');
  const blockMatch = css.match(new RegExp(`\\.${mode}\\s*\\{([\\s\\S]*?)\\}`));
  expect(blockMatch, `globals.css 應有 .${mode} 區塊`).not.toBeNull();
  const m = blockMatch![1].match(/--muted-foreground:\s*oklch\(([\d.]+)\s+0\s+0\)/);
  expect(m, `.${mode} 區塊應有無彩度 oklch 的 --muted-foreground`).not.toBeNull();
  return oklchGreyToRgb(parseFloat(m![1]));
}

const WHITE = hexToRgb(PRIMITIVES.white);

// 雙模式權杖來源：暗＝THEMES、亮＝LIGHT_THEMES（同 key 形狀）。
const MODE_THEMES: Record<Mode, Record<string, Record<string, string>>> = {
  dark: THEMES as unknown as Record<string, Record<string, string>>,
  light: LIGHT_THEMES as unknown as Record<string, Record<string, string>>,
};

describe.each(Object.entries(MODE_THEMES))('對比度守衛（%s 模式，全 6 主題實算）', (mode, themes) => {
  const themeEntries = Object.entries(themes) as [string, Record<string, string>][];
  const mutedFg = mutedForegroundRgb(mode as Mode);

  it.each(themeEntries)('%s：CTA 深階按鈕底色上的白字 ≥ 4.5:1（決策①）', (_name, theme) => {
    expect(theme.primaryButton, '主題缺 primaryButton token').toBeTruthy();
    const btn = hexToRgb(theme.primaryButton);
    expect(contrast(WHITE, btn)).toBeGreaterThanOrEqual(4.5);
  });

  it.each(themeEntries)('%s：primary 作為文字（連結/強調）在 bg 上 ≥ 4.5:1', (_name, theme) => {
    expect(
      contrast(hexToRgb(theme.primary), hexToRgb(theme.bg)),
    ).toBeGreaterThanOrEqual(4.5);
  });

  it.each(themeEntries)('%s：muted-foreground 在 bg/surface/muted 上 ≥ 4.5:1', (_name, theme) => {
    for (const key of ['bg', 'surface', 'muted'] as const) {
      expect(
        contrast(mutedFg, hexToRgb(theme[key])),
        `muted-foreground on ${key}`,
      ).toBeGreaterThanOrEqual(4.5);
    }
  });

  it.each(themeEntries)(
    '%s：表單控件邊框 inputBorder 實效對比 ≥ 3:1（決策②，對 bg 與 muted 兩種襯底）',
    (_name, theme) => {
      expect(theme.inputBorder, '主題缺 inputBorder token').toBeTruthy();
      const { rgb, alpha } = parseColor(theme.inputBorder);
      for (const key of ['bg', 'muted'] as const) {
        const base = hexToRgb(theme[key]);
        expect(
          contrast(blendOver(rgb, alpha, base), base),
          `inputBorder over ${key}`,
        ).toBeGreaterThanOrEqual(3.0);
      }
    },
  );
});
