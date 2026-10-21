import type { KnipConfig } from "knip";

const config: KnipConfig = {
  entry: [
    // Next.js app router entry points（頁面自動豁免）
    "src/app/**/page.tsx",
    "src/app/**/layout.tsx",
    "src/app/**/loading.tsx",
    "src/app/**/error.tsx",
    "src/app/**/not-found.tsx",
    "src/app/**/template.tsx",
    // Guard scripts（CI 工具）
    "scripts/**/*.ts",
    // Explicit Docker builder / offline replay entry (ADR-0226).
    "scripts/ensure-critical-native.mjs",
    // Next.js config files
    "next.config.ts",
    "tailwind.config.ts",
    // esbuild 入口：artifact 沙箱 runtime（非 app 匯入，由 build-artifact-runtime.mjs 打包；ADR-0114）
    "src/lib/artifacts/runtime-entry.ts",
  ],
  // guard:prune 帶 --no-gitignore（.toukadev/worktrees/ 底下 gitignore 會濾掉入口檔；ADR-0243）。
  // 原本靠 .gitignore 擋掉的產生檔與建置產物在此移出相依圖；只放 ignore 時它們仍會被分析，
  // 其引用會讓沒用到的 export 被當成有用到。逐條清單見 tests/unit/test_knip_worktree_parity.py。
  project: [
    "**/*.{js,mjs,cjs,jsx,ts,tsx,mts,cts}!",
    "!src/generated/**",
    "!.next/**",
    "!out/**",
    "!build/**",
    "!coverage/**",
    "!storybook-static/**",
    "!**/.vercel/**",
    "!**/.vite/**",
    "!**/next-env.d.ts",
  ],
  ignore: [
    // Auto-generated OpenAPI types（由 sync-api script 產生）
    "src/types/api.ts",
    // Auto-generated artifact runtime bundle（esbuild 產物；ADR-0114）
    "src/generated/**",
    // Test files
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    // Node.js scripts（非 React 環境）
    "src/lib/terminal_server.mjs",
    // Shadcn UI 組件庫（所有 export 均為刻意的可組合 API）
    "src/components/ui/**",
  ],
  ignoreDependencies: [
    // Next.js / Tailwind 的隱式依賴
    "tw-animate-css",
    "postcss",
    // Radix UI 透過 radix-ui 包含（非直接依賴）
    "@radix-ui/*",
    // 暫時保留的相容依賴
    "react-diff-view",
    "socket.io-client",
    "xterm-addon-fit",
    "@astral-sh/ruff-wasm-web",
  ],
  ignoreBinaries: ["tsx"],
  // 不報告 TypeScript 型別導出（誤報率高，類型可被隱式使用）
  include: ["files", "exports", "unlisted", "duplicates"],
};

export default config;
