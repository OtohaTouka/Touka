import fs from 'fs';
import path from 'path';
import { Project } from 'ts-morph';
import glob from 'fast-glob';
import { OpenApiParser, FrontendScanner, ParityEngine } from './lib/guard';
import {
  collectContractAliasNames,
  compareContractBaseline,
  findHandwrittenContractTypes,
  isProductionSource,
} from './lib/contract_type_guard_logic';

const UPDATE_FLAG = '--update';
const CONTRACT_BASELINE = 'scripts/contract_type_baseline.json';
const CONTRACT_ALIASES_FILE = 'src/lib/types.ts';

/** 第 2 部分（禁令 51，ADR-0297）：和後端合約同名的手寫型別必須和基準完全相等。 */
async function checkContractTypes(projectRoot: string, schemaPath: string, update: boolean): Promise<number> {
  const schema = JSON.parse(fs.readFileSync(schemaPath, 'utf-8'));
  const project = new Project();
  const aliases = collectContractAliasNames(
    project.addSourceFileAtPath(path.resolve(projectRoot, CONTRACT_ALIASES_FILE)),
  );
  const contractNames = new Set([...Object.keys(schema.components?.schemas ?? {}), ...aliases]);
  const files = await glob('src/**/*.{ts,tsx}', { cwd: projectRoot });

  const current = files
    .filter(isProductionSource)
    .flatMap((file) =>
      findHandwrittenContractTypes(project.addSourceFileAtPath(path.resolve(projectRoot, file)), contractNames).map(
        (name) => `${file}:${name}`,
      ),
    )
    .sort();
  console.log(`✅ 已比對 ${contractNames.size} 個合約名稱，找到 ${current.length} 個同名手寫型別。`);

  const baselinePath = path.resolve(projectRoot, CONTRACT_BASELINE);
  if (update) {
    fs.writeFileSync(baselinePath, `${JSON.stringify(current, null, 2)}\n`, 'utf-8');
    console.log(`📝 已重拍 ${CONTRACT_BASELINE}。`);
    return 0;
  }
  const baseline: string[] = fs.existsSync(baselinePath) ? JSON.parse(fs.readFileSync(baselinePath, 'utf-8')) : [];
  const problems = compareContractBaseline(current, baseline);
  for (const problem of problems) {
    console.error(`🚨 [CONTRACT_TYPE_VIOLATION] ${problem}`);
  }
  if (problems.length > 0) {
    console.error(`💡 確認後重拍：npm run guard:api -- ${UPDATE_FLAG}\n`);
  }
  return problems.length;
}

async function main() {
  const args = process.argv.slice(2);
  if (args.some((arg) => arg !== UPDATE_FLAG)) {
    console.error(`用法：guard_frontend.ts [${UPDATE_FLAG}]`);
    process.exit(2);
  }

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

    // 4. 和後端合約同名的手寫型別
    const contractViolations = await checkContractTypes(projectRoot, schemaPath, args.includes(UPDATE_FLAG));

    if (violations === 0 && contractViolations === 0) {
      console.log(`🎉 恭喜！未發現 API 端點或合約型別不一致的問題。`);
      process.exit(0);
    } else {
      console.error(`❌ 總計發現 ${violations} 處 API 呼叫違規、${contractViolations} 處合約型別違規。`);
      process.exit(1);
    }
    } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`💥 執行發生錯誤: ${message}`);
    process.exit(1);
  }
}

main();
