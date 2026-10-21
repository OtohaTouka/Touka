import { Project, SyntaxKind } from 'ts-morph';
import path from 'path';
import glob from 'fast-glob';

/**
 * Touka Frontend Import Integrity Guard
 * 
 * 專門掃描前端代碼中的匯入語句，確保所引用的符號在目標模組中確實存在。
 * 防止出現 "Export X doesn't exist in target module" 的編譯錯誤。
 */
async function checkIntegrity() {
    const projectRoot = path.resolve(__dirname, '..');
    console.log(`🔍 正在執行 Touka Frontend Import Integrity 檢查...`);

    const project = new Project({
        tsConfigFilePath: path.resolve(projectRoot, 'tsconfig.json'),
        skipAddingFilesFromTsConfig: true,
    });

    const files = await glob('src/**/*.{ts,tsx}', { cwd: projectRoot, absolute: true });
    project.addSourceFilesAtPaths(files);

    let violations = 0;

    for (const sourceFile of project.getSourceFiles()) {
        const relativePath = path.relative(projectRoot, sourceFile.getFilePath());
        
        // 獲取所有匯入宣告
        const importDeclarations = sourceFile.getImportDeclarations();
        
        for (const importDecl of importDeclarations) {
            const moduleSpecifier = importDecl.getModuleSpecifierValue();
            
            // 僅檢查內部模組 (以 @/ 或 ./ 開頭)
            if (!moduleSpecifier.startsWith('@/') && !moduleSpecifier.startsWith('.')) continue;

            const targetSourceFile = importDecl.getModuleSpecifierSourceFile();
            if (!targetSourceFile) {
                // 如果找不到源檔案，tsc 通常會報錯，但這裡我們重點檢查具體符號
                continue;
            }

            // 檢查具名匯入 (Named Imports)
            const namedImports = importDecl.getNamedImports();
            for (const namedImport of namedImports) {
                const name = namedImport.getName();
                
                // 檢查目標檔案是否有導出此名稱
                const exportedSymbols = targetSourceFile.getExportSymbols();
                const exists = exportedSymbols.some(s => s.getName() === name);

                if (!exists) {
                    console.error(`❌ [IMPORT_INTEGRITY_VIOLATION]`);
                    console.error(`  📄 檔案: ${relativePath}:${namedImport.getStartLineNumber()}`);
                    console.error(`  🚫 符號: "${name}" 不存在於模組 "${moduleSpecifier}" 中。`);
                    console.error(`  🔗 目標檔案: ${path.relative(projectRoot, targetSourceFile.getFilePath())}\n`);
                    violations++;
                }
            }
        }
    }

    if (violations === 0) {
        console.log(`✅ 前端匯入完整性檢查通過！未發現缺失的符號引用。`);
        process.exit(0);
    } else {
        console.error(`❌ 總計發現 ${violations} 處匯入違規。`);
        process.exit(1);
    }
}

checkIntegrity().catch(err => {
    console.error("💥 檢查過程中發生意外錯誤:", err);
    process.exit(1);
});
