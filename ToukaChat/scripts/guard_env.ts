import path from 'path';
import { EnvGuardScanner } from './lib/env_guard_logic';

async function main() {
  const projectRoot = path.resolve(__dirname, '..');

  console.log(`🔍 Touka Frontend Env Guard 正在啟動...`);
  console.log(`📂 專案目錄: ${projectRoot}\n`);

  try {
    const scanner = new EnvGuardScanner(projectRoot);
    const violations = await scanner.scan();

    if (violations.length === 0) {
      console.log(`🎉 恭喜！未發現環境變數違規調用。`);
      process.exit(0);
    } else {
      console.error(`🚨 [FRONTEND_ENV_SSOT_VIOLATION] 偵測到違規呼叫環境變數！`);
      console.error(`💡 規定：所有環境變數必須透過 'src/lib/config.ts' 進行 SSOT 封裝，嚴禁在組件中直接調用。\n`);
      
      for (const v of violations) {
        console.error(`  📄 檔案: ${v.file}:${v.line}`);
        console.error(`  ❌ 違規變數: ${v.variable}`);
        console.error(`  📝 內容: ${v.content}\n`);
      }

      console.error(`❌ 總計發現 ${violations.length} 處環境變數違規。`);
      process.exit(1);
    }
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`💥 執行發生錯誤: ${message}`);
    process.exit(1);
  }
}

main();
