import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    rules: {
      // 🛡️ [Hygiene] 未使用變數/匯入告警；底線前綴＝刻意未使用（與 TS noUnusedLocals 的底線慣例一致）
      "@typescript-eslint/no-unused-vars": ["warn", {
        "argsIgnorePattern": "^_",
        "varsIgnorePattern": "^_",
        "caughtErrorsIgnorePattern": "^_"
      }],
      // 🛡️ [Component SSOT] 禁止跨過 shadcn/UI 直接引用底層 Radix UI
      "no-restricted-imports": [
        "error",
        {
          "patterns": [
            {
              "group": ["@radix-ui/*"],
              "message": "🚫 [SSOT] 請勿直接引用 Radix UI。請使用 @/components/ui/ 下已經過樣式封裝的組件。"
            }
          ]
        }
      ],
      // 🛡️ [Storage SSOT] 禁止直接使用 localStorage/sessionStorage，強制使用 Hooks
      "no-restricted-globals": [
        "warn",
        {
          "name": "localStorage",
          "message": "🚫 [SSOT] 請使用 src/hooks 下的相關 Hooks (如 useStudio) 以確保數據同步。"
        },
        {
          "name": "sessionStorage",
          "message": "🚫 [SSOT] 請將狀態下沉至領域 Hooks 管理。"
        },
        {
          "name": "fetch",
          "message": "🚫 [SSOT] 請使用 src/lib/connections.ts 中的 fetchTouka() 以確保認證 Token 自動注入。"
        }
      ],

      // 🛡️ [Architecture Guard] 禁止跨模組深度相對匯入。請使用 @/ 別名以維持 SSOT。
      "no-restricted-syntax": [
        "warn",
        {
          "selector": "ImportDeclaration[source.value=/^\\.\\.\\/\\.\\.\\//]",
          "message": "🚫 [Architecture] 禁止跨模組深度相對匯入。請使用 @/ 別名以維持 SSOT。"
        }
      ]
    }
  },
  {
    // Component SSOT 的封裝層本體：src/components/ui/ 即是規則指定的「唯一 Radix 入口」，
    // 於此放行直引（規則訊息自述「請使用 @/components/ui/ 封裝」——封裝層自身必須能 import）。
    files: ["src/components/ui/**"],
    rules: {
      "no-restricted-imports": "off"
    }
  },
  {
    files: ["src/**/*.ts", "src/**/*.tsx"],
    ignores: ["src/lib/types.ts"],
    rules: {
      "no-restricted-syntax": [
        "warn",
        {
          "selector": "TSInterfaceDeclaration[id.name='McpHubServer'], TSTypeAliasDeclaration[id.name='McpHubServer']",
          "message": "🚫 [Contract SSOT] 禁止手動重複定義 McpHubServer。請從 @/lib/types 匯入以確保與後端合約同步。"
        }
      ]
    }
  },
  {
    // 測試檔的 `any`：Mock／型別繞道屬可接受慣例，豁免 no-explicit-any（生產碼仍受棘輪 Guard #35 管制）
    files: ["**/*.test.ts", "**/*.test.tsx", "**/__tests__/**", "src/test/**"],
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
    },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Auto-generated artifact runtime bundle (esbuild output; ADR-0114)
    "src/generated/**",
  ]),
]);


export default eslintConfig;
