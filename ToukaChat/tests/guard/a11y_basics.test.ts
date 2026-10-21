/**
 * A11y 基線守衛（WCAG 2.2 AA 分批修復 A1）— 源碼掃描型，長期執法。
 *
 * 1) 4.1.2：所有 size="icon" 按鈕必須有可及名稱（aria-label 或 title）
 * 2) 3.3.2/1.3.5/2.4.6：login/register 須有 label 關聯、autoComplete、h1
 * 3) 2.4.2：每條子路由須有獨立頁面標題（segment layout.tsx 的 metadata）
 * 4) 4.1.3：狀態元件須有 live region 語意（role="status"）
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, relative } from 'node:path';

const SRC = join(__dirname, '../../src');
const APP = join(SRC, 'app');

/** 遞迴蒐集 src 下所有 .tsx 源檔（排除測試檔）。 */
function collectTsx(dir: string, acc: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === '__tests__' || name === 'node_modules') continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) collectTsx(p, acc);
    else if (name.endsWith('.tsx') && !name.includes('.test.')) acc.push(p);
  }
  return acc;
}

/** 取出 content 中 idx 所在 JSX 開標籤全文（容忍 props 內的 `=>` 箭頭）。 */
function openingTagAt(content: string, idx: number): string {
  const start = content.lastIndexOf('<', idx);
  let i = idx;
  while (i < content.length) {
    const gt = content.indexOf('>', i);
    if (gt === -1) return content.slice(start);
    if (content[gt - 1] === '=') {
      i = gt + 1; // 跳過 onClick={() => ...} 的箭頭
      continue;
    }
    return content.slice(start, gt + 1);
  }
  return content.slice(start);
}

function lineOf(content: string, idx: number): number {
  return content.slice(0, idx).split('\n').length;
}

describe('a11y 基線守衛', () => {
  it('所有 size="icon" 按鈕都有可及名稱（aria-label 或 title）— WCAG 4.1.2', () => {
    const violations: string[] = [];
    for (const file of collectTsx(SRC)) {
      const content = readFileSync(file, 'utf-8');
      let idx = content.indexOf('size="icon"');
      while (idx !== -1) {
        const tag = openingTagAt(content, idx);
        if (!/aria-label=|title=/.test(tag)) {
          violations.push(`${relative(SRC, file)}:${lineOf(content, idx)}`);
        }
        idx = content.indexOf('size="icon"', idx + 1);
      }
    }
    expect(violations, `icon 按鈕缺可及名稱（螢幕閱讀器只會唸「按鈕」）：\n${violations.join('\n')}`).toEqual([]);
  });

  it.each(['login', 'register'])(
    '%s 頁有 label 關聯、autoComplete 與 h1 — WCAG 3.3.2/1.3.5/2.4.6',
    (route) => {
      const content = readFileSync(join(APP, route, 'page.tsx'), 'utf-8');
      expect(content, `${route} 缺 <label>（placeholder 不是程式化標籤）`).toMatch(/<label/i);
      expect(content, `${route} email 缺 autoComplete`).toMatch(/autoComplete="(username|email)"/);
      expect(content, `${route} password 缺 autoComplete`).toMatch(
        /autoComplete="(current-password|new-password)"/,
      );
      expect(content, `${route} 缺 h1 標題元素`).toMatch(/<h1[\s>]/);
    },
  );

  it.each(['login', 'register', 'connections', 'design-system'])(
    '子路由 %s 有獨立頁面標題（segment layout metadata）— WCAG 2.4.2',
    (route) => {
      const layout = join(APP, route, 'layout.tsx');
      expect(existsSync(layout), `${route}/layout.tsx 不存在（client page 無法自帶 metadata）`).toBe(true);
      const content = readFileSync(layout, 'utf-8');
      expect(content, `${route}/layout.tsx 缺 metadata title`).toMatch(/title:\s*["'`]/);
    },
  );

  it.each([
    'components/sidebar/monitoring/ConnectionStatus.tsx',
    'components/sidebar/monitoring/BackgroundActivity.tsx',
    'components/sidebar/monitoring/InterruptedRunsNotice.tsx',
    'components/chat/AgentActivityStrip.tsx',
    'components/chat/ChatArea.tsx', // 串流回覆完成的 sr-only 宣告器
  ])('%s 有 live region 語意（role="status"）— WCAG 4.1.3', (rel) => {
    const content = readFileSync(join(SRC, rel), 'utf-8');
    expect(content, `${rel} 狀態變化對螢幕閱讀器無聲`).toMatch(/role="status"/);
  });
});

describe('a11y 鍵盤可操作性守衛（A2 批）', () => {
  it('行動版背景遮罩：裝飾層 aria-hidden ＋ Esc 鍵盤替代 — WCAG 2.1.1', () => {
    const content = readFileSync(join(APP, 'page.tsx'), 'utf-8');
    expect(content, '遮罩是滑鼠專用控制，鍵盤關不掉側欄').toMatch(/Escape/);
    expect(content, '遮罩為裝飾層，應 aria-hidden').toMatch(/aria-hidden="true"/);
  });

  it('skip link：root layout 有「跳至主內容」、主要頁面 main 有錨點 — WCAG 2.4.1', () => {
    const layout = readFileSync(join(APP, 'layout.tsx'), 'utf-8');
    expect(layout, 'root layout 缺 skip link').toMatch(/#main-content/);
    for (const rel of ['page.tsx', 'connections/page.tsx']) {
      const content = readFileSync(join(APP, rel), 'utf-8');
      expect(content, `${rel} 的 <main> 缺 id="main-content" 錨點`).toMatch(/id="main-content"/);
    }
  });

  it.each(['login', 'register'])(
    '%s 頁不得用 <span onClick> 當連結（鍵盤不可達）— WCAG 2.1.1',
    (route) => {
      const content = readFileSync(join(APP, route, 'page.tsx'), 'utf-8');
      expect(content, `${route} 的切換連結是滑鼠專用 span`).not.toMatch(
        /<span[\s\S]{0,200}?onClick/,
      );
    },
  );

  it.each(['login', 'register'])(
    '%s 頁有 main 地標＋skip link 錨點 — WCAG（A5 axe：landmark/region/skip-link）',
    (route) => {
      const content = readFileSync(join(APP, route, 'page.tsx'), 'utf-8');
      expect(content, `${route} 缺 <main id="main-content">`).toMatch(
        /<main id="main-content"/,
      );
    },
  );

  it('SessionItem：原生 button 切換＋無巢狀互動＋可見焦點 — WCAG 2.1.1/4.1.2/2.4.7', () => {
    const content = readFileSync(
      join(SRC, 'components/sidebar/left/SessionItem.tsx'),
      'utf-8',
    );
    // A5 axe nested-interactive 根治：切換改原生 <button>，外層不得 role="button"
    expect(content, '外層容器不得是 role="button"（nested-interactive）').not.toMatch(
      /role="button"/,
    );
    expect(content, '切換需有 aria-current 標記當前會話').toMatch(/aria-current/);
    expect(content, '選單觸發鈕需具名').toMatch(/aria-label=\{t\('sidebar\.session_item\.session_options/);
    expect(content, '會話列/編輯框需可見焦點樣式').toMatch(/focus-visible:ring/);
  });

  it('tag-input：移除鈕有可及名稱與可見焦點 — WCAG 4.1.2/2.4.7', () => {
    const content = readFileSync(join(SRC, 'components/ui/tag-input.tsx'), 'utf-8');
    expect(content, '移除鈕是 icon-only，需 aria-label').toMatch(/aria-label=\{t\("ui\.tag_input\.remove_tag_aria/);
    expect(content, '移除鈕 focus:outline-none 需 focus-visible 補償').toMatch(
      /focus-visible:ring/,
    );
  });

  it('ToolStepTracker：展開列有 aria-expanded — WCAG 4.1.2', () => {
    const content = readFileSync(join(SRC, 'components/chat/ToolStepTracker.tsx'), 'utf-8');
    expect(content).toMatch(/aria-expanded/);
  });

  it('ProjectSidebarView 檔案樹目錄列為原生 button＋aria-expanded（非裸 div onClick）— WCAG 2.1.1/4.1.2', () => {
    const content = readFileSync(join(SRC, 'components/projects/ProjectSidebarView.tsx'), 'utf-8');
    expect(content, '檔案樹目錄切換需 aria-expanded 暴露展開狀態（鍵盤可操作）').toMatch(/aria-expanded/);
  });
});

describe('a11y 視覺合規守衛（A4 批）', () => {
  it('globals.css 尊重 prefers-reduced-motion — WCAG 2.2.2', () => {
    const css = readFileSync(join(APP, 'globals.css'), 'utf-8');
    expect(css).toMatch(/prefers-reduced-motion:\s*reduce/);
  });

  it('Button 預設 CTA 用深階 primary-button（白字 ≥4.5 由 a11y_contrast 執法）', () => {
    const content = readFileSync(join(SRC, 'components/ui/button.tsx'), 'utf-8');
    expect(content).toMatch(/bg-primary-button/);
  });

  it('全 src 禁低 alpha 的 muted-foreground 文字（placeholder 2.98:1 教訓）— WCAG 1.4.3', () => {
    const offenders: string[] = [];
    for (const file of collectTsx(SRC)) {
      const content = readFileSync(file, 'utf-8');
      const m = content.match(/text-muted-foreground\/(30|40|50|60|70)\b/);
      if (m) offenders.push(`${relative(SRC, file)} (${m[0]})`);
    }
    expect(offenders, `低 alpha 次要文字不達 4.5:1：\n${offenders.join('\n')}`).toEqual([]);
  });

  it('framer-motion 全域接 MotionConfig 並預設尊重 OS 減少動態（user）', () => {
    // 外觀打磨包：MotionConfig 改為依使用者偏好 reduceMotion 動態綁定；
    // 預設（reduceMotion=system）對應 framer-motion 的 'user'＝尊重 OS。
    const content = readFileSync(join(SRC, 'lib/ThemeContext.tsx'), 'utf-8');
    expect(content).toMatch(/MotionConfig reducedMotion=\{motionMode\}/);
    expect(content).toMatch(/:\s*'user'/); // system 分支保留 OS 行為
  });
});

describe('a11y modal 守衛（A3 批）— WCAG 2.1.2 焦點陷阱', () => {
  it.each([
    'components/modals/EvolutionModal.tsx',
    'components/modals/ImportAgentModal.tsx',
    'components/layout/SoulStudio.tsx',
    'components/modals/McpInstallModal.tsx',
    'components/modals/SkillInstallModal.tsx',
    'components/dashboard/EnvSettings.tsx',
  ])('%s 使用 Radix Dialog、不得手寫 fixed inset-0 覆蓋層', (rel) => {
    const content = readFileSync(join(SRC, rel), 'utf-8');
    expect(content, `${rel} 應使用 ui/dialog 的 DialogContent`).toMatch(/<DialogContent/);
    expect(
      content,
      `${rel} 仍有手寫覆蓋層（無焦點陷阱/Esc/焦點歸位）——應由 Radix Dialog 接管`,
    ).not.toMatch(/className="fixed inset-0/);
  });
});

describe('a11y P3 控制語意守衛 — WCAG 2.1.1/4.1.2', () => {
  it('IdentityMapping 能力開關為鍵盤可達 toggle（aria-pressed）', () => {
    const content = readFileSync(join(SRC, 'components/settings/tabs/security/IdentityMapping.tsx'), 'utf-8');
    expect(content, '能力開關應以 aria-pressed 暴露 toggle 狀態').toMatch(/aria-pressed/);
  });
});
