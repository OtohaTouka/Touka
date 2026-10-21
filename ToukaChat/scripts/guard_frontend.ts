import path from 'path';
import { OpenApiParser, FrontendScanner, ParityEngine } from './lib/guard';

async function main() {
  const projectRoot = path.resolve(__dirname, '..');
  const schemaPath = path.resolve(projectRoot, '../openapi.json');

  console.log(`🔍 Touka API Parity Guard 正在啟動...`);
  console.log(`📂 專案目錄: ${projectRoot}`);
  console.log(`📄 Schema 路徑: ${schemaPath}\n`);

  try {
    // 1. 解析後端路由
    const parser = new OpenApiParser(schemaPath);
    const backendRoutes = parser.getRoutes();
    const engine = new ParityEngine(backendRoutes);
    console.log(`✅ 已載入 ${backendRoutes.length} 個後端路由 (SSOT)。`);

    // 2. 掃描前端代碼
    const scanner = new FrontendScanner(projectRoot);
    const calls = await scanner.scanCalls();
    console.log(`✅ 已掃描到 ${calls.length} 處 API 呼叫。\n`);

    // 3. 執行驗證
    let violations = 0;
    for (const call of calls) {
      const result = engine.validate(call.url);
      if (!result.valid) {
        console.error(`🚨 [API_PARITY_VIOLATION]`);
        console.error(`  📄 檔案: ${call.file}:${call.line}`);
        console.error(`  ❌ 錯誤路徑: "${call.url}"`);
        console.error(`  💡 原因: ${result.error || '在後端路由中找不到此端點，請檢查是否有拼字錯誤或介面已變更。'}\n`);
        violations++;
      }
    }

    if (violations === 0) {
      console.log(`🎉 恭喜！未發現 API 端點不一致的問題。`);
      process.exit(0);
    } else {
      console.error(`❌ 總計發現 ${violations} 處 API 呼叫違規。`);
      process.exit(1);
    }
    } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`💥 執行發生錯誤: ${message}`);
    process.exit(1);
  }
}

main();
