import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Touka CSS Integrity Guard
 * 職責：確保 globals.css 符合 Tailwind v4 的語義化映射規範。
 */

const __dirname = dirname(fileURLToPath(import.meta.url));
const GLOBALS_CSS_PATH = join(__dirname, '../src/app/globals.css');

// 必須映射至 Tailwind --color-* 的核心權杖
const REQUIRED_COLOR_MAPPINGS = [
    'background',
    'foreground',
    'primary',
    'border',
    'card',
    'sidebar',
    'ring'
];

function checkCssIntegrity() {
    console.log('🔍 正在執行 Touka CSS 完整性檢查 (Build-time Safety)...');
    
    let cssContent: string;
    try {
        cssContent = readFileSync(GLOBALS_CSS_PATH, 'utf8');
    } catch (err) {
        console.error(`❌ 無法讀取 globals.css: ${err}`);
        process.exit(1);
    }

    const violations: string[] = [];

    // 1. 檢查 @theme 區塊中是否包含核心顏色映射
    // 在 Tailwind v4 中，這通常寫作 --color-xxx: var(--xxx);
    REQUIRED_COLOR_MAPPINGS.forEach(token => {
        const pattern = `--color-${token}:\\s*var\\(--${token}\\);`;
        const regex = new RegExp(pattern);
        
        if (!regex.test(cssContent)) {
            violations.push(`缺失核心映射: @theme 區塊中未發現 --color-${token}: var(--${token});`);
        }
    });

    // 2. 檢查 @apply 的有效性 (基本檢查)
    // 尋找所有的 @apply border-border, bg-background 等，並確認其對應的 --color-xxx 存在
    const applyMatches = cssContent.match(/@apply\s+([^;]+);/g) || [];
    applyMatches.forEach(match => {
        const classes = match.replace('@apply', '').replace(';', '').trim().split(/\s+/);
        classes.forEach(cls => {
            // 處理 border-border, bg-primary, text-foreground 等格式
            // 邏輯：如果是 bg-X, text-X, border-X, outline-X，檢查 X 是否在 theme 中定義
            const colorMatch = cls.match(/^(?:bg|text|border|outline)-([a-z-]+)(?:\/\d+)?$/);
            if (colorMatch) {
                const colorName = colorMatch[1];
                // 如果 colorName 是我們定義的核心權杖，確保它在 theme 中有映射
                if (REQUIRED_COLOR_MAPPINGS.includes(colorName)) {
                    const themePattern = `--color-${colorName}:`;
                    if (!cssContent.includes(themePattern)) {
                        violations.push(`語法衝突: globals.css 使用了 @apply ${cls}，但主題中未定義 --color-${colorName}`);
                    }
                }
            }
        });
    });

    // 3. 輸出報告
    if (violations.length > 0) {
        console.error(`\n❌ 發現 ${violations.length} 處 CSS 語法或映射違規：\n`);
        violations.forEach(v => console.error(`- ${v}`));
        console.error('\n提示: 請執行 `npm run sync:design` 來自動生成遺失的映射。');
        process.exit(1);
    } else {
        console.log('\n✅ CSS 完整性檢查通過！所有語義化工具類均已正確映射。');
        process.exit(0);
    }
}

checkCssIntegrity();
